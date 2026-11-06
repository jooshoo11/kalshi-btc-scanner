"""
core/tests/test_bot_database.py — Test Harness for AuditDatabase.

Verifies:
  1. Trade open/close recording and retrieval
  2. Portfolio summary stats
  3. Open trade retrieval
  4. CSV export
  5. Schema migration (order_group_id column)
"""

import os
import sys
import time
import tempfile

import pytest


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture
def audit_db(db_path):
    from core.bot_database import AuditDatabase
    return AuditDatabase(db_path=db_path)


def test_record_and_retrieve_trade(audit_db):
    """Recording a trade open and close should persist and be retrievable."""
    trade_id = audit_db.record_trade_open(
        ticker="KXBTC15M-26OCT171515-15",
        series="KXBTC15M",
        side="YES",
        entry_price=0.44,
        contracts=16,
        cost_dollars=7.04,
        fee_dollars=0.05,
        model_prob_entry=65.0,
        delta_at_entry=15.0,
        seconds_left_at_entry=840,
        rationale="Test entry",
    )
    assert trade_id is not None
    assert trade_id > 0

    open_trades = audit_db.get_open_trades()
    assert len(open_trades) == 1
    assert open_trades[0]["ticker"] == "KXBTC15M-26OCT171515-15"
    assert open_trades[0]["side"] == "YES"
    assert open_trades[0]["status"] == "OPEN"

    # Close the trade
    audit_db.record_trade_close(
        trade_id=trade_id,
        exit_price=0.57,
        exit_reason="TAKE_PROFIT",
        pnl_dollars=2.08,
        pnl_pct=28.8,
    )

    open_trades = audit_db.get_open_trades()
    assert len(open_trades) == 0

    recent = audit_db.get_recent_trades(limit=10)
    assert len(recent) == 1
    assert recent[0]["status"] == "CLOSED"
    assert recent[0]["exit_reason"] == "TAKE_PROFIT"
    assert recent[0]["pnl_dollars"] == 2.08


def test_portfolio_summary_stats(audit_db):
    """Summary stats should aggregate across all closed trades."""
    # No trades -> zero stats
    stats = audit_db.get_portfolio_summary_stats()
    assert stats["total_trades"] == 0
    assert stats["winning_trades"] == 0
    assert stats["realized_pnl"] == 0.0

    # Add some trades
    for i in range(5):
        tid = audit_db.record_trade_open(
            ticker=f"KXBTC15M-test-{i}",
            series="KXBTC15M",
            side="YES",
            entry_price=0.50,
            contracts=10,
            cost_dollars=5.00,
            fee_dollars=0.04,
        )
        won = i % 2 == 0
        pnl = 2.0 if won else -1.5
        audit_db.record_trade_close(
            trade_id=tid,
            exit_price=0.60 if won else 0.40,
            exit_reason="TAKE_PROFIT" if won else "STOP_LOSS",
            pnl_dollars=pnl,
            pnl_pct=40.0 if won else -30.0,
        )

    stats = audit_db.get_portfolio_summary_stats()
    assert stats["total_trades"] == 5
    assert stats["winning_trades"] == 3
    assert stats["realized_pnl"] == 3.0  # 3*2.0 + 2*(-1.5) = 3.0


def test_csv_export(audit_db):
    """CSV export should include all trades with proper headers."""
    audit_db.record_trade_open(
        ticker="KXBTC15M-test",
        series="KXBTC15M",
        side="YES",
        entry_price=0.50,
        contracts=10,
        cost_dollars=5.00,
        fee_dollars=0.04,
    )
    csv_content = audit_db.export_trades_csv()
    assert "id,timestamp,ticker,series,side" in csv_content
    assert "KXBTC15M-test" in csv_content


def test_schema_migration_order_group_id(audit_db):
    """The trades table should have an order_group_id column after init."""
    import sqlite3
    with sqlite3.connect(audit_db.db_path) as conn:
        cursor = conn.cursor()
        cols = {row[1] for row in cursor.execute("PRAGMA table_info(trades)").fetchall()}
        assert "order_group_id" in cols, "order_group_id column missing after migration"
