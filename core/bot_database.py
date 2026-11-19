"""
core/bot_database.py — Event-Sourced SQLite Trade & Portfolio Audit Persistence.

Implements CQRS (Command Query Responsibility Segregation) & Event Sourcing:
1. Immutable Event Store (Write Model): Append-only `trade_events` table (no UPDATE/DELETE).
   Event Types: ORDER_PLACED, ORDER_FILLED, ORDER_CANCELLED, POSITION_CLOSED.
2. Read Projections (Read Model): Materialized `trades` table for fast O(1) queries
   (get_open_trades, get_recent_trades, portfolio summary stats).
3. Event Replay & Time Travel: Rebuilds portfolio_state.json from event log at any point in time.
4. Schema Evolution & Upcasting: Versioned events (v1 legacy BTC -> v2 multi-coin SOL/XRP).
"""

import csv
import io
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, Any, List, Optional, Tuple

from core.db_pool import get_db_pool, SQLiteConnectionPool, PooledConnection

logger = logging.getLogger("AuditDatabase")


class TradeEventType(str, Enum):
    """Immutable Trade Event Types."""
    ORDER_PLACED = "ORDER_PLACED"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    POSITION_CLOSED = "POSITION_CLOSED"
    OMS_ROLLBACK = "OMS_ROLLBACK"
    SYSTEM_ALERT = "SYSTEM_ALERT"


@dataclass
class TradeEvent:
    """Represents an immutable domain event in the event store."""
    event_id: Optional[int]
    aggregate_id: str
    event_type: TradeEventType
    event_version: int
    timestamp: float
    payload: Dict[str, Any]


def upcast_event(event: TradeEvent) -> TradeEvent:
    """
    Event Upcaster for schema evolution:
    Converts legacy v1 events (implicit BTC) into v2 multi-coin events (SOL, XRP, ETH, BTC).
    """
    if event.event_version >= 2:
        return event

    payload = dict(event.payload)
    ticker = str(payload.get("ticker", "")).upper()
    series = str(payload.get("series", "")).upper()

    # Determine coin and settlement index
    if "SOL" in series or "SOL" in ticker:
        coin = "SOL"
        settlement_index = "SOLUSD_RTI"
        price_precision = 2
    elif "XRP" in series or "XRP" in ticker:
        coin = "XRP"
        settlement_index = "XRPUSD_RTI"
        price_precision = 4
    elif "ETH" in series or "ETH" in ticker:
        coin = "ETH"
        settlement_index = "ETHUSD_RTI"
        price_precision = 2
    else:
        coin = "BTC"
        settlement_index = "BRTI"
        price_precision = 2

    payload.setdefault("coin", coin)
    payload.setdefault("settlement_index", settlement_index)
    payload.setdefault("price_precision", price_precision)

    return TradeEvent(
        event_id=event.event_id,
        aggregate_id=event.aggregate_id,
        event_type=event.event_type,
        event_version=2,
        timestamp=event.timestamp,
        payload=payload
    )


class AuditDatabase:
    """
    Event-Sourced SQLite audit ledger for Kalshi trading with CQRS read projections.
    
    Tables:
      - trade_events: Append-only immutable event store (Write Model).
      - trades: Materialized read projection (Read Model).
      - portfolio_snapshots: Periodic equity snapshots.
      - metrics_snapshots: Periodic market metrics snapshots.
    """

    def __init__(self, db_path: str = "kalshi_audit.db", pool_size: int = 10):
        self.db_path = db_path
        self.pool: SQLiteConnectionPool = get_db_pool(db_path, max_connections=pool_size)
        self._init_db()

    def _get_conn(self) -> PooledConnection:
        """Acquire a pooled connection with WAL mode and 5000ms busy_timeout."""
        if hasattr(self, "pool") and (self.pool is None or getattr(self.pool, "_closed", False)):
            self.pool = get_db_pool(self.db_path)
        return self.pool.get_connection()

    def _init_db(self):
        with self._get_conn() as conn:
            cursor = conn.cursor()

            # 1. Append-Only Immutable Event Store (Write Model)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trade_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    aggregate_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_version INTEGER NOT NULL DEFAULT 2,
                    timestamp REAL NOT NULL,
                    payload_json TEXT NOT NULL
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trade_events_agg ON trade_events(aggregate_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trade_events_type ON trade_events(event_type)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trade_events_ts ON trade_events(timestamp)")

            # Immutability Guarantees: DB-level triggers rejecting any UPDATE or DELETE on trade_events
            cursor.execute("""
                CREATE TRIGGER IF NOT EXISTS prevent_trade_events_update
                BEFORE UPDATE ON trade_events
                BEGIN
                    SELECT RAISE(ABORT, 'trade_events is an immutable append-only event store: UPDATE prohibited');
                END;
            """)
            cursor.execute("""
                CREATE TRIGGER IF NOT EXISTS prevent_trade_events_delete
                BEFORE DELETE ON trade_events
                BEGIN
                    SELECT RAISE(ABORT, 'trade_events is an immutable append-only event store: DELETE prohibited');
                END;
            """)

            # 2. Materialized Trades Projection (Read Model)
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
                    order_group_id TEXT,
                    order_id TEXT
                )
            """)
            # Schema migration for existing legacy databases
            cols = {row[1] for row in cursor.execute("PRAGMA table_info(trades)").fetchall()}
            if "order_group_id" not in cols:
                cursor.execute("ALTER TABLE trades ADD COLUMN order_group_id TEXT")
                print("[AuditDatabase] Migrated trades table: added order_group_id column")
            if "order_id" not in cols:
                cursor.execute("ALTER TABLE trades ADD COLUMN order_id TEXT")
                print("[AuditDatabase] Migrated trades table: added order_id column")

            # High-throughput B-tree indexes for fast HFT queries
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_series ON trades(series)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_order_group ON trades(order_group_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_order_id ON trades(order_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_series_ticker ON trades(series, ticker)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_status_series ON trades(status, series, timestamp)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_series_ts ON trades(series, timestamp)")

            # 3. Snapshots tables with timestamp indexes
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
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_ts ON portfolio_snapshots(timestamp)")

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
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_metrics_snapshots_ts ON metrics_snapshots(timestamp)")
            conn.commit()

    # =========================================================================
    # 1. EVENT STORE (WRITE MODEL) — APPEND-ONLY
    # =========================================================================

    def append_event(
        self,
        aggregate_id: str,
        event_type: TradeEventType,
        payload: Dict[str, Any],
        event_version: int = 2,
        timestamp: Optional[float] = None
    ) -> int:
        """
        Append an immutable event to trade_events.
        Strictly append-only (no UPDATE / DELETE queries).
        """
        ts = timestamp or time.time()
        payload_json = json.dumps(payload)

        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO trade_events (
                    aggregate_id, event_type, event_version, timestamp, payload_json
                ) VALUES (?, ?, ?, ?, ?)
            """, (
                str(aggregate_id),
                event_type.value if isinstance(event_type, TradeEventType) else str(event_type),
                event_version,
                ts,
                payload_json
            ))
            conn.commit()
            return cursor.lastrowid

    def get_events_for_aggregate(self, aggregate_id: str) -> List[TradeEvent]:
        """Fetch all events for a specific aggregate, applying schema upcasters."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT event_id, aggregate_id, event_type, event_version, timestamp, payload_json "
                "FROM trade_events WHERE aggregate_id = ? ORDER BY event_id ASC",
                (str(aggregate_id),)
            )
            events = []
            for row in cursor.fetchall():
                ev = TradeEvent(
                    event_id=row["event_id"],
                    aggregate_id=row["aggregate_id"],
                    event_type=TradeEventType(row["event_type"]),
                    event_version=row["event_version"],
                    timestamp=row["timestamp"],
                    payload=json.loads(row["payload_json"])
                )
                events.append(upcast_event(ev))
            return events

    def get_all_events(self, to_timestamp: Optional[float] = None) -> List[TradeEvent]:
        """Fetch all historical events up to a given point in time."""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            if to_timestamp is not None:
                cursor.execute(
                    "SELECT event_id, aggregate_id, event_type, event_version, timestamp, payload_json "
                    "FROM trade_events WHERE timestamp <= ? ORDER BY event_id ASC",
                    (to_timestamp,)
                )
            else:
                cursor.execute(
                    "SELECT event_id, aggregate_id, event_type, event_version, timestamp, payload_json "
                    "FROM trade_events ORDER BY event_id ASC"
                )
            events = []
            for row in cursor.fetchall():
                ev = TradeEvent(
                    event_id=row["event_id"],
                    aggregate_id=row["aggregate_id"],
                    event_type=TradeEventType(row["event_type"]),
                    event_version=row["event_version"],
                    timestamp=row["timestamp"],
                    payload=json.loads(row["payload_json"])
                )
                events.append(upcast_event(ev))
            return events

    # =========================================================================
    # 2. CQRS WRITE COMMANDS (Emits Events & Projects to Read Model)
    # =========================================================================

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
        order_id: Optional[str] = None,
        timestamp: Optional[float] = None,
        coin: Optional[str] = None
    ) -> int:
        """
        Command: Open a trade.
        1. Projects into read model trades table.
        2. Appends ORDER_PLACED and ORDER_FILLED events to the immutable event store.
        """
        ts = timestamp or time.time()
        inferred_coin = coin or ("SOL" if "SOL" in series else ("XRP" if "XRP" in series else ("ETH" if "ETH" in series else "BTC")))
        actual_order_id = order_id or order_group_id
        actual_group_id = order_group_id or order_id

        # 1. Project into trades read table
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO trades (
                    timestamp, ticker, series, side, entry_price, contracts,
                    cost_dollars, fee_dollars, status, model_prob_entry,
                    delta_at_entry, seconds_left_at_entry, rationale, order_group_id, order_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?)
            """, (
                ts, ticker, series, side, entry_price, contracts,
                cost_dollars, fee_dollars, model_prob_entry,
                delta_at_entry, seconds_left_at_entry, rationale, actual_group_id, actual_order_id
            ))
            conn.commit()
            trade_id = cursor.lastrowid

        # 2. Append events to event store
        aggregate_id = str(trade_id)
        placed_payload = {
            "trade_id": trade_id,
            "ticker": ticker,
            "series": series,
            "coin": inferred_coin,
            "side": side,
            "entry_price": entry_price,
            "contracts": contracts,
            "cost_dollars": cost_dollars,
            "fee_dollars": fee_dollars,
            "model_prob_entry": model_prob_entry,
            "delta_at_entry": delta_at_entry,
            "seconds_left_at_entry": seconds_left_at_entry,
            "rationale": rationale,
            "order_group_id": order_group_id
        }
        self.append_event(aggregate_id, TradeEventType.ORDER_PLACED, placed_payload, event_version=2, timestamp=ts)

        filled_payload = {
            "trade_id": trade_id,
            "filled_contracts": contracts,
            "fill_price": entry_price,
            "cost_dollars": cost_dollars,
            "fee_dollars": fee_dollars
        }
        self.append_event(aggregate_id, TradeEventType.ORDER_FILLED, filled_payload, event_version=2, timestamp=ts)

        return trade_id

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
        """
        Command: Close an existing OPEN trade.
        1. Updates read model trades projection.
        2. Appends POSITION_CLOSED event to the immutable event store.
        """
        ts = exit_timestamp or time.time()

        # 1. Update read model
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
                exit_price, ts, exit_reason, pnl_dollars, pnl_pct, trade_id
            ))
            conn.commit()

        # 2. Append immutable POSITION_CLOSED event
        close_payload = {
            "trade_id": trade_id,
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "pnl_dollars": pnl_dollars,
            "pnl_pct": pnl_pct,
            "exit_timestamp": ts,
            "rationale": rationale
        }
        self.append_event(str(trade_id), TradeEventType.POSITION_CLOSED, close_payload, event_version=2, timestamp=ts)

    def record_order_cancelled(
        self,
        trade_id: int,
        reason: str = "CANCELLED_UNFILLED",
        remaining_contracts: int = 0
    ):
        """Command: Cancel an unfilled or partially filled order."""
        ts = time.time()
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE trades SET status = 'CANCELLED' WHERE id = ?", (trade_id,))
            conn.commit()

        payload = {
            "trade_id": trade_id,
            "reason": reason,
            "remaining_contracts": remaining_contracts,
            "timestamp": ts
        }
        self.append_event(str(trade_id), TradeEventType.ORDER_CANCELLED, payload, event_version=2, timestamp=ts)

    # =========================================================================
    # 3. PROJECTION REBUILDING (CQRS REBUILD CAPABILITY)
    # =========================================================================

    def rebuild_projections(self):
        """
        Reconstruct the read model `trades` table entirely by replaying all events
        from the immutable `trade_events` store.
        """
        events = self.get_all_events()

        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM trades")

            # In-memory accumulator per trade aggregate
            trades_reconstructed: Dict[int, Dict[str, Any]] = {}

            for ev in events:
                p = ev.payload
                tid = int(p.get("trade_id") or ev.aggregate_id)

                if ev.event_type == TradeEventType.ORDER_PLACED:
                    trades_reconstructed[tid] = {
                        "id": tid,
                        "timestamp": ev.timestamp,
                        "ticker": p.get("ticker", ""),
                        "series": p.get("series", ""),
                        "side": p.get("side", ""),
                        "entry_price": float(p.get("entry_price", 0.0)),
                        "contracts": int(p.get("contracts", 0)),
                        "cost_dollars": float(p.get("cost_dollars", 0.0)),
                        "fee_dollars": float(p.get("fee_dollars", 0.0)),
                        "status": "OPEN",
                        "model_prob_entry": p.get("model_prob_entry"),
                        "delta_at_entry": p.get("delta_at_entry"),
                        "seconds_left_at_entry": p.get("seconds_left_at_entry"),
                        "rationale": p.get("rationale"),
                        "order_group_id": p.get("order_group_id"),
                        "exit_price": None,
                        "exit_timestamp": None,
                        "exit_reason": None,
                        "pnl_dollars": None,
                        "pnl_pct": None,
                    }
                elif ev.event_type == TradeEventType.ORDER_FILLED:
                    if tid in trades_reconstructed:
                        trades_reconstructed[tid]["status"] = "OPEN"
                elif ev.event_type == TradeEventType.ORDER_CANCELLED:
                    if tid in trades_reconstructed:
                        trades_reconstructed[tid]["status"] = "CANCELLED"
                elif ev.event_type == TradeEventType.POSITION_CLOSED:
                    if tid in trades_reconstructed:
                        trades_reconstructed[tid]["status"] = "CLOSED"
                        trades_reconstructed[tid]["exit_price"] = p.get("exit_price")
                        trades_reconstructed[tid]["exit_timestamp"] = p.get("exit_timestamp", ev.timestamp)
                        trades_reconstructed[tid]["exit_reason"] = p.get("exit_reason")
                        trades_reconstructed[tid]["pnl_dollars"] = p.get("pnl_dollars")
                        trades_reconstructed[tid]["pnl_pct"] = p.get("pnl_pct")

            # Repopulate read table
            for t in sorted(trades_reconstructed.values(), key=lambda x: x["id"]):
                cursor.execute("""
                    INSERT INTO trades (
                        id, timestamp, ticker, series, side, entry_price, contracts,
                        cost_dollars, fee_dollars, exit_price, exit_timestamp, exit_reason,
                        pnl_dollars, pnl_pct, status, model_prob_entry, delta_at_entry,
                        seconds_left_at_entry, rationale, order_group_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    t["id"], t["timestamp"], t["ticker"], t["series"], t["side"],
                    t["entry_price"], t["contracts"], t["cost_dollars"], t["fee_dollars"],
                    t["exit_price"], t["exit_timestamp"], t["exit_reason"],
                    t["pnl_dollars"], t["pnl_pct"], t["status"], t["model_prob_entry"],
                    t["delta_at_entry"], t["seconds_left_at_entry"], t["rationale"], t["order_group_id"]
                ))
            conn.commit()

        logger.info(f"[AuditDatabase] Projections rebuilt successfully from {len(events)} events.")

    # =========================================================================
    # 4. EVENT REPLAY & PORTFOLIO STATE RECONSTRUCTION
    # =========================================================================

    def replay_portfolio_state(
        self,
        day_start_equity: float = 100.0,
        to_timestamp: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Reconstructs the complete portfolio state (positions, cash, realized PnL)
        by replaying the event log up to an optional point-in-time timestamp.
        """
        events = self.get_all_events(to_timestamp=to_timestamp)

        cash = day_start_equity
        realized_pnl = 0.0
        active_positions: Dict[str, Dict[str, Any]] = {}
        trade_count = 0
        winning_trades = 0

        for ev in events:
            p = ev.payload
            tid = str(p.get("trade_id") or ev.aggregate_id)
            coin = p.get("coin", "BTC")

            if ev.event_type == TradeEventType.ORDER_PLACED:
                # Deduct margin/cost for new position
                cost = float(p.get("cost_dollars", 0.0)) + float(p.get("fee_dollars", 0.0))
                cash -= cost
                active_positions[tid] = {
                    "trade_id": tid,
                    "coin": coin,
                    "ticker": p.get("ticker", ""),
                    "series": p.get("series", ""),
                    "side": p.get("side", "YES"),
                    "entry_price": float(p.get("entry_price", 0.50)),
                    "contracts": int(p.get("contracts", 0)),
                    "cost_dollars": float(p.get("cost_dollars", 0.0)),
                    "fee_dollars": float(p.get("fee_dollars", 0.0)),
                    "status": "OPEN",
                    "timestamp": ev.timestamp
                }

            elif ev.event_type == TradeEventType.ORDER_CANCELLED:
                # Refund reserved cash if order cancelled unfilled
                if tid in active_positions:
                    pos = active_positions.pop(tid)
                    cost = pos["cost_dollars"] + pos["fee_dollars"]
                    cash += cost

            elif ev.event_type == TradeEventType.POSITION_CLOSED:
                pnl = float(p.get("pnl_dollars", 0.0))
                realized_pnl += pnl
                trade_count += 1
                if pnl > 0:
                    winning_trades += 1

                if tid in active_positions:
                    pos = active_positions.pop(tid)
                    # Return initial cost + net PnL
                    cost = pos["cost_dollars"]
                    cash += (cost + pnl)

        # Calculate position valuation
        open_pos_val = sum(pos["cost_dollars"] for pos in active_positions.values())
        total_equity = cash + open_pos_val

        # Drawdown calculation
        loss_dollars = max(0.0, day_start_equity - total_equity)
        loss_pct = (loss_dollars / day_start_equity * 100.0) if day_start_equity > 0 else 0.0
        daily_loss_tripped = (loss_pct >= 10.0)

        return {
            "day_start_date": time.strftime("%Y-%m-%d"),
            "day_start_equity": round(day_start_equity, 2),
            "current_equity": round(total_equity, 2),
            "current_cash": round(cash, 2),
            "open_position_value": round(open_pos_val, 2),
            "realized_pnl": round(realized_pnl, 2),
            "daily_drawdown_pct": round(loss_pct, 2),
            "daily_loss_tripped": daily_loss_tripped,
            "open_positions": active_positions,
            "open_positions_count": len(active_positions),
            "total_trades": trade_count,
            "winning_trades": winning_trades,
            "win_rate_pct": round((winning_trades / trade_count * 100.0) if trade_count > 0 else 0.0, 1),
            "events_replayed_count": len(events),
            "replayed_up_to_timestamp": to_timestamp or time.time()
        }

    def rebuild_portfolio_state_file(
        self,
        day_start_equity: float = 100.0,
        output_file: str = "portfolio_state.json"
    ) -> Dict[str, Any]:
        """
        Reconstructs portfolio_state.json directly from the immutable event log
        via atomic tempfile replacement.
        """
        state = self.replay_portfolio_state(day_start_equity=day_start_equity)
        tmp = f"{output_file}.tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, output_file)
        logger.info(f"[AuditDatabase] Successfully rebuilt {output_file} from event log.")
        return state

    # =========================================================================
    # 5. READ PROJECTIONS (READ MODEL) — BACKWARD COMPATIBLE QUERIES
    # =========================================================================

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
                time.time(), cash, pos_val, equity, realized, unrealized, win_rate, trades_count
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
                time.time(), spot, open_p, delta, vol_24h, rsi, funding, prob_15m
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
                writer.writerow([row[col] for col in row.keys()])
        return output.getvalue()

    # =========================================================================
    # PARTITIONING & COLD ARCHIVAL INTERFACE
    # =========================================================================

    def archive_cold_data(
        self,
        cutoff_hours: int = 24,
        archive_dir: str = "archive",
        prune_hot: bool = False,
    ) -> Dict[str, Any]:
        """Archive trades and events older than cutoff_hours to Parquet."""
        from core.partition_manager import PartitionArchiveManager
        manager = PartitionArchiveManager(archive_base_dir=archive_dir)
        trades_res = manager.archive_cold_trades(
            db_path=self.db_path,
            cutoff_hours=cutoff_hours,
            prune_hot=prune_hot,
        )
        events_res = manager.archive_cold_events(
            db_path=self.db_path,
            cutoff_hours=cutoff_hours,
            prune_hot=prune_hot,
        )
        return {
            "trades": trades_res,
            "events": events_res,
        }

    def get_hot_trades(
        self,
        cutoff_hours: int = 24,
        limit: int = 500,
        ticker: Optional[str] = None,
        series: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Query hot trades residing in SQLite within the last cutoff_hours."""
        from core.partition_manager import PartitionArchiveManager
        manager = PartitionArchiveManager()
        return manager.query_hot_trades(
            db_path=self.db_path,
            cutoff_hours=cutoff_hours,
            limit=limit,
            ticker=ticker,
            series=series,
        )

    def get_unified_trades(
        self,
        archive_dir: str = "archive",
        start_ts: Optional[float] = None,
        end_ts: Optional[float] = None,
        ticker: Optional[str] = None,
        series: Optional[str] = None,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        """Query unified trades across hot SQLite and cold Parquet partitions."""
        from core.partition_manager import PartitionArchiveManager
        manager = PartitionArchiveManager(archive_base_dir=archive_dir)
        return manager.query_unified_trades(
            db_path=self.db_path,
            start_ts=start_ts,
            end_ts=end_ts,
            ticker=ticker,
            series=series,
            limit=limit,
        )

    def checkpoint(self, mode: str = "PASSIVE") -> Tuple[int, int, int]:
        """Trigger WAL checkpoint to flush WAL pages into primary DB file."""
        return self.pool.checkpoint(mode=mode)

    def close(self):
        """Close underlying connection pool and release file handles."""
        if hasattr(self, "pool") and self.pool is not None:
            self.pool.close_all()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


