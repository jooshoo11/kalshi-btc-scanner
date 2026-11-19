"""
test_deployment_pipeline.py — Unit Tests for Deployment CI/CD, Kill-Switch, Blue/Green, and Rollback.

Verifies:
1. Kill-Switch latching, file creation, daily stop loss (10%) tripping, and order rejection.
2. Blue/Green shadow fill comparison, slippage SLAs, and latency tracking.
3. Automated rollback to paper mode upon orphan order detection.
4. Pre-deploy regression and smoke test execution.
"""

import os
import json
import time
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from core.kill_switch import KillSwitchController
from core.blue_green_comparator import BlueGreenFillComparator, FillRecord
from core.portfolio_manager import PortfolioManager
from core.live_executor import KalshiOrderManagementSystem
from core.order_saga import OrderSaga, OrderSagaState


@pytest.fixture
def temp_kill_file(tmp_path):
    kill_path = str(tmp_path / "TEST_KILL")
    state_path = str(tmp_path / "TEST_STATE.json")
    with open(state_path, "w") as f:
        json.dump({"daily_loss_tripped": False}, f)
    yield kill_path, state_path
    for p in [kill_path, state_path]:
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass


@pytest.mark.asyncio
async def test_kill_switch_trip_and_reset(temp_kill_file):
    """Test kill switch manual tripping, file persistence, and reset."""
    kill_path, state_path = temp_kill_file
    cancel_mock = AsyncMock(return_value=True)
    alert_mock = AsyncMock()

    ks = KillSwitchController(
        kill_file_path=kill_path,
        state_file_path=state_path,
        max_daily_loss_pct=10.0,
        cancel_callback=cancel_mock,
        on_trip_callback=alert_mock
    )

    # Initial state: Armed
    tripped, reason = ks.is_tripped()
    assert not tripped
    assert not os.path.exists(kill_path)

    # Trip kill switch asynchronously
    await ks.trip_async("Emergency drawdown test")

    assert ks.is_tripped()[0] is True
    assert "drawdown test" in ks.is_tripped()[1]
    assert os.path.exists(kill_path)
    assert cancel_mock.await_count == 1
    assert alert_mock.await_count == 1

    # Verify status dictionary
    status = ks.get_status()
    assert status["is_tripped"] is True
    assert status["status"] == "TRIPPED"

    # Reset kill switch
    ok, msg = ks.reset()
    assert ok is True
    assert not os.path.exists(kill_path)
    assert ks.is_tripped()[0] is False


@pytest.mark.asyncio
async def test_portfolio_manager_rejects_on_kill_switch(temp_kill_file):
    """Test PortfolioManager rejects all trades when KillSwitch is tripped."""
    kill_path, state_path = temp_kill_file
    ks = KillSwitchController(kill_file_path=kill_path, state_file_path=state_path)
    ks.trip_sync("Circuit breaker tripped")

    pm = PortfolioManager(
        auth=None,
        rest_url="https://demo-api.kalshi.co",
        initial_balance=100.0,
        global_stop_loss_daily_pct=10.0
    )

    with patch("core.portfolio_manager.kill_switch", ks):
        result = await pm.evaluate_and_reserve_capital(
            asset="BTC",
            entry_price=0.50,
            fee_per_contract=0.0175
        )
        assert result["approved"] is False
        assert "kill switch" in result["reason"].lower() or "circuit breaker" in result["reason"].lower()


@pytest.mark.asyncio
async def test_portfolio_manager_trips_on_10_percent_drawdown(temp_kill_file):
    """Test PortfolioManager automatically trips when portfolio drops 10% from day start."""
    kill_path, state_path = temp_kill_file
    ks = KillSwitchController(kill_file_path=kill_path, state_file_path=state_path)

    pm = PortfolioManager(
        auth=None,
        rest_url="https://demo-api.kalshi.co",
        initial_balance=100.0,
        day_start_equity=100.0,
        global_stop_loss_daily_pct=10.0
    )

    # Drop balance to $89.00 (11% drawdown from $100.00 baseline)
    pm.total_cash = 89.0
    pm.total_equity = 89.0
    assert pm.get_daily_drawdown_pct() == 11.0

    with patch("core.portfolio_manager.kill_switch", ks):
        result = await pm.evaluate_and_reserve_capital(
            asset="ETH",
            entry_price=0.50,
            fee_per_contract=0.0175
        )
        assert result["approved"] is False
        assert "stop loss" in result["reason"].lower()
        # Verify kill switch also latched
        assert ks.is_tripped()[0] is True


def test_blue_green_fill_comparator(tmp_path):
    """Test Blue/Green dual fill tracking, slippage delta, and SLA breaches."""
    audit_file = str(tmp_path / "bg_audit.json")
    comparator = BlueGreenFillComparator(
        max_slippage_cents=2.0,
        max_latency_ms=500.0,
        audit_file=audit_file
    )

    t0 = time.time()

    # Case 1: Healthy execution within SLA
    # Paper filled @ 50c, Live filled @ 51c (+1.0c slippage, 120ms latency)
    comparator.record_blue_fill(
        signal_id="SIG_001",
        ticker="KXBTC15M-T70000",
        side="YES",
        action="BUY",
        contracts=10,
        price_cents=50.0,
        fee_dollars=0.175,
        timestamp=t0
    )
    res_1 = comparator.record_green_fill(
        signal_id="SIG_001",
        ticker="KXBTC15M-T70000",
        side="YES",
        action="BUY",
        contracts=10,
        price_cents=51.0,
        fee_dollars=0.175,
        timestamp=t0 + 0.120,
        latency_ms=120.0
    )

    assert res_1 is not None
    assert res_1["slippage_cents"] == 1.0
    assert res_1["latency_delta_ms"] == 120.0
    assert res_1["status"] == "OK"
    assert res_1["slippage_breach"] is False

    # Case 2: High slippage breach (> 2.0c)
    # Paper filled @ 50c, Live filled @ 53c (+3.0c slippage)
    comparator.record_blue_fill(
        signal_id="SIG_002",
        ticker="KXETH15M-T3500",
        side="YES",
        action="BUY",
        contracts=5,
        price_cents=50.0,
        fee_dollars=0.08,
        timestamp=t0 + 1.0
    )
    res_2 = comparator.record_green_fill(
        signal_id="SIG_002",
        ticker="KXETH15M-T3500",
        side="YES",
        action="BUY",
        contracts=5,
        price_cents=53.0,
        fee_dollars=0.08,
        timestamp=t0 + 1.250,
        latency_ms=250.0
    )

    assert res_2 is not None
    assert res_2["slippage_cents"] == 3.0
    assert res_2["slippage_breach"] is True
    assert res_2["status"] == "SLA_BREACH"

    # Verify Summary
    summary = comparator.get_summary()
    assert summary["total_paired"] == 2
    assert summary["slippage_breaches"] == 1
    assert summary["mean_slippage_cents"] == 2.0
    assert summary["sla_status"] == "DEGRADED"


@pytest.mark.asyncio
async def test_live_oms_rollback_on_orphaned_orders():
    """Test that live OMS auto-cancels and rolls back to paper mode when orphans occur."""
    mock_auth = MagicMock()
    mock_auth.is_configured = False
    mock_auth.key_id = "test-key"

    oms = KalshiOrderManagementSystem(
        auth=mock_auth,
        rest_url="https://demo-api.kalshi.co/trade-api/v2",
        ws_client=None,
        dry_run=False  # Simulate live armed mode
    )

    assert oms.dry_run is False

    # Create an orphaned saga resting > 120s
    old_time = time.time() - 150.0
    orphan_saga = OrderSaga(
        saga_id="saga_ghost_1",
        client_order_id="client_ghost_1",
        ticker="KXSOL15M-T200",
        side="YES",
        action="buy",
        target_contracts=5,
        price_cents=50,
        state=OrderSagaState.SUBMITTED,
        order_id="order_ghost_1",
        remaining_contracts=5,
        created_at=old_time
    )
    oms.saga_orchestrator.sagas["saga_ghost_1"] = orphan_saga

    # Mock cancel_order REST call
    oms._cancel_rest_order = AsyncMock(return_value=True)
    oms.cancel_all_orders = AsyncMock(return_value={"status": "CANCELLED_ALL_TEST"})

    # Trigger sweep with auto_rollback=True
    orphans = await oms.sweep_and_cancel_orphaned_orders(max_hold_seconds=120.0, auto_rollback=True)

    assert len(orphans) == 1
    assert orphans[0]["order_id"] == "order_ghost_1"

    # Confirm auto-rollback triggered: OMS reverted to paper mode!
    assert oms.dry_run is True
    assert oms.cancel_all_orders.await_count >= 1


@pytest.mark.asyncio
async def test_manual_rollback_trigger():
    """Test explicit manual rollback endpoint / function."""
    mock_auth = MagicMock()
    oms = KalshiOrderManagementSystem(
        auth=mock_auth,
        rest_url="https://demo-api.kalshi.co/trade-api/v2",
        ws_client=None,
        dry_run=False
    )
    oms.cancel_all_orders = AsyncMock(return_value={"status": "BATCH_CANCELLED"})

    res = await oms.trigger_rollback_to_paper(reason="Operator emergency rollback")
    assert res["status"] == "REVERTED_TO_PAPER"
    assert oms.dry_run is True
    assert oms.cancel_all_orders.await_count == 1
