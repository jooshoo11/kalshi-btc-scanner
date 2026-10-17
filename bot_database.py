import sqlite3
import os
import time
from typing import Dict, Any, List, Optional
import csv
import io

DB_PATH = os.path.join(os.path.dirname(__file__), "kalshi_audit.db")

class AuditDatabase:
    """
    Persistent SQLite storage for all trades, predictions, portfolio equity,
    and market metrics snapshots for complete auditability.
    """
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_tables()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self):
        with self._get_conn() as conn:
            cursor = conn.cursor()
            # 1. Trades log
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    ticker TEXT NOT NULL,
                    series TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_price REAL NOT NULL,
                    contracts INTEGER NOT NULL,
                    cost_dollars REAL NOT NULL,
                    fee_dollars REAL NOT NULL,
                    exit_price REAL,
                    exit_timestamp REAL,
                    exit_reason TEXT,
                    pnl_dollars REAL,
                    pnl_pct REAL,
                    status TEXT NOT NULL,
                    model_prob_entry REAL,
                    delta_at_entry REAL,
                    seconds_left_at_entry INTEGER,
                    rationale TEXT
                )
            """)

            # 2. Metrics audit snapshot log
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS metrics_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    spot_price REAL NOT NULL,
                    candle_open REAL,
                    delta REAL,
                    vol_24h_ann REAL,
                    rsi_14 REAL,
                    funding_rate_ann REAL,
                    model_prob_15m REAL
                )
            """)

            # 3. Portfolio equity history log
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    cash REAL NOT NULL,
                    position_value REAL NOT NULL,
                    total_equity REAL NOT NULL,
                    realized_pnl REAL NOT NULL,
                    unrealized_pnl REAL NOT NULL,
                    win_rate_pct REAL NOT NULL,
                    total_trades_count INTEGER NOT NULL
                )
            """)
            conn.commit()

    def record_trade_open(
        self,
        ticker: str,
        series: str,
        side: str,
        entry_price: float,
        contracts: int,
        cost_dollars: float,
        fee_dollars: float,
        model_prob_entry: float,
        delta_at_entry: float,
        seconds_left_at_entry: int,
        rationale: str
    ) -> int:
        """Insert newly opened position and return trade ID."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO trades (
                    timestamp, ticker, series, side, entry_price, contracts,
                    cost_dollars, fee_dollars, status, model_prob_entry,
                    delta_at_entry, seconds_left_at_entry, rationale
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?)
            """, (
                time.time(), ticker, series, side, entry_price, contracts,
                cost_dollars, fee_dollars, model_prob_entry,
                delta_at_entry, seconds_left_at_entry, rationale
            ))
            conn.commit()
            return cursor.lastrowid

    def record_trade_close(
        self,
        trade_id: int,
        exit_price: float,
        exit_reason: str,
        pnl_dollars: float,
        pnl_pct: float
    ):
        """Update closed trade record with final exit metrics."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE trades
                SET exit_price = ?,
                    exit_timestamp = ?,
                    exit_reason = ?,
                    pnl_dollars = ?,
                    pnl_pct = ?,
                    status = 'CLOSED'
                WHERE id = ?
            """, (exit_price, time.time(), exit_reason, pnl_dollars, pnl_pct, trade_id))
            conn.commit()

    def log_metrics_snapshot(
        self,
        spot: float,
        open_p: float,
        delta: float,
        vol_24h: float,
        rsi: float,
        funding: float,
        prob_15m: float
    ):
        """Append periodic metrics snapshot."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO metrics_snapshots (
                    timestamp, spot_price, candle_open, delta,
                    vol_24h_ann, rsi_14, funding_rate_ann, model_prob_15m
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (time.time(), spot, open_p, delta, vol_24h, rsi, funding, prob_15m))
            conn.commit()

    def log_portfolio_snapshot(
        self,
        cash: float,
        pos_val: float,
        equity: float,
        realized: float,
        unrealized: float,
        win_rate: float,
        trades_count: int
    ):
        """Append portfolio balance point."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO portfolio_snapshots (
                    timestamp, cash, position_value, total_equity,
                    realized_pnl, unrealized_pnl, win_rate_pct, total_trades_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (time.time(), cash, pos_val, equity, realized, unrealized, win_rate, trades_count))
            conn.commit()

    def get_recent_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Fetch list of recent trades for UI and audit review."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM trades
                ORDER BY id DESC
                LIMIT ?
            """, (limit,))
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def get_portfolio_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Fetch historical equity curve."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM portfolio_snapshots
                ORDER BY id DESC
                LIMIT ?
            """, (limit,))
            rows = cursor.fetchall()
            return [dict(r) for r in rows][::-1]

    def export_trades_csv(self) -> str:
        """Export full trades audit log as CSV string."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM trades ORDER BY id ASC")
            rows = cursor.fetchall()
            if not rows:
                return "id,timestamp,ticker,side,entry_price,contracts,cost_dollars,exit_price,exit_reason,pnl_dollars,pnl_pct,status,rationale\n"

            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(rows[0].keys())
            for r in rows:
                writer.writerow(list(r))
            return output.getvalue()
