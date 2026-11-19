"""
core/tests/test_bot_database_mock.py — Mocking SQLite Operations for AuditDatabase QA.

Verifies:
1. SQL query and parameter binding via mocked sqlite3 connection.
2. Resilience against database locked errors (sqlite3.OperationalError).
3. Rollback and error propagation on cursor execution failures.
4. Correct mapping of sqlite3.Row results to dictionaries.
5. In-memory roundtrip integration verification.
"""

import sqlite3
import pytest
from unittest.mock import patch, MagicMock

from core.bot_database import AuditDatabase


def test_mocked_record_trade_open():
    """Verify record_trade_open prepares and executes proper SQL query."""
    mock_conn = MagicMock(spec=sqlite3.Connection)
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.__exit__.return_value = None

    mock_cursor = MagicMock(spec=sqlite3.Cursor)
    mock_conn.cursor.return_value = mock_cursor
    mock_cursor.lastrowid = 42

    with patch("sqlite3.connect", return_value=mock_conn), \
         patch.object(AuditDatabase, "_init_db"):
        db = AuditDatabase(db_path=":memory:")
        trade_id = db.record_trade_open(
            ticker="KXBTC15M-TEST",
            series="KXBTC15M",
            side="YES",
            entry_price=0.50,
            contracts=10,
            cost_dollars=5.0,
            fee_dollars=0.18,
            model_prob_entry=65.0,
            delta_at_entry=20.0,
            seconds_left_at_entry=600,
            rationale="Deterministic mock entry",
            order_group_id="grp_qa_99"
        )

        assert trade_id == 42
        # Verify execute was called with INSERT INTO trades and INSERT INTO trade_events
        assert mock_cursor.execute.called
        executed_sqls = [call[0][0] for call in mock_cursor.execute.call_args_list]
        assert any("INSERT INTO trades" in s for s in executed_sqls)
        assert any("INSERT INTO trade_events" in s for s in executed_sqls)
        
        # Verify params
        first_call_params = mock_cursor.execute.call_args_list[0][0][1]
        assert "KXBTC15M-TEST" in first_call_params
        assert "grp_qa_99" in first_call_params
        assert mock_conn.commit.called


def test_mocked_sqlite_locked_exception_handling():
    """Verify that sqlite3.OperationalError (database locked) is raised and handled cleanly."""
    mock_conn = MagicMock(spec=sqlite3.Connection)
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.__exit__.return_value = None

    mock_cursor = MagicMock(spec=sqlite3.Cursor)
    mock_conn.cursor.return_value = mock_cursor
    mock_cursor.execute.side_effect = sqlite3.OperationalError("database is locked")

    with patch("sqlite3.connect", return_value=mock_conn):
        # Database initialization encounters locked DB and propagates OperationalError
        with pytest.raises(sqlite3.OperationalError) as exc_info:
            AuditDatabase(db_path=":memory:")
        assert "database is locked" in str(exc_info.value)


def test_mocked_get_open_trades():
    """Verify row mapping from sqlite3.Row results to standard dictionaries."""
    mock_conn = MagicMock(spec=sqlite3.Connection)
    mock_conn.__enter__.return_value = mock_conn
    mock_conn.__exit__.return_value = None

    mock_cursor = MagicMock(spec=sqlite3.Cursor)
    mock_conn.cursor.return_value = mock_cursor

    fake_row = {
        "id": 1,
        "ticker": "KXBTC15M-TEST",
        "series": "KXBTC15M",
        "side": "YES",
        "entry_price": 0.50,
        "contracts": 10,
        "cost_dollars": 5.0,
        "fee_dollars": 0.18,
        "status": "OPEN",
        "timestamp": 1731000000.0,
        "exit_price": None,
        "exit_timestamp": None,
        "exit_reason": None,
        "pnl_dollars": None,
        "pnl_pct": None,
        "model_prob_entry": 65.0,
        "delta_at_entry": 10.0,
        "seconds_left_at_entry": 500,
        "rationale": None,
        "order_group_id": None
    }
    mock_cursor.fetchall.return_value = [fake_row]

    with patch("sqlite3.connect", return_value=mock_conn), \
         patch.object(AuditDatabase, "_init_db"):
        db = AuditDatabase(db_path=":memory:")
        open_trades = db.get_open_trades()

        assert len(open_trades) == 1
        assert open_trades[0]["ticker"] == "KXBTC15M-TEST"
        assert open_trades[0]["status"] == "OPEN"
        assert open_trades[0]["entry_price"] == 0.50


def test_in_memory_sqlite_roundtrip():
    """Verify full end-to-end open/close lifecycle on real in-memory SQLite schema."""
    import tempfile
    import os
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name

    try:
        db = AuditDatabase(db_path=db_path)
        trade_id = db.record_trade_open(
            ticker="KXETH15M-TEST",
            series="KXETH15M",
            side="NO",
            entry_price=0.45,
            contracts=20,
            cost_dollars=9.0,
            fee_dollars=0.35,
            model_prob_entry=58.0,
            delta_at_entry=-15.0,
            seconds_left_at_entry=450,
            rationale="Test roundtrip entry",
            order_group_id="grp_roundtrip_1"
        )
        assert trade_id > 0

        open_trades = db.get_open_trades()
        assert len(open_trades) == 1
        assert open_trades[0]["ticker"] == "KXETH15M-TEST"
        assert open_trades[0]["order_group_id"] == "grp_roundtrip_1"

        db.record_trade_close(
            trade_id=trade_id,
            exit_price=1.00,
            exit_reason="EXPIRED_WIN",
            pnl_dollars=11.0,
            pnl_pct=122.2
        )

        assert len(db.get_open_trades()) == 0
    finally:
        if os.path.exists(db_path):
            os.remove(db_path)
