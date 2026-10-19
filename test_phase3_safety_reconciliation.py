#!/usr/bin/env python3
"""
test_phase3_safety_reconciliation.py

Comprehensive test harness for Phase 3:
1. Stale-Tick Watchdog:
   - Verifies trading halt when feed delta / latency > 1500ms
   - Verifies trading halt when tick heartbeat expires (>1500ms)
   - Verifies auto-recovery when healthy ticks arrive (<800ms for 3 consecutive samples)
   - Verifies OMS aborts order dispatch when watchdog is halted (ABORTED_STALE_TICK)
2. Startup Exchange State Reconciler:
   - Verifies synchronization between Kalshi /portfolio/positions & /portfolio/orders and SQLite kalshi_audit.db
   - Closes offline settled contracts with EXCHANGE_RECONCILED_OFFLINE
   - Ingests active exchange positions into DB with RECONCILED_FROM_EXCHANGE
   - Cancels orphaned resting orders
"""

import sys
import os
import time
import asyncio
import sqlite3
import tempfile

from live_executor import (
    StaleTickWatchdog,
    KalshiStateReconciler,
    KalshiOrderManagementSystem,
    L2OrderBook
)
from bot_database import AuditDatabase


def run_watchdog_tests():
    print("=" * 80)
    print("       1. TESTING STALE-TICK WATCHDOG (LATENCY & HEARTBEAT)")
    print("=" * 80)

    watchdog = StaleTickWatchdog(
        max_allowed_delta_ms=1500.0,
        recovery_threshold_ms=800.0,
        recovery_ticks=3
    )

    # 1. Healthy ticks
    t0 = time.time()
    ok = watchdog.record_tick(feed_timestamp=t0, reported_latency_ms=120.0)
    assert ok is True, "Healthy tick should not trip watchdog"
    assert watchdog.is_safe_to_trade() is True, "Watchdog should report safe to trade"
    assert watchdog.is_halted is False, "Watchdog should not be halted"
    print("  ✓ Case 1.1: Healthy tick (120ms latency) -> Safe to trade.")

    # 2. Stale tick (> 1500ms latency)
    stale_ok = watchdog.record_tick(feed_timestamp=t0 - 2.5, reported_latency_ms=2500.0)
    assert stale_ok is False, "Stale tick must trip watchdog"
    assert watchdog.is_halted is True, "Watchdog must be halted"
    assert watchdog.is_safe_to_trade() is False, "Watchdog must block trading"
    assert "STALE_TICK_DELTA_EXCEEDED" in watchdog.halt_reason
    print(f"  ✓ Case 1.2: Stale tick (2500ms delta) -> Circuit breaker TRIPPED ({watchdog.halt_reason[:45]}...).")

    # 3. Ticks arriving but still elevated (> 800ms recovery threshold)
    still_slow = watchdog.record_tick(reported_latency_ms=1100.0)
    assert watchdog.is_halted is True, "Watchdog should remain halted while latency is 1100ms"
    print("  ✓ Case 1.3: Latency at 1100ms -> Circuit breaker remains active.")

    # 4. Recovery hysteresis (requires 3 consecutive ticks <= 800ms)
    watchdog.record_tick(reported_latency_ms=300.0) # Tick 1
    assert watchdog.is_halted is True, "Still halted after 1 recovery tick"
    watchdog.record_tick(reported_latency_ms=250.0) # Tick 2
    assert watchdog.is_halted is True, "Still halted after 2 recovery ticks"
    watchdog.record_tick(reported_latency_ms=180.0) # Tick 3
    assert watchdog.is_halted is False, "Watchdog should recover after 3 healthy ticks"
    assert watchdog.is_safe_to_trade() is True, "Trading should resume"
    print("  ✓ Case 1.4: Auto-Recovery verified (3 consecutive ticks <800ms) -> Resumed trading.")

    # 5. Heartbeat timeout
    watchdog.last_tick_time = time.time() - 2.0 # 2000ms ago
    assert watchdog.check_heartbeat() is False, "Heartbeat should fail after 2000ms silence"
    assert watchdog.is_halted is True, "Watchdog should be halted on heartbeat timeout"
    print(f"  ✓ Case 1.5: Heartbeat timeout (2000ms gap) -> Circuit breaker TRIPPED.")


async def run_oms_watchdog_integration_tests():
    print("\n" + "=" * 80)
    print("       2. TESTING OMS WATCHDOG CIRCUIT BREAKER ENFORCEMENT")
    print("=" * 80)

    watchdog = StaleTickWatchdog(max_allowed_delta_ms=1500.0)
    oms = KalshiOrderManagementSystem(
        auth=None,
        watchdog=watchdog,
        dry_run=True
    )

    # Trip the watchdog manually
    watchdog.record_tick(reported_latency_ms=3200.0)
    assert watchdog.is_halted is True

    # Attempt to place trade via OMS
    result = await oms.execute_entry_with_vwap_protection(
        ticker="KXBTC15M-TEST",
        side="yes",
        contracts=10,
        model_prob=0.85,
        max_acceptable_vwap_cents=70
    )

    assert result["status"] == "ABORTED_STALE_TICK", f"Expected ABORTED_STALE_TICK, got {result['status']}"
    print(f"  ✓ Case 2.1: OMS correctly aborted order when watchdog tripped: {result['status']} ({result['reason'][:40]}...).")

    # Recover watchdog
    for _ in range(3):
        watchdog.record_tick(reported_latency_ms=100.0)
    assert watchdog.is_safe_to_trade() is True

    # Order should now proceed past watchdog check
    result2 = await oms.execute_entry_with_vwap_protection(
        ticker="KXBTC15M-TEST",
        side="yes",
        contracts=10,
        model_prob=0.85,
        max_acceptable_vwap_cents=70
    )
    # Since dry_run is True and no book exists, it dispatches simulated order
    assert result2["status"] in ("simulated", "executed", "FILLED_SIMULATION"), f"Expected simulated/executed, got {result2['status']}"
    print("  ✓ Case 2.2: OMS executed order smoothly after watchdog returned to HEALTHY.")


async def run_state_reconciliation_tests():
    print("\n" + "=" * 80)
    print("       3. TESTING STARTUP EXCHANGE STATE RECONCILIATION")
    print("=" * 80)

    # Create temporary SQLite audit database
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_file:
        tmp_db_path = tmp_file.name

    try:
        test_db = AuditDatabase(db_path=tmp_db_path)

        # Seed local DB with:
        # Trade 1: OPEN trade for KXBTC15M-EXP1 (already settled offline on exchange -> should close)
        # Trade 2: OPEN trade for KXBTC15M-EXP2 (still active on exchange -> should remain open)
        t1 = test_db.record_trade_open(
            ticker="KXBTC15M-EXP1",
            series="KXBTC15M",
            side="YES",
            entry_price=0.55,
            contracts=20,
            cost_dollars=11.0,
            fee_dollars=0.35,
            model_prob_entry=0.75,
            delta_at_entry=45.0,
            seconds_left_at_entry=600,
            rationale="Test seed trade 1"
        )
        t2 = test_db.record_trade_open(
            ticker="KXBTC15M-EXP2",
            series="KXBTC15M",
            side="YES",
            entry_price=0.60,
            contracts=15,
            cost_dollars=9.0,
            fee_dollars=0.25,
            model_prob_entry=0.80,
            delta_at_entry=60.0,
            seconds_left_at_entry=500,
            rationale="Test seed trade 2"
        )

        # Create Mock Reconciler subclass simulating Kalshi REST response
        class MockKalshiStateReconciler(KalshiStateReconciler):
            async def fetch_exchange_positions(self):
                return [
                    # EXP2 is still open with 15 contracts
                    {"ticker": "KXBTC15M-EXP2", "position": 15, "market_exposure": 9.0},
                    # EXP3 exists on exchange but was missing from local DB! (e.g. executed elsewhere)
                    {"ticker": "KXBTC15M-EXP3", "position": 25, "market_exposure": 12.5}
                ]

            async def fetch_resting_orders(self):
                return [
                    {"order_id": "ord_orphan_991", "ticker": "KXBTC15M-OLD", "status": "resting"}
                ]

            async def cancel_order(self, order_id: str):
                return True

        reconciler = MockKalshiStateReconciler(dry_run=False)
        report = await reconciler.reconcile_with_db(test_db, cancel_orphaned_orders=True)

        assert report["status"] == "SUCCESS"
        assert report["active_exchange_positions"] == 2
        assert report["local_db_open_trades"] == 2

        # 1. EXP1 should be closed
        assert len(report["reconciled_closed_in_db"]) == 1
        assert report["reconciled_closed_in_db"][0]["ticker"] == "KXBTC15M-EXP1"
        print("  ✓ Case 3.1: Offline settled contract KXBTC15M-EXP1 successfully closed in SQLite DB.")

        # 2. EXP3 should be synced into DB
        assert len(report["reconciled_opened_in_db"]) == 1
        assert report["reconciled_opened_in_db"][0]["ticker"] == "KXBTC15M-EXP3"
        print("  ✓ Case 3.2: Untracked exchange position KXBTC15M-EXP3 successfully synced into SQLite DB.")

        # 3. Orphaned order should be canceled
        assert len(report["resting_orders_canceled"]) == 1
        assert report["resting_orders_canceled"][0] == "ord_orphan_991"
        print("  ✓ Case 3.3: Orphaned resting order 'ord_orphan_991' successfully canceled on exchange.")

        # Verify DB final state
        with test_db._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM trades WHERE status = 'OPEN'")
            open_rows = [dict(r) for r in cursor.fetchall()]
            open_tickers = {r["ticker"] for r in open_rows}
            assert "KXBTC15M-EXP2" in open_tickers
            assert "KXBTC15M-EXP3" in open_tickers
            assert "KXBTC15M-EXP1" not in open_tickers

            cursor.execute("SELECT * FROM trades WHERE id = ?", (t1,))
            closed_r = dict(cursor.fetchone())
            assert closed_r["status"] == "CLOSED"
            assert closed_r["exit_reason"] == "EXCHANGE_RECONCILED_OFFLINE"
        print("  ✓ Case 3.4: SQLite audit integrity fully validated after reconciliation.")

    finally:
        if os.path.exists(tmp_db_path):
            os.remove(tmp_db_path)


async def main():
    run_watchdog_tests()
    await run_oms_watchdog_integration_tests()
    await run_state_reconciliation_tests()

    print("\n" + "=" * 80)
    print("      ALL PHASE 3 SAFETY & STATE RECONCILIATION TESTS PASSED!")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
