"""
core/bot_database.py — SQLite Trade & Portfolio Audit Persistence.

Single source of truth for all trade ledger, portfolio snapshot, and metrics
audit records across the multi-coin engine. Used by PaperTradingBot,
MultiCoinPaperEngine, and the KalshiStateReconciler.
"""

import sqlite3
import time
import os
import csv
import io
from typing import Dict, Any, List, Optional


class AuditDatabase:
    """
    SQLite-backed audit ledger for Kalshi paper trading.

    Tables:
      - trades: one row per trade open/close event
      - portfolio_snapshots: periodic equity snapshots
      - metrics_snapshots: periodic market metrics snapshots
    """

    def __init__(self, db_path: str = "kalshi_audit.db"):
        self.db_path = db_path
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            cursor = conn.cursor()
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
                    status TEXT NOT NULL DEFAULT 'OPEN',
                    model_prob_entry REAL,
                    delta_at_entry REAL,
                    seconds_left_at_entry INTEGER,
                    rationale TEXT,
                    order_group_id TEXT
                )
            """)
            # Schema migration: add order_group_id if missing (older DBs)
            cols = {row[1] for row in cursor.execute("PRAGMA table_info(trades)").fetchall()}
            if "order_group_id" not in cols:
                cursor.execute("ALTER TABLE trades ADD COLUMN order_group_id TEXT")
                print("[AuditDatabase] Migrated trades table: added order_group_id column")

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    cash REAL NOT NULL,
                    pos_val REAL NOT NULL,
                    equity REAL NOT NULL,
                    realized REAL NOT NULL,
                    unrealized REAL NOT NULL,
                    win_rate REAL NOT NULL,
                    trades_count INTEGER NOT NULL
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS metrics_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    spot_price REAL NOT NULL,
                    candle_open REAL NOT NULL,
                    delta REAL NOT NULL,
                    vol_24h_ann_pct REAL NOT NULL,
                    rsi_14 REAL NOT NULL,
                    funding_rate_ann_pct REAL NOT NULL,
                    prob_15m REAL NOT NULL
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
        model_prob_entry: float = 50.0,
        delta_at_entry: float = 0.0,
        seconds_left_at_entry: int = 0,
        rationale: str = "",
        order_group_id: Optional[str] = None,
        timestamp: Optional[float] = None,
    ) -> int:
        """Insert a new OPEN trade row and return its rowid."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO trades (
                    timestamp, ticker, series, side, entry_price, contracts,
                    cost_dollars, fee_dollars, status, model_prob_entry,
                    delta_at_entry, seconds_left_at_entry, rationale, order_group_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?)
            """, (
                timestamp or time.time(),
                ticker,
                series,
                side,
                entry_price,
                contracts,
                cost_dollars,
                fee_dollars,
                model_prob_entry,
                delta_at_entry,
                seconds_left_at_entry,
                rationale,
                order_group_id,
            ))
            conn.commit()
            return cursor.lastrowid

    def record_trade_close(
        self,
        trade_id: int,
        exit_price: float,
        exit_reason: str,
        pnl_dollars: float,
        pnl_pct: float,
        exit_timestamp: Optional[float] = None,
        rationale: Optional[str] = None,
    ):
        """Close an existing OPEN trade row."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE trades SET
                    exit_price = ?,
                    exit_timestamp = ?,
                    exit_reason = ?,
                    pnl_dollars = ?,
                    pnl_pct = ?,
                    status = 'CLOSED'
                WHERE id = ?
            """, (
                exit_price,
                exit_timestamp or time.time(),
                exit_reason,
                pnl_dollars,
                pnl_pct,
                trade_id,
            ))
            conn.commit()

    def get_open_trades(self) -> List[Dict[str, Any]]:
        """Return all OPEN trades ordered by id."""
        with self._get_conn() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM trades WHERE status = 'OPEN' ORDER BY id")
            return [dict(row) for row in cursor.fetchall()]

    def get_recent_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return the most recent trades (open or closed) ordered by id desc."""
        with self._get_conn() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def get_portfolio_summary_stats(self) -> Dict[str, Any]:
        """Aggregate stats across all CLOSED trades."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    COUNT(*) AS total_trades,
                    SUM(CASE WHEN pnl_dollars > 0 THEN 1 ELSE 0 END) AS winning_trades,
                    COALESCE(SUM(pnl_dollars), 0.0) AS realized_pnl
                FROM trades
                WHERE status = 'CLOSED' AND pnl_dollars IS NOT NULL
            """)
            row = cursor.fetchone()
            total = row[0] or 0
            wins = row[1] or 0
            pnl = row[2] or 0.0
            return {
                "total_trades": total,
                "winning_trades": wins,
                "realized_pnl": round(pnl, 2),
                "win_rate_pct": round((wins / total * 100.0) if total > 0 else 0.0, 1),
            }

    def log_portfolio_snapshot(
        self,
        cash: float,
        pos_val: float,
        equity: float,
        realized: float,
        unrealized: float,
        win_rate: float,
        trades_count: int,
    ):
        """Persist a portfolio equity snapshot."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO portfolio_snapshots (
                    timestamp, cash, pos_val, equity, realized, unrealized,
                    win_rate, trades_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                time.time(),
                cash,
                pos_val,
                equity,
                realized,
                unrealized,
                win_rate,
                trades_count,
            ))
            conn.commit()

    def log_metrics_snapshot(
        self,
        spot: float,
        open_p: float,
        delta: float,
        vol_24h: float,
        rsi: float,
        funding: float,
        prob_15m: float,
    ):
        """Persist a market metrics snapshot."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO metrics_snapshots (
                    timestamp, spot_price, candle_open, delta, vol_24h_ann_pct,
                    rsi_14, funding_rate_ann_pct, prob_15m
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                time.time(),
                spot,
                open_p,
                delta,
                vol_24h,
                rsi,
                funding,
                prob_15m,
            ))
            conn.commit()

    def get_portfolio_history(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return recent portfolio snapshots ordered by timestamp desc."""
        with self._get_conn() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM portfolio_snapshots ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]

    def export_trades_csv(self) -> str:
        """Export all trades as a CSV string."""
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "id", "timestamp", "ticker", "series", "side", "entry_price",
            "contracts", "cost_dollars", "fee_dollars", "exit_price",
            "exit_timestamp", "exit_reason", "pnl_dollars", "pnl_pct",
            "status", "model_prob_entry", "delta_at_entry",
            "seconds_left_at_entry", "rationale", "order_group_id",
        ])
        with self._get_conn() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM trades ORDER BY id")
            for row in cursor.fetchall():
                writer.writerow([row[col] for row in [row] for col in row.keys()])
        return output.getvalue()
