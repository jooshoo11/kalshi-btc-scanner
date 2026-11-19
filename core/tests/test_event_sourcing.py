"""
core/tests/test_event_sourcing.py — Event Sourcing, CQRS Projections & Event Replay Verification.

Verifies:
1. Immutable append-only event store (trade_events table: ORDER_PLACED, ORDER_FILLED, ORDER_CANCELLED, POSITION_CLOSED).
2. CQRS separation: write model (trade_events) separate from read model (trades projection).
3. Complete projection rebuilding from scratch by replaying the event log.
4. Point-in-time temporal replay and rebuilding portfolio_state.json.
5. Event versioning and schema evolution: upcasting v1 legacy events to v2 multi-coin events (SOL, XRP, ETH, BTC).
"""

import os
import json
import time
import tempfile
import pytest

from core.bot_database import (
    AuditDatabase,
    TradeEventType,
    TradeEvent,
    upcast_event
)


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = AuditDatabase(db_path=path)
    yield db
    db.close()
    if os.path.exists(path):
        os.remove(path)


def test_immutable_event_store_append_only(temp_db):
    """Verify that trade lifecycle appends events without UPDATE or DELETE on trade_events."""
    trade_id = temp_db.record_trade_open(
        ticker="KXBTC15M-SAMPLE",
        series="KXBTC15M",
        side="YES",
        entry_price=0.50,
        contracts=10,
        cost_dollars=5.0,
        fee_dollars=0.18,
        model_prob_entry=65.0,
        delta_at_entry=10.0,
        seconds_left_at_entry=600,
        rationale="Event sourcing open"
    )

    events = temp_db.get_events_for_aggregate(str(trade_id))
    assert len(events) == 2
    assert events[0].event_type == TradeEventType.ORDER_PLACED
    assert events[1].event_type == TradeEventType.ORDER_FILLED
    assert events[0].payload["contracts"] == 10
    assert events[0].payload["coin"] == "BTC"

    # Close position
    temp_db.record_trade_close(
        trade_id=trade_id,
        exit_price=0.75,
        exit_reason="TAKE_PROFIT",
        pnl_dollars=2.50,
        pnl_pct=50.0
    )

    events_after = temp_db.get_events_for_aggregate(str(trade_id))
    assert len(events_after) == 3
    assert events_after[2].event_type == TradeEventType.POSITION_CLOSED
    assert events_after[2].payload["pnl_dollars"] == 2.50

    # Verify event store row count directly
    with temp_db._get_conn() as conn:
        cursor = conn.cursor()
        count = cursor.execute("SELECT COUNT(*) FROM trade_events").fetchone()[0]
        assert count == 3


def test_cqrs_projections_rebuild(temp_db):
    """Verify that read model projections can be completely wiped and restored from the event log."""
    # Open 3 trades across BTC, ETH, SOL
    t1 = temp_db.record_trade_open(
        ticker="KXBTC15M-1", series="KXBTC15M", side="YES", entry_price=0.50,
        contracts=10, cost_dollars=5.0, fee_dollars=0.18, coin="BTC"
    )
    t2 = temp_db.record_trade_open(
        ticker="KXETH15M-2", series="KXETH15M", side="NO", entry_price=0.45,
        contracts=20, cost_dollars=9.0, fee_dollars=0.35, coin="ETH"
    )
    t3 = temp_db.record_trade_open(
        ticker="KXSOL15M-3", series="KXSOL15M", side="YES", entry_price=0.60,
        contracts=5, cost_dollars=3.0, fee_dollars=0.10, coin="SOL"
    )

    # Close t1 as WIN, t2 as LOSS, leave t3 OPEN
    temp_db.record_trade_close(trade_id=t1, exit_price=1.00, exit_reason="WIN", pnl_dollars=5.0, pnl_pct=100.0)
    temp_db.record_trade_close(trade_id=t2, exit_price=0.00, exit_reason="LOSS", pnl_dollars=-9.0, pnl_pct=-100.0)

    # Wipe the read model projection table
    with temp_db._get_conn() as conn:
        conn.cursor().execute("DELETE FROM trades")
        conn.commit()

    assert len(temp_db.get_open_trades()) == 0
    assert len(temp_db.get_recent_trades()) == 0

    # Rebuild read model from immutable event store
    temp_db.rebuild_projections()

    open_trades = temp_db.get_open_trades()
    assert len(open_trades) == 1
    assert open_trades[0]["ticker"] == "KXSOL15M-3"
    assert open_trades[0]["status"] == "OPEN"

    recent = temp_db.get_recent_trades(limit=10)
    assert len(recent) == 3
    stats = temp_db.get_portfolio_summary_stats()
    assert stats["total_trades"] == 2
    assert stats["winning_trades"] == 1
    assert stats["realized_pnl"] == -4.0


def test_event_replay_portfolio_state_rebuild(temp_db, tmp_path):
    """Verify event replay reconstructs portfolio_state.json with point-in-time time travel."""
    base_time = 1730000000.0

    # Step 1: Place BTC order at T=0
    t1 = temp_db.record_trade_open(
        ticker="KXBTC15M-A", series="KXBTC15M", side="YES", entry_price=0.50,
        contracts=10, cost_dollars=5.0, fee_dollars=0.18, timestamp=base_time
    )

    # Step 2: Cancelled order at T=10
    temp_db.append_event(
        aggregate_id="ord_cancelled_9",
        event_type=TradeEventType.ORDER_PLACED,
        payload={"trade_id": "99", "ticker": "KXXRP15M-X", "coin": "XRP", "cost_dollars": 4.0, "fee_dollars": 0.15},
        timestamp=base_time + 10.0
    )
    temp_db.append_event(
        aggregate_id="ord_cancelled_9",
        event_type=TradeEventType.ORDER_CANCELLED,
        payload={"trade_id": "99", "reason": "UNFILLED"},
        timestamp=base_time + 15.0
    )

    # Step 3: Close BTC order with +$4.00 profit at T=30
    temp_db.record_trade_close(
        trade_id=t1, exit_price=0.90, exit_reason="TAKE_PROFIT",
        pnl_dollars=4.00, pnl_pct=80.0, exit_timestamp=base_time + 30.0
    )

    # Test point-in-time replay at T=20 (before position was closed)
    state_at_t20 = temp_db.replay_portfolio_state(day_start_equity=100.0, to_timestamp=base_time + 20.0)
    assert state_at_t20["open_positions_count"] == 1
    assert str(t1) in state_at_t20["open_positions"]
    assert state_at_t20["realized_pnl"] == 0.0

    # Test complete replay at T=40
    final_state = temp_db.replay_portfolio_state(day_start_equity=100.0, to_timestamp=base_time + 40.0)
    assert final_state["open_positions_count"] == 0
    assert final_state["realized_pnl"] == 4.00
    assert final_state["current_cash"] == 103.82  # 100 - (5.0 + 0.18) + (5.0 + 4.0)
    assert final_state["total_trades"] == 1
    assert final_state["win_rate_pct"] == 100.0

    # Test rebuilding portfolio_state.json file
    out_file = str(tmp_path / "rebuilt_portfolio_state.json")
    saved = temp_db.rebuild_portfolio_state_file(day_start_equity=100.0, output_file=out_file)
    assert os.path.exists(out_file)
    with open(out_file, "r") as f:
        loaded = json.load(f)
    assert loaded["realized_pnl"] == 4.00
    assert loaded["current_equity"] == saved["current_equity"]


def test_event_versioning_and_upcasting(temp_db):
    """Verify v1 legacy events without coin field are upcast to v2 multi-coin schema."""
    # Directly inject v1 event without coin, settlement_index, or price_precision
    v1_payload = {
        "trade_id": 101,
        "ticker": "KXXRP15M-SAMPLE",
        "series": "KXXRP15M",
        "side": "YES",
        "entry_price": 0.5850,
        "contracts": 50,
        "cost_dollars": 29.25,
        "fee_dollars": 0.85
    }

    temp_db.append_event(
        aggregate_id="101",
        event_type=TradeEventType.ORDER_PLACED,
        payload=v1_payload,
        event_version=1  # legacy v1
    )

    # Fetch and verify upcasting
    events = temp_db.get_events_for_aggregate("101")
    assert len(events) == 1
    ev = events[0]
    assert ev.event_version == 2
    assert ev.payload["coin"] == "XRP"
    assert ev.payload["settlement_index"] == "XRPUSD_RTI"
    assert ev.payload["price_precision"] == 4

    # Test SOL upcasting
    ev_sol_raw = TradeEvent(
        event_id=1,
        aggregate_id="102",
        event_type=TradeEventType.ORDER_PLACED,
        event_version=1,
        timestamp=time.time(),
        payload={"ticker": "KXSOL15M-TEST", "series": "KXSOL15M"}
    )
    upcast_sol = upcast_event(ev_sol_raw)
    assert upcast_sol.payload["coin"] == "SOL"
    assert upcast_sol.payload["settlement_index"] == "SOLUSD_RTI"
    assert upcast_sol.payload["price_precision"] == 2


def test_database_immutability_triggers(temp_db):
    """Verify database-level SQLite triggers prevent any UPDATE or DELETE on trade_events."""
    import sqlite3
    temp_db.append_event(
        aggregate_id="test_agg",
        event_type=TradeEventType.ORDER_PLACED,
        payload={"ticker": "KXBTC15M-1", "contracts": 10}
    )

    with temp_db._get_conn() as conn:
        cursor = conn.cursor()

        # Attempting UPDATE must fail with immutable trigger error
        with pytest.raises(sqlite3.DatabaseError, match="immutable append-only event store: UPDATE prohibited"):
            cursor.execute("UPDATE trade_events SET payload_json = '{}' WHERE aggregate_id = 'test_agg'")

        # Attempting DELETE must fail with immutable trigger error
        with pytest.raises(sqlite3.DatabaseError, match="immutable append-only event store: DELETE prohibited"):
            cursor.execute("DELETE FROM trade_events WHERE aggregate_id = 'test_agg'")
