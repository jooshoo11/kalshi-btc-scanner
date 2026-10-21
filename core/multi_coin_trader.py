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
                    rationale TEXT
                )
            """)
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
                    delta_at_entry, seconds_left_at_entry, rationale
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?)
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
                kwargs.get("rationale", "")
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
                FROM trades WHERE coin = ? AND status = 'CLOSED'
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
        db_path: str = "multi_coin_paper.db"
    ):
        self.initial_balance = initial_balance
        self.total_cash = initial_balance
        self.total_equity = initial_balance
        self.db = MultiCoinAuditDatabase(db_path=db_path)

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

        # Recover historical balance from DB if trades exist
        self._restore_from_db()

        # Active positions per coin: coin -> position_dict
        self.active_positions: Dict[str, Dict[str, Any]] = {}

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
        self.min_edge_pct = 4.0             # 4.0% min model edge
        self.min_prob_pct = 60.0            # 60.0% min model conviction

        self.last_candle_ids: Dict[str, str] = {c: "" for c in self.coin_weights}
        self.trades_in_candle: Dict[str, int] = {c: 0 for c in self.coin_weights}
        self.candle_stopped_out: Dict[str, str] = {c: "" for c in self.coin_weights}
        self.last_exit_ts: Dict[str, float] = {c: 0.0 for c in self.coin_weights}

    def _restore_from_db(self):
        """Restore cash and weights from historical database trades."""
        with sqlite3.connect(self.db.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT SUM(pnl_dollars) FROM trades WHERE status='CLOSED'")
            row = cursor.fetchone()
            cum_pnl = row[0] or 0.0
            if cum_pnl != 0.0:
                self.total_cash = round(self.initial_balance + cum_pnl, 2)
                self.total_equity = self.total_cash
                self.rebalance_weights_on_performance()

    def rebalance_weights_on_performance(self):
        """
        Dynamically adjusts capital weights based on empirical profit factor & win rate.
        Floor: 5% minimum. Ceiling: 50% maximum.
        """
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

        # Update sub-equities
        for coin, weight in self.coin_weights.items():
            self.coin_equities[coin] = round(self.total_equity * weight, 2)

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

        # Position Sizing: 20% of coin's sub-equity capped at 60 contracts
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

        # Debit cash
        self.total_cash -= (cost + total_fee)
        self.trades_in_candle[coin] = self.trades_in_candle.get(coin, 0) + 1

        rationale = (
            f"Multi-Coin Paper entered {target_side} on {ticker} ({contracts}x @ {int(target_price*100)}¢). "
            f"{coin} delta {delta:+.2f}. Model prob {model_prob:.1f}% (+{edge_pct:.1f}% edge)."
        )

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
            rationale=rationale
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
            "rationale": rationale
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
            reason = "SETTLED_WIN" if won else "SETTLED_LOSS"
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
