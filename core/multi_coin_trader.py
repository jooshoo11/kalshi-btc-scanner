"""
core/multi_coin_trader.py — Multi-Asset Paper Trading Engine with Performance-Based Allocation.

Supports BTC, ETH, SOL, XRP on a unified $100 starting portfolio.
Dynamically shifts capital allocation weights toward higher-performing assets while
enforcing 20% trade risk limits, +25% Take Profit, -25% Stop Loss, and 60s expiration cutoffs.
"""

import os
import time
import math
import sqlite3
import logging
import json
from typing import Dict, Any, List, Optional

logger = logging.getLogger("MultiCoinTrader")


def calculate_kalshi_taker_fee(contracts: int, price: float) -> float:
    """Standard Kalshi non-linear fee formula."""
    if contracts <= 0 or price <= 0.0 or price >= 1.0:
        return 0.0
    return math.ceil(0.07 * float(contracts) * float(price) * (1.0 - float(price)) * 100.0) / 100.0


class MultiCoinAuditDatabase:
    """SQLite storage for multi-coin paper portfolio."""
    def __init__(self, db_path: str = "multi_coin_paper.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    coin TEXT NOT NULL,
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
                    rationale TEXT,
                    order_group_id TEXT
                )
            """)
            # Schema migration: DBs created before order_group_id existed won't get
            # the column from CREATE TABLE IF NOT EXISTS -> every insert would fail.
            cols = {row[1] for row in cursor.execute("PRAGMA table_info(trades)").fetchall()}
            if "order_group_id" not in cols:
                cursor.execute("ALTER TABLE trades ADD COLUMN order_group_id TEXT")
                print("[MultiCoinDB] Migrated trades table: added order_group_id column")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL NOT NULL,
                    total_equity REAL NOT NULL,
                    total_cash REAL NOT NULL,
                    btc_weight REAL,
                    eth_weight REAL,
                    sol_weight REAL,
                    xrp_weight REAL,
                    btc_equity REAL,
                    eth_equity REAL,
                    sol_equity REAL,
                    xrp_equity REAL
                )
            """)
            conn.commit()

    def record_trade_open(self, **kwargs) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO trades (
                    timestamp, coin, ticker, series, side, entry_price, contracts,
                    cost_dollars, fee_dollars, status, model_prob_entry,
                    delta_at_entry, seconds_left_at_entry, rationale, order_group_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?)
            """, (
                kwargs.get("timestamp", time.time()),
                kwargs.get("coin", "BTC"),
                kwargs.get("ticker", ""),
                kwargs.get("series", ""),
                kwargs.get("side", "YES"),
                kwargs.get("entry_price", 0.50),
                kwargs.get("contracts", 1),
                kwargs.get("cost_dollars", 0.0),
                kwargs.get("fee_dollars", 0.0),
                kwargs.get("model_prob_entry", 50.0),
                kwargs.get("delta_at_entry", 0.0),
                kwargs.get("seconds_left_at_entry", 0),
                kwargs.get("rationale", ""),
                kwargs.get("order_group_id", None)
            ))
            conn.commit()
            return cursor.lastrowid

    def record_trade_close(self, trade_id: int, **kwargs):
        with sqlite3.connect(self.db_path) as conn:
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
                kwargs.get("exit_price", 0.0),
                kwargs.get("exit_timestamp", time.time()),
                kwargs.get("exit_reason", "UNKNOWN"),
                kwargs.get("pnl_dollars", 0.0),
                kwargs.get("pnl_pct", 0.0),
                trade_id
            ))
            conn.commit()

    def get_recent_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def get_coin_stats(self, coin: str) -> Dict[str, Any]:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    COUNT(*),
                    SUM(CASE WHEN pnl_dollars > 0 THEN 1 ELSE 0 END),
                    SUM(pnl_dollars),
                    SUM(cost_dollars)
                FROM trades
                WHERE coin = ? AND status = 'CLOSED'
                  -- condition 2: unconfirmed settlements are excluded from win-rate/Kelly until confirmed
                  AND (exit_reason IS NULL
                       OR (exit_reason NOT LIKE 'SETTLED_UNKNOWN%' AND exit_reason NOT LIKE 'SETTLED_ESTIMATED%'))
            """, (coin,))
            row = cursor.fetchone()
            total = row[0] or 0
            wins = row[1] or 0
            pnl = row[2] or 0.0
            volume = row[3] or 0.0
            win_rate = (wins / total * 100.0) if total > 0 else 0.0
            return {
                "coin": coin,
                "total_trades": total,
                "wins": wins,
                "losses": total - wins,
                "win_rate_pct": round(win_rate, 1),
                "total_pnl": round(pnl, 2),
                "volume": round(volume, 2)
            }


class MultiCoinPaperEngine:
    """
    Multi-Asset Paper Trading Engine ($100 initial balance).
    Dynamically adjusts performance-based allocation weights across BTC, ETH, SOL, XRP.
    """

    def __init__(
        self,
        initial_balance: float = 100.0,
        db_path: str = "multi_coin_paper.db",
        pinned_weights: Optional[Dict[str, float]] = None,
        disabled_coins: Optional[set] = None,
        ws_client: Optional[Any] = None,  # WebSocket client for L2 book access
    ):
        self.initial_balance = initial_balance
        self.total_cash = initial_balance
        self.total_equity = initial_balance
        self.db = MultiCoinAuditDatabase(db_path=db_path)

        # WebSocket client for L2 order book access (real-time slippage validation)
        self.ws_client: Optional[Any] = ws_client

        # Operator-pinned allocation (e.g. BTC 50% / ETH 50%): skip performance reshuffle
        self.pinned_weights: Optional[Dict[str, float]] = pinned_weights
        self.disabled_coins: set = disabled_coins or set()

        # Baseline performance weights (Sum = 100%)
        # BTC (40%), ETH (30%), SOL (20%), XRP (10%)
        self.coin_weights: Dict[str, float] = {
            "BTC": 0.40,
            "ETH": 0.30,
            "SOL": 0.20,
            "XRP": 0.10
        }

        # Sub-balances per coin
        self.coin_equities: Dict[str, float] = {
            coin: self.total_equity * weight
            for coin, weight in self.coin_weights.items()
        }

        # Apply pinned weights immediately if provided.
        # FIX: merge over the baseline 4-coin dict so SOL/XRP keys always exist
        # (pinned at 0.0 when disabled) instead of vanishing from the dict entirely.
        if self.pinned_weights:
            merged = dict(self.coin_weights)
            merged.update({c: float(w) for c, w in self.pinned_weights.items()})
            self.coin_weights = {c: round(float(w), 3) for c, w in merged.items()}
            for coin in self.disabled_coins:
                if coin in self.coin_weights:
                    self.coin_weights[coin] = 0.0
            s = sum(self.coin_weights.values())
            if s > 0:
                for coin in self.coin_weights:
                    if self.coin_weights[coin] > 0:
                        self.coin_weights[coin] = round(self.coin_weights[coin] / s, 3)
            for coin, weight in self.coin_weights.items():
                self.coin_equities[coin] = round(self.total_equity * weight, 2)

        # NOTE: _restore_from_db() is called below, AFTER active_positions exists,
        # because cold-boot recovery rebuilds open positions into that dict.

        # Risk parameters
        self.max_trade_alloc_pct = 20.0     # 20% of coin's sub-equity per trade
        self.max_contracts_per_trade = 60   # Liquidity protection cap to prevent orderbook slippage
        self.max_entry_price = 0.68         # Cap entry price at 68¢ to maintain positive risk/reward
        self.take_profit_roi_pct = 25.0     # Lock in +25% ROI
        self.stop_loss_roi_pct = 25.0       # Exit at -25% ROI if model confirms decay
        self.stop_loss_max_prob_pct = 38.0  # Dual confirmation: cut loss if model prob <= 38.0%
        self.stop_loss_hard_roi_pct = 28.0  # Hard circuit-breaker: cut immediately at -28% ROI regardless of model prob
        self.min_seconds_left = 90          # 90s expiration safety cutoff (avoid pin risk)
        self.max_seconds_left = 660         # 660s (first 4 minutes noise filter: avoid entering with >11m left)
        self.max_hold_seconds = 120.0       # CREATOR PREFERENCE: never hold to expiry — market-exit after this long (quick scalps like the first trades)
        self.min_edge_pct = 4.0             # 4.0% min model edge
        self.min_prob_pct = 60.0            # 60.0% min model conviction

        self.last_candle_ids: Dict[str, str] = {c: "" for c in self.coin_weights}
        self.trades_in_candle: Dict[str, int] = {c: 0 for c in self.coin_weights}
        self.candle_stopped_out: Dict[str, str] = {c: "" for c in self.coin_weights}
        self.last_exit_ts: Dict[str, float] = {c: 0.0 for c in self.coin_weights}

        # Active positions per coin: coin -> position_dict
        self.active_positions: Dict[str, Dict[str, Any]] = {}

        # Recover historical balance, weights AND open positions from DB
        self._restore_from_db()

        # State persistence
        self.state_file = "multi_coin_state.json"
        self.max_daily_loss_pct = 10.0
        self.max_daily_loss_usd = None
        self.max_open_exposure_pct = 50.0
        self.max_contracts_per_market = 100
        self.kill_file = "KILL"
        self._day_start_equity: float = initial_balance
        self._day_start_date: str = time.strftime("%Y-%m-%d")
        self.order_group_id: Optional[str] = None
        self.consecutive_failures: int = 0
        self.max_consecutive_failures: int = 5
        self.api_error_count: int = 0
        self.max_api_errors_per_minute: int = 20
        self._api_error_window_start: float = time.time()
        self._load_state()

    def _load_state(self):
        """Load persisted state from JSON (order_group_id, day_start_equity, etc.)."""
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, "r") as f:
                    state = json.load(f)
                self.order_group_id = state.get("order_group_id")
                self._day_start_equity = float(state.get("day_start_equity", self.initial_balance))
                self._day_start_date = state.get("day_start_date", time.strftime("%Y-%m-%d"))
                self.consecutive_failures = int(state.get("consecutive_failures", 0))
                logger.info(f"[MultiCoin] Loaded state: group_id={self.order_group_id}, day_start_equity={self._day_start_equity}")
        except Exception as e:
            logger.warning(f"[MultiCoin] Failed to load state: {e}")

    def _save_state(self):
        """Persist state to JSON (atomic write)."""
        try:
            state = {
                "order_group_id": self.order_group_id,
                "day_start_equity": round(self._day_start_equity, 2),
                "day_start_date": self._day_start_date,
                "consecutive_failures": self.consecutive_failures,
                "timestamp": time.time()
            }
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, self.state_file)
        except Exception as e:
            logger.error(f"[MultiCoin] Failed to save state: {e}")

    def _check_kill_switch(self) -> Optional[str]:
        """Check if KILL file exists. Returns reason string if kill active, else None."""
        if os.path.exists(self.kill_file):
            return f"KILL file present ({self.kill_file})"
        return None

    def _check_daily_loss_limit(self) -> Optional[str]:
        """Check if daily loss limit exceeded. Returns reason if tripped."""
        today = time.strftime("%Y-%m-%d")
        if today != self._day_start_date:
            # New day: reset day-start equity to current equity
            self._day_start_equity = self.total_equity
            self._day_start_date = today
            self._save_state()
            return None

        current_equity = self.total_equity
        loss_pct = ((self._day_start_equity - current_equity) / self._day_start_equity) * 100.0
        if loss_pct >= self.max_daily_loss_pct:
            return f"Daily loss limit hit: {loss_pct:.1f}% >= {self.max_daily_loss_pct}% (day-start equity ${self._day_start_equity:.2f} -> ${current_equity:.2f})"
        if self.max_daily_loss_usd is not None:
            loss_usd = self._day_start_equity - current_equity
            if loss_usd >= self.max_daily_loss_usd:
                return f"Daily loss limit hit: ${loss_usd:.2f} >= ${self.max_daily_loss_usd:.2f}"
        return None

    def _check_exposure_limit(self) -> Optional[str]:
        """Check if total open exposure exceeds max_open_exposure_pct of equity."""
        open_value = sum(p["contracts"] * p["entry_price"] for p in self.active_positions.values())
        max_exposure = self.total_equity * (self.max_open_exposure_pct / 100.0)
        if open_value > max_exposure:
            return f"Portfolio exposure limit: ${open_value:.2f} > ${max_exposure:.2f} ({self.max_open_exposure_pct}% of equity)"
        return None

    def _check_consecutive_failures(self) -> Optional[str]:
        if self.consecutive_failures >= self.max_consecutive_failures:
            return f"Consecutive failures limit: {self.consecutive_failures} >= {self.max_consecutive_failures}"
        return None

    def _check_api_error_rate(self) -> Optional[str]:
        now = time.time()
        if now - self._api_error_window_start > 60.0:
            self.api_error_count = 0
            self._api_error_window_start = now
        if self.api_error_count >= self.max_api_errors_per_minute:
            return f"API error rate limit: {self.api_error_count} errors in last minute"
        return None

    def _check_portfolio_daily_loss(self) -> Optional[str]:
        """PORTFOLIO-WIDE daily loss gate shared across ALL engines (agreed plan, phase 3).
        Evaluates the COMBINED equity of every registered engine, not just this one."""
        try:
            try:
                from .portfolio_state import check_portfolio_daily_loss, combined_equity
            except ImportError:
                from core.portfolio_state import check_portfolio_daily_loss, combined_equity
        except ImportError:
            return None
        reason = check_portfolio_daily_loss(combined_equity())
        if reason:
            return f"PORTFOLIO-WIDE {reason}"
        return None

    def _pre_trade_risk_checks(self) -> Optional[str]:
        """Run all pre-trade risk gates. Returns reason string if any fails, else None."""
        for check in [self._check_kill_switch, self._check_daily_loss_limit,
                      self._check_portfolio_daily_loss,
                      self._check_exposure_limit, self._check_consecutive_failures,
                      self._check_api_error_rate]:
            reason = check()
            if reason:
                logger.warning(f"[MultiCoin] Pre-trade risk gate FAILED: {reason}")
                return reason
        return None

    def record_api_error(self):
        self.api_error_count += 1
        self.consecutive_failures += 1
        self._save_state()

    def record_api_success(self):
        self.consecutive_failures = 0
        self._save_state()

    def _restore_from_db(self):
        """
        Restore cash, equity, weights AND open positions from the historical database.
        Cash invariant (DB is source of truth):
            total_cash = initial_balance + SUM(closed pnl) - SUM(open cost+fee)
        FIX: previously open trades were ignored (cash didn't subtract their cost and
        the positions were never rebuilt), so a restart orphaned OPEN rows and
        corrupted equity.
        """
        with sqlite3.connect(self.db.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT SUM(pnl_dollars) FROM trades WHERE status='CLOSED'")
            cum_pnl = cursor.fetchone()[0] or 0.0
            cursor.execute("SELECT SUM(cost_dollars + fee_dollars) FROM trades WHERE status='OPEN'")
            open_cost = cursor.fetchone()[0] or 0.0
            cursor.execute("SELECT * FROM trades WHERE status='OPEN' ORDER BY id")
            open_rows = [dict(r) for r in cursor.fetchall()]

        self.total_cash = round(self.initial_balance + cum_pnl - open_cost, 2)
        self.total_equity = round(self.total_cash + open_cost, 2)
        self.rebalance_weights_on_performance()

        for r in open_rows:
            coin = r["coin"]
            self.active_positions[coin] = {
                "trade_id": r["id"],
                "coin": coin,
                "ticker": r["ticker"],
                "series": r["series"],
                "side": r["side"],
                "contracts": r["contracts"],
                "entry_price": r["entry_price"],
                "cost_dollars": r["cost_dollars"],
                "fee_dollars": r["fee_dollars"],
                "entry_timestamp": r["timestamp"],
                "current_bid": r["entry_price"],
                "candle_id": "",
                "rationale": r.get("rationale") or "",
                "order_group_id": r.get("order_group_id"),
            }
        if open_rows:
            logger.info(f"[MultiCoin] Cold-boot restored {len(open_rows)} open position(s): {sorted(self.active_positions.keys())}")

    def reconcile_with_db(self) -> Dict[str, Any]:
        """
        Periodic reconciliation of in-memory engine state against SQLite (source of truth).
        - Adopts DB cash if memory drifted > $0.01 (loud log)
        - Rebuilds any OPEN DB trade missing from memory (e.g. crash mid-entry)
        - Warns on in-memory positions with no OPEN DB row
        Returns a report dict for the app-level reconcile loop.
        """
        report: Dict[str, Any] = {"timestamp": time.time(), "discrepancies": [], "cash_before": round(self.total_cash, 2)}
        with sqlite3.connect(self.db.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT SUM(pnl_dollars) FROM trades WHERE status='CLOSED'")
            cum_pnl = cur.fetchone()[0] or 0.0
            cur.execute("SELECT * FROM trades WHERE status='OPEN' ORDER BY id")
            open_rows = [dict(r) for r in cur.fetchall()]

        expected_cash = round(self.initial_balance + cum_pnl - sum(r["cost_dollars"] + r["fee_dollars"] for r in open_rows), 2)
        if abs(expected_cash - self.total_cash) > 0.01:
            report["discrepancies"].append(
                f"cash drift: memory ${self.total_cash:.2f} vs DB ${expected_cash:.2f} -> adopting DB value"
            )
            self.total_cash = expected_cash

        db_open_coins = {r["coin"] for r in open_rows}
        mem_coins = set(self.active_positions.keys())
        for r in open_rows:
            if r["coin"] not in mem_coins:
                report["discrepancies"].append(f"restored missing open position: {r['coin']} trade #{r['id']} ({r['ticker']})")
                self.active_positions[r["coin"]] = {
                    "trade_id": r["id"],
                    "coin": r["coin"],
                    "ticker": r["ticker"],
                    "series": r["series"],
                    "side": r["side"],
                    "contracts": r["contracts"],
                    "entry_price": r["entry_price"],
                    "cost_dollars": r["cost_dollars"],
                    "fee_dollars": r["fee_dollars"],
                    "entry_timestamp": r["timestamp"],
                    "current_bid": r["entry_price"],
                    "candle_id": "",
                    "rationale": r.get("rationale") or "",
                    "order_group_id": r.get("order_group_id"),
                }
        for coin in sorted(mem_coins - db_open_coins):
            report["discrepancies"].append(f"WARNING: in-memory position on {coin} has no OPEN row in DB")

        self._update_total_equity()
        report["cash_after"] = round(self.total_cash, 2)
        report["equity"] = round(self.total_equity, 2)
        report["open_positions"] = sorted(db_open_coins)
        return report

    def rebalance_weights_on_performance(self):
        """
        Dynamically adjusts capital weights based on empirical profit factor & win rate.
        Floor: 5% minimum. Ceiling: 50% maximum.
        """
        # Operator-pinned allocation (e.g. BTC 50% / ETH 50%): skip performance reshuffle
        if getattr(self, "pinned_weights", None):
            merged = dict(self.coin_weights)
            merged.update({c: float(w) for c, w in self.pinned_weights.items()})
            self.coin_weights = {c: round(float(w), 3) for c, w in merged.items()}
            for coin in getattr(self, "disabled_coins", set()) or set():
                if coin in self.coin_weights:
                    self.coin_weights[coin] = 0.0
            s = sum(self.coin_weights.values())
            if s > 0:
                for coin in self.coin_weights:
                    if self.coin_weights[coin] > 0:
                        self.coin_weights[coin] = round(self.coin_weights[coin] / s, 3)
            for coin, weight in self.coin_weights.items():
                self.coin_equities[coin] = round(self.total_equity * weight, 2)
            return

        raw_scores = {}
        for coin in self.coin_weights:
            stats = self.db.get_coin_stats(coin)
            trades = stats["total_trades"]
            win_rate = stats["win_rate_pct"]
            pnl = stats["total_pnl"]

            # Prior base score: 1.0 + (win_rate - 50)/50 + pnl_factor
            if trades >= 3:
                score = max(0.2, (win_rate / 100.0) + (pnl / max(10.0, self.initial_balance)))
            else:
                score = {"BTC": 1.2, "ETH": 1.0, "SOL": 0.9, "XRP": 0.7}[coin]
            raw_scores[coin] = score

        total_score = sum(raw_scores.values())
        if total_score > 0:
            for coin in self.coin_weights:
                norm_w = raw_scores[coin] / total_score
                # Clamped between 5% and 50%
                self.coin_weights[coin] = round(max(0.05, min(0.50, norm_w)), 3)

            # Re-normalize to exactly 1.00
            s = sum(self.coin_weights.values())
            for coin in self.coin_weights:
                self.coin_weights[coin] = round(self.coin_weights[coin] / s, 3)

        # FIX: respect disabled coins — pin them to 0 (overrides the 5% floor clamp above)
        # and re-normalize the remaining enabled coins to sum to 1.00
        disabled = getattr(self, "disabled_coins", None) or set()
        if disabled:
            for coin in disabled:
                if coin in self.coin_weights:
                    self.coin_weights[coin] = 0.0
            s = sum(self.coin_weights.values())
            if s > 0:
                for coin in self.coin_weights:
                    if self.coin_weights[coin] > 0:
                        self.coin_weights[coin] = round(self.coin_weights[coin] / s, 3)

        # Update sub-equities
        for coin, weight in self.coin_weights.items():
            self.coin_equities[coin] = round(self.total_equity * weight, 2)

    def _validate_l2_entry(
        self,
        ticker: str,
        side: str,  # "YES" or "NO"
        target_price: float,
        contracts: int,
        model_prob: float,
        edge_pct: float,
    ) -> bool:
        """
        Validate entry against live L2 order book for slippage/VWAP edge protection.
        Returns True if entry passes L2 validation, False if should abort.
        """
        if not self.ws_client:
            # No WS client available - skip L2 validation (fallback to REST prices)
            return True

        # Get L2 book for this ticker
        book = None
        if hasattr(self.ws_client, "books") and ticker in self.ws_client.books:
            book = self.ws_client.books[ticker]
        elif hasattr(self.ws_client, "get_or_create_book"):
            book = self.ws_client.get_or_create_book(ticker)

        if not book:
            # No L2 book available - skip validation
            return True

        if getattr(book, "stale", False):
            # FINDING #7: book marked stale after a delta parse failure — refuse to gate on drifted data
            logger.warning(f"[L2 Validation] Book for {ticker} is STALE — aborting entry on stale data")
            return False

        # Determine which side of the book to sweep
        is_buy_yes = (side == "YES")
        order_levels = book.asks if is_buy_yes else book.bids

        if not order_levels:
            logger.warning(f"[L2 Validation] No L2 depth on {'asks' if is_buy_yes else 'bids'} for {ticker}")
            return False

        # Walk down L2 book to calculate VWAP for target contracts
        sorted_prices = sorted(order_levels.keys()) if is_buy_yes else sorted(order_levels.keys(), reverse=True)
        accumulated_qty = 0
        total_cost_cents = 0
        worst_price_cents = 0

        for p in sorted_prices:
            avail = order_levels[p]
            needed = contracts - accumulated_qty
            fill = min(avail, needed)
            effective_level_price = (100 - p) if side == "NO" else p
            total_cost_cents += fill * effective_level_price
            accumulated_qty += fill
            worst_price_cents = effective_level_price
            if accumulated_qty >= contracts:
                break

        if accumulated_qty < contracts:
            # Partial liquidity - scale down or reject
            logger.warning(
                f"[L2 Validation] Insufficient liquidity on {ticker}: "
                f"requested {contracts}, available {accumulated_qty} at {side} levels"
            )
            return False

        vwap_cents = round(total_cost_cents / float(accumulated_qty), 2)
        vwap_dollars = vwap_cents / 100.0

        # Calculate net edge after VWAP slippage and fees.
        # FIX (units): model_prob arrives in PERCENT (e.g. 77.3) while VWAP/fee are
        # dollars (0-1). Mixing them made net_edge ~76 -> edge_pct ~7600%, so the
        # slippage abort could never fire. Also FIX (name): calculate_kalshi_fee
        # does not exist in this module -> NameError; use calculate_kalshi_taker_fee.
        kalshi_fee = calculate_kalshi_taker_fee(1, vwap_dollars)
        model_frac = model_prob / 100.0 if model_prob > 1.0 else model_prob
        net_edge = model_frac - (vwap_dollars + kalshi_fee)
        actual_edge_pct = round(net_edge * 100.0, 2)

        # Check if edge survives slippage
        min_edge_pct = getattr(self, "min_edge_pct", 4.0)
        if actual_edge_pct < min_edge_pct:
            logger.warning(
                f"[L2 Validation] VWAP slippage abort on {ticker}: "
                f"VWAP {vwap_cents:.1f}¢ leaves net edge {actual_edge_pct:.1f}% "
                f"< threshold {min_edge_pct:.1f}% (REST price: {target_price*100:.0f}¢)"
            )
            return False

        # Log VWAP info for debugging
        logger.info(
            f"[L2 Validation] {ticker} {side} {contracts}x @ VWAP {vwap_cents:.1f}¢ "
            f"(REST: {target_price*100:.0f}¢, worst: {worst_price_cents}¢) | "
            f"Model prob {model_prob:.1f}% | Net edge {actual_edge_pct:.1f}%"
        )
        return True

    def evaluate_entry(
        self,
        coin: str,
        ticker: str,
        series: str,
        signal: str,
        edge_pct: float,
        model_prob: float,
        yes_ask: float,
        no_ask: float,
        seconds_left: float,
        delta: float,
        candle_id: str,
        current_time: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        """Evaluate paper entry for a specific coin under portfolio rules."""
        now = current_time or time.time()
        if coin in self.active_positions:
            return None

        # ==================== PRE-TRADE RISK GATES ====================
        risk_reason = self._pre_trade_risk_checks()
        if risk_reason:
            return None

        # Timing constraints: Avoid first 3 minutes of noise (>720s) and final 90s expiration trap (<90s)
        if seconds_left < self.min_seconds_left or seconds_left > self.max_seconds_left:
            return None

        # Re-entry Lockout: Do not re-enter if coin stopped out in current candle
        if self.candle_stopped_out.get(coin) == candle_id:
            return None

        # Candle trade limit
        if self.last_candle_ids.get(coin) != candle_id:
            self.last_candle_ids[coin] = candle_id
            self.trades_in_candle[coin] = 0

        if self.trades_in_candle.get(coin, 0) >= 3:
            return None

        # Cooldown
        if now - self.last_exit_ts.get(coin, 0.0) < 4.0:
            return None

        # Conviction & edge requirements
        if edge_pct < self.min_edge_pct or model_prob < self.min_prob_pct:
            return None

        target_side = None
        target_price = 0.0
        if "BUY YES" in signal and 0.20 <= yes_ask <= 0.80:
            target_side = "YES"
            target_price = yes_ask
        elif "BUY NO" in signal and 0.20 <= no_ask <= 0.80:
            target_side = "NO"
            target_price = no_ask

        if not target_side or target_price <= 0.0:
            return None

        # Entry price gate: reject contracts priced above 68¢ (or below 20¢)
        if target_price > getattr(self, "max_entry_price", 0.68) or target_price < 0.20:
            return None

        # ==================== POSITION SIZING ====================
        # Compute size FIRST: L2 validation needs the contract count to walk the book.
        # FIX (crash): this block used to run AFTER _validate_l2_entry, so `contracts`
        # was unbound at the call site -> UnboundLocalError on EVERY entry attempt,
        # which silently froze the engine (no trades since the L2 gate was added).
        # 20% of coin's sub-equity capped at max_contracts_per_trade
        sub_equity = self.coin_equities.get(coin, self.total_equity * self.coin_weights.get(coin, 0.25))
        trade_budget = sub_equity * (self.max_trade_alloc_pct / 100.0)
        trade_budget = min(trade_budget, self.total_cash)

        fee_est = calculate_kalshi_taker_fee(1, target_price)
        contracts = int(trade_budget / (target_price + fee_est))
        max_c = getattr(self, "max_contracts_per_trade", 60)
        if max_c > 0:
            contracts = min(contracts, max_c)
        if contracts < 1:
            return None

        cost = round(contracts * target_price, 2)
        total_fee = calculate_kalshi_taker_fee(contracts, target_price)

        if cost + total_fee > self.total_cash:
            contracts = int((self.total_cash - total_fee) / target_price)
            if contracts < 1:
                return None
            cost = round(contracts * target_price, 2)
            total_fee = calculate_kalshi_taker_fee(contracts, target_price)

        # ==================== L2 ORDER BOOK VALIDATION ====================
        # Validate against live L2 book for slippage/VWAP edge protection
        if not self._validate_l2_entry(ticker, target_side, target_price, contracts, model_prob, edge_pct):
            return None

        # Debit cash (only after ALL gates pass, incl. L2 VWAP sweep)
        self.total_cash -= (cost + total_fee)
        self.trades_in_candle[coin] = self.trades_in_candle.get(coin, 0) + 1

        rationale = (
            f"Multi-Coin Paper entered {target_side} on {ticker} ({contracts}x @ {int(target_price*100)}¢). "
            f"{coin} delta {delta:+.2f}. Model prob {model_prob:.1f}% (+{edge_pct:.1f}% edge)."
        )

        # Ensure we have an order group ID (create if needed, reuse if exists)
        if not self.order_group_id:
            self.order_group_id = f"mcg_{int(time.time()*1000)}"
            self._save_state()

        trade_id = self.db.record_trade_open(
            timestamp=time.time(),
            coin=coin,
            ticker=ticker,
            series=series,
            side=target_side,
            entry_price=target_price,
            contracts=contracts,
            cost_dollars=cost,
            fee_dollars=total_fee,
            model_prob_entry=model_prob,
            delta_at_entry=delta,
            seconds_left_at_entry=int(seconds_left),
            rationale=rationale,
            order_group_id=self.order_group_id
        )

        pos_dict = {
            "trade_id": trade_id,
            "coin": coin,
            "ticker": ticker,
            "series": series,
            "side": target_side,
            "contracts": contracts,
            "entry_price": target_price,
            "cost_dollars": cost,
            "fee_dollars": total_fee,
            "entry_timestamp": time.time(),
            "current_bid": target_price,
            "candle_id": candle_id,
            "rationale": rationale,
            "order_group_id": self.order_group_id
        }
        self.active_positions[coin] = pos_dict
        logger.info(f"[MultiCoin Open] {coin} | {ticker} {target_side} {contracts}x @ {int(target_price*100)}¢ | Cost: ${cost:.2f}")
        return pos_dict

    def update_position(
        self,
        coin: str,
        current_bid: float,
        seconds_left: float,
        settle_spot: float,
        strike: float,
        current_time: Optional[float] = None,
        model_prob: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        """Update open position: checks +25% Take Profit, Dual-Confirmation Stop Loss, or Expiration."""
        pos = self.active_positions.get(coin)
        if not pos:
            return None

        now = current_time or time.time()
        entry_p = pos["entry_price"]
        roi_pct = ((current_bid - entry_p) / entry_p) * 100.0

        # A. Expiration Resolution
        if seconds_left <= 3:
            won = (settle_spot >= strike) if pos["side"] == "YES" else (settle_spot < strike)
            payout = pos["contracts"] * 1.00 if won else 0.00
            pnl = payout - (pos["cost_dollars"] + pos["fee_dollars"])
            # Spot-vs-strike simulation: tagged ESTIMATED (excluded from win-rate/Kelly/baseline
            # until the exchange settlement confirms it) — agreed plan, condition 2.
            reason = "SETTLED_ESTIMATED_WIN" if won else "SETTLED_ESTIMATED_LOSS"
            return self._close_position(coin, exit_price=1.0 if won else 0.0, reason=reason, pnl=pnl, payout=payout, current_time=now)

        # B. Take Profit Exit (+25% ROI)
        if roi_pct >= self.take_profit_roi_pct:
            exit_fee = calculate_kalshi_taker_fee(pos["contracts"], current_bid)
            gross = pos["contracts"] * current_bid
            pnl = gross - exit_fee - (pos["cost_dollars"] + pos["fee_dollars"])
            return self._close_position(coin, exit_price=current_bid, reason="TAKE_PROFIT", pnl=pnl, payout=gross - exit_fee, current_time=now)

        # C. Dynamic Dual-Confirmation Stop Loss (-25% ROI AND Model Prob <= 38%, OR Hard Circuit-Breaker at -28% ROI)
        is_dual_confirmed = (roi_pct <= -self.stop_loss_roi_pct and (model_prob is None or model_prob <= self.stop_loss_max_prob_pct))
        is_hard_stop = (roi_pct <= -self.stop_loss_hard_roi_pct)
        if is_dual_confirmed or is_hard_stop:
            exit_fee = calculate_kalshi_taker_fee(pos["contracts"], current_bid)
            gross = pos["contracts"] * current_bid
            pnl = gross - exit_fee - (pos["cost_dollars"] + pos["fee_dollars"])
            candle_id = pos.get("candle_id", "")
            if candle_id:
                self.candle_stopped_out[coin] = candle_id
            return self._close_position(coin, exit_price=current_bid, reason="STOP_LOSS", pnl=pnl, payout=gross - exit_fee, current_time=now)

        # D. Time-based exit: NEVER hold a position to expiry. If the trade has been
        # open longer than max_hold_seconds and neither TP nor SL fired, exit at market.
        hold_secs = now - pos.get("entry_timestamp", now)
        if hold_secs >= self.max_hold_seconds:
            exit_fee = calculate_kalshi_taker_fee(pos["contracts"], current_bid)
            gross = pos["contracts"] * current_bid
            pnl = gross - exit_fee - (pos["cost_dollars"] + pos["fee_dollars"])
            logger.info(f"[MultiCoin] TIME_EXIT {coin} after {hold_secs:.0f}s (max_hold {self.max_hold_seconds:.0f}s) | ROI {roi_pct:+.1f}% | PnL ${pnl:+.2f}")
            return self._close_position(coin, exit_price=current_bid, reason="TIME_EXIT", pnl=pnl, payout=gross - exit_fee, current_time=now)

        return None

    def _close_position(self, coin: str, exit_price: float, reason: str, pnl: float, payout: float, current_time: Optional[float] = None) -> Optional[Dict[str, Any]]:
        pos = self.active_positions.pop(coin, None)
        if not pos:
            return None

        now = current_time or time.time()
        self.total_cash += payout
        roi = (pnl / max(0.01, pos["cost_dollars"] + pos["fee_dollars"])) * 100.0

        self.db.record_trade_close(
            trade_id=pos["trade_id"],
            exit_price=exit_price,
            exit_timestamp=now,
            exit_reason=reason,
            pnl_dollars=round(pnl, 2),
            pnl_pct=round(roi, 1)
        )

        self.last_exit_ts[coin] = now
        self._update_total_equity()
        self.rebalance_weights_on_performance()
        logger.info(f"[MultiCoin Close] {coin} | {pos['ticker']} {reason} | PnL: ${pnl:.2f} ({roi:+.1f}%) | Equity: ${self.total_equity:.2f}")
        return {
            "coin": coin,
            "ticker": pos["ticker"],
            "side": pos["side"],
            "contracts": pos["contracts"],
            "entry_price": pos["entry_price"],
            "exit_price": exit_price,
            "reason": reason,
            "pnl": round(pnl, 2),
            "roi": round(roi, 1),
            "equity": self.total_equity
        }

    def _update_total_equity(self):
        open_val = sum(p["cost_dollars"] for p in self.active_positions.values())
        self.total_equity = round(self.total_cash + open_val, 2)
        for coin, weight in self.coin_weights.items():
            self.coin_equities[coin] = round(self.total_equity * weight, 2)

    def get_status_dict(self) -> Dict[str, Any]:
        """Telemetry dictionary for dashboard API."""
        self._update_total_equity()
        coin_stats = {coin: self.db.get_coin_stats(coin) for coin in self.coin_weights}
        total_trades = sum(s["total_trades"] for s in coin_stats.values())
        total_wins = sum(s["wins"] for s in coin_stats.values())
        overall_win_rate = (total_wins / total_trades * 100.0) if total_trades > 0 else 0.0

        return {
            "initial_balance": self.initial_balance,
            "total_cash": round(self.total_cash, 2),
            "total_equity": round(self.total_equity, 2),
            "total_pnl": round(self.total_equity - self.initial_balance, 2),
            "total_return_pct": round(((self.total_equity - self.initial_balance) / self.initial_balance) * 100.0, 2),
            "total_trades": total_trades,
            "overall_win_rate_pct": round(overall_win_rate, 1),
            "coin_weights": self.coin_weights,
            "coin_equities": self.coin_equities,
            "coin_stats": coin_stats,
            "active_positions": self.active_positions,
            "recent_trades": self.db.get_recent_trades(limit=25)
        }
