import time
from typing import Dict, Any, List, Optional
from bot_database import AuditDatabase
from prediction_engine import calculate_kalshi_fee

class PaperTradingBot:
    """
    Simulated Paper Trading Bot for Kalshi Bitcoin Markets.
    Initial balance: $50.00.
    Trades 15-minute Bitcoin candlestick markets (KXBTC15M) dynamically with:
    - Drift-diffusion momentum & BRTI 60-second variance collapse entry
    - Net Post-Fee Half-Kelly position sizing with 3¢ adverse slippage haircut
    - Active profit-taking (securing profits before resolution)
    - Stop-loss capital preservation
    - Mid-candle re-entry capability
    - Settlement resolution at expiry
    - Full SQLite audit trail
    """
    def __init__(self, db: AuditDatabase, initial_balance: float = 50.0, watchdog: Optional[Any] = None):
        self.db = db
        self.initial_balance = initial_balance
        self.cash = initial_balance
        self.open_position: Optional[Dict[str, Any]] = None
        self.watchdog = watchdog
        
        # Bot Configuration Settings
        self.enabled = True
        self.take_profit_roi_pct = 25.0    # Take profit when up 25%
        self.stop_loss_roi_pct = 25.0      # Cut loss when down 25%
        self.max_allocation_pct = 15.0     # Max 15% of equity per trade
        self.min_edge_pct = 4.0            # Minimum model edge required to enter
        self.slippage_penalty_cents = 3.0  # 3¢ adverse slippage haircut
        self.taker_fee_cents = 0.5         # Reference baseline taker fee

        # Candle & Re-entry tracking
        self.current_candle_id: str = ""
        self.trades_in_candle: int = 0
        self.max_trades_per_candle: int = 3 # Allows re-entry if profit was taken
        self.last_exit_ts: float = 0.0

        # Stats
        self.total_trades_count: int = 0
        self.winning_trades_count: int = 0
        self.realized_pnl: float = 0.0

        # Restore persistent state from DB if available
        self._restore_from_db()

    def _restore_from_db(self):
        try:
            stats = self.db.get_portfolio_summary_stats()
            if stats["total_trades"] > 0:
                self.total_trades_count = stats["total_trades"]
                self.winning_trades_count = stats["winning_trades"]
                self.realized_pnl = stats["realized_pnl"]
                self.cash = round(self.initial_balance + self.realized_pnl, 2)
        except Exception as e:
            print(f"[PaperTrader] Error restoring from DB: {e}")

    def get_equity(self) -> float:
        """Calculate total current equity: Cash + Position Market Value."""
        if not self.open_position:
            return self.cash
        pos_val = self.open_position["contracts"] * self.open_position.get("current_bid", self.open_position["entry_price"])
        return self.cash + pos_val

    def get_unrealized_pnl(self) -> float:
        """Calculate unrealized PnL in dollars on active open position."""
        if not self.open_position:
            return 0.0
        contracts = self.open_position["contracts"]
        current_bid = self.open_position.get("current_bid", self.open_position["entry_price"])
        entry_price = self.open_position["entry_price"]
        return round(contracts * (current_bid - entry_price), 2)

    def get_unrealized_pnl_pct(self) -> float:
        """Calculate unrealized PnL % on active open position."""
        if not self.open_position or self.open_position["entry_price"] <= 0:
            return 0.0
        current_bid = self.open_position.get("current_bid", self.open_position["entry_price"])
        entry_price = self.open_position["entry_price"]
        return round(((current_bid - entry_price) / entry_price) * 100.0, 1)

    def update_tick(
        self,
        scalp_data: Dict[str, Any],
        spot_price: float,
        metrics: Optional[Dict[str, Any]] = None
    ):
        """
        Main execution tick called every second by live engine.
        Monitors open position for profit securing/stop loss, settles on close,
        and evaluates new entries/re-entries.
        """
        if not scalp_data or not scalp_data.get("ticker"):
            return

        ticker = scalp_data.get("ticker", "")
        close_time = scalp_data.get("close_time", "")
        seconds_left = scalp_data.get("seconds_left", 0)
        candle_open = scalp_data.get("candle_open", spot_price)
        model_prob_yes = scalp_data.get("model_prob_yes_pct", 50.0)
        model_prob_no = scalp_data.get("model_prob_no_pct", 50.0)
        delta = scalp_data.get("delta", 0.0)
        edge_pct = scalp_data.get("best_edge_pct", 0.0)

        # Track candle transition
        if self.current_candle_id != close_time:
            # New 15-minute candle began
            self.current_candle_id = close_time
            self.trades_in_candle = 0

        # -------------------------------------------------------------
        # STEP 1: If open position exists, monitor and manage active trade
        # -------------------------------------------------------------
        if self.open_position:
            pos = self.open_position
            # Update current market bid for held contract
            if pos["side"] == "YES":
                current_bid = float(scalp_data.get("yes_bid_cents", 0)) / 100.0
                curr_model_prob = model_prob_yes
            else:
                current_bid = float(scalp_data.get("no_bid_cents", 0)) / 100.0
                curr_model_prob = model_prob_no

            pos["current_bid"] = max(0.01, current_bid)
            unrealized_pnl = self.get_unrealized_pnl()
            unrealized_pct = self.get_unrealized_pnl_pct()

            # A. Check for Resolution (Time expired or <= 3s)
            if seconds_left <= 3 or pos.get("ticker") != ticker:
                self._settle_at_expiration(pos, spot_price, candle_open, metrics)
                return

            # B. Dynamic Profit Securing (Take Profit Exit)
            # If market moved heavily in our favor, lock in profit early instead of risking late reversal
            if unrealized_pct >= self.take_profit_roi_pct and (time.time() - pos["entry_timestamp"] >= 5.0):
                self._close_position(
                    pos,
                    exit_price=current_bid,
                    reason="TAKE_PROFIT",
                    rationale=f"Secured +{unrealized_pct}% profit! Market bid jumped to {int(current_bid*100)}¢ (entry: {int(pos['entry_price']*100)}¢)."
                )
                return

            # C. Dynamic Risk Cut (Stop Loss)
            # If delta reversed and model probability collapsed below 35% with adverse drawdown
            if unrealized_pct <= -self.stop_loss_roi_pct and curr_model_prob <= 35.0:
                self._close_position(
                    pos,
                    exit_price=current_bid,
                    reason="STOP_LOSS",
                    rationale=f"Cut loss at {unrealized_pct}%: Model probability dropped to {curr_model_prob}% as delta flipped against position."
                )
                return

            return # Position still open and running

        # -------------------------------------------------------------
        # STEP 2: Evaluate New Entry or Re-entry
        # -------------------------------------------------------------
        if not self.enabled:
            return

        # Check Stale-Tick Watchdog safety circuit breaker
        if self.watchdog and not self.watchdog.is_safe_to_trade():
            return

        # Check re-entry limit & cooldown
        if self.trades_in_candle >= self.max_trades_per_candle:
            return
        if time.time() - self.last_exit_ts < 8.0:
            return # 8-second cooldown after exit

        # Timing constraints: Avoid first 50s (noise) but allow down to 10s for BRTI variance collapse!
        if seconds_left < 10 or seconds_left > 850:
            return

        # Evaluate Signal & Edge
        signal = scalp_data.get("trade_signal", "")
        yes_ask = float(scalp_data.get("yes_ask_cents", 0)) / 100.0
        no_ask = float(scalp_data.get("no_ask_cents", 0)) / 100.0

        target_side = None
        target_price = 0.0
        model_prob = 0.0

        if "BUY YES" in signal and edge_pct >= self.min_edge_pct and model_prob_yes >= 60.0 and yes_ask > 0 and yes_ask <= 0.78:
            target_side = "YES"
            target_price = yes_ask
            model_prob = model_prob_yes
        elif "BUY NO" in signal and edge_pct >= self.min_edge_pct and model_prob_no >= 60.0 and no_ask > 0 and no_ask <= 0.78:
            target_side = "NO"
            target_price = no_ask
            model_prob = model_prob_no

        if not target_side or target_price <= 0.05:
            return

        # Net Post-Fee Half-Kelly Position Sizing
        half_kelly_frac = float(scalp_data.get("half_kelly_pct", 0.0)) / 100.0
        if half_kelly_frac <= 0.01:
            return # Zero allocation if edge is insufficient after fees & 3¢ slippage

        # Allocate between Half-Kelly and max_allocation_pct safety ceiling
        alloc_pct = min(self.max_allocation_pct / 100.0, half_kelly_frac)
        equity = self.get_equity()
        budget = min(self.cash, equity * alloc_pct)

        # Apply 3¢ adverse slippage penalty to fill price
        fill_price = min(0.99, target_price + (self.slippage_penalty_cents / 100.0))
        fee_per_contract = calculate_kalshi_fee(1, fill_price)
        contracts = int(budget / (fill_price + fee_per_contract))
        if contracts < 1:
            return

        cost = contracts * fill_price
        fees = calculate_kalshi_fee(contracts, fill_price)
        total_debit = cost + fees

        if total_debit > self.cash:
            contracts = int((self.cash - fees) / fill_price)
            if contracts < 1:
                return
            cost = contracts * fill_price
            fees = calculate_kalshi_fee(contracts, fill_price)
            total_debit = cost + fees

        # Deduct cash and open position
        self.cash -= total_debit
        self.trades_in_candle += 1
        self.total_trades_count += 1

        rationale = (
            f"Bot entered {target_side} on {ticker} at {int(fill_price*100)}¢ ({contracts} contracts, Net Half-Kelly {half_kelly_frac*100:.1f}%). "
            f"BTC is {delta:+.1f} from open. Model prob {model_prob}% with +{edge_pct}% net edge."
        )

        trade_id = self.db.record_trade_open(
            ticker=ticker,
            series="KXBTC15M",
            side=target_side,
            entry_price=fill_price,
            contracts=contracts,
            cost_dollars=cost,
            fee_dollars=fees,
            model_prob_entry=model_prob,
            delta_at_entry=delta,
            seconds_left_at_entry=int(seconds_left),
            rationale=rationale
        )

        self.open_position = {
            "trade_id": trade_id,
            "ticker": ticker,
            "side": target_side,
            "entry_price": fill_price,
            "contracts": contracts,
            "cost_dollars": cost,
            "fee_dollars": fees,
            "current_bid": target_price,
            "entry_timestamp": time.time(),
            "close_time": close_time,
            "candle_open_at_entry": candle_open,
            "model_prob_at_entry": model_prob,
            "rationale": rationale
        }

    def _close_position(self, pos: Dict[str, Any], exit_price: float, reason: str, rationale: str):
        """Sell position to close early (securing profit or taking stop). Subject to Kalshi taker fee & adverse slippage."""
        contracts = pos["contracts"]
        # Apply adverse slippage haircut to early market exits (selling into bid - slippage)
        slip = self.slippage_penalty_cents / 100.0
        fill_exit_price = max(0.01, round(exit_price - slip, 2))

        gross_proceeds = contracts * fill_exit_price
        fees = calculate_kalshi_fee(contracts, fill_exit_price)
        net_proceeds = gross_proceeds - fees

        self.cash += net_proceeds
        pnl = net_proceeds - (pos["cost_dollars"] + pos["fee_dollars"])
        pnl_pct = (pnl / (pos["cost_dollars"] + pos["fee_dollars"])) * 100.0

        self.realized_pnl += pnl
        if pnl > 0:
            self.winning_trades_count += 1

        self.db.record_trade_close(
            trade_id=pos["trade_id"],
            exit_price=fill_exit_price,
            exit_reason=reason,
            pnl_dollars=round(pnl, 2),
            pnl_pct=round(pnl_pct, 1)
        )

        self.last_exit_ts = time.time()
        self.open_position = None

    def _settle_at_expiration(
        self,
        pos: Dict[str, Any],
        final_spot: float,
        candle_open: float,
        metrics: Optional[Any] = None
    ):
        """
        Settle contract at expiration against CF Benchmarks BRTI 60-second simple average.
        Kalshi settlement at expiration carries $0.00 fee.
        """
        contracts = pos["contracts"]
        settle_price = final_spot
        if metrics and hasattr(metrics, "brti_accumulator") and metrics.brti_accumulator.has_samples():
            settle_price = metrics.brti_accumulator.get_mean()
        elif isinstance(metrics, dict) and metrics.get("brti_mean"):
            settle_price = float(metrics["brti_mean"])

        won = (settle_price >= candle_open) if pos["side"] == "YES" else (settle_price < candle_open)

        payout_per_contract = 1.00 if won else 0.00
        gross_proceeds = contracts * payout_per_contract
        self.cash += gross_proceeds

        pnl = gross_proceeds - (pos["cost_dollars"] + pos["fee_dollars"])
        pnl_pct = (pnl / (pos["cost_dollars"] + pos["fee_dollars"])) * 100.0

        self.realized_pnl += pnl
        if pnl > 0:
            self.winning_trades_count += 1

        reason = "SETTLED_WIN" if won else "SETTLED_LOSS"
        self.db.record_trade_close(
            trade_id=pos["trade_id"],
            exit_price=payout_per_contract,
            exit_reason=reason,
            pnl_dollars=round(pnl, 2),
            pnl_pct=round(pnl_pct, 1)
        )

        self.last_exit_ts = time.time()
        self.open_position = None

    def manual_close_position(self, reason: str = "MANUAL_EXIT") -> Dict[str, Any]:
        """User manual emergency exit button from dashboard."""
        if not self.open_position:
            return {"status": "error", "message": "No open position"}
        pos = self.open_position
        exit_price = pos.get("current_bid", pos["entry_price"])
        self._close_position(pos, exit_price, reason, "Manually closed by user from dashboard.")
        return {"status": "success", "message": "Position closed successfully"}

    def reset_account(self, initial_balance: Optional[float] = None):
        """Reset paper trading balance back to initial capital."""
        if initial_balance:
            self.initial_balance = initial_balance
        self.cash = self.initial_balance
        self.open_position = None
        self.realized_pnl = 0.0
        self.total_trades_count = 0
        self.winning_trades_count = 0

    def get_status_dict(self) -> Dict[str, Any]:
        """Return full JSON status for dashboard display."""
        equity = self.get_equity()
        unrealized = self.get_unrealized_pnl()
        unrealized_pct = self.get_unrealized_pnl_pct()
        win_rate = (self.winning_trades_count / self.total_trades_count * 100.0) if self.total_trades_count > 0 else 0.0

        pos_data = None
        if self.open_position:
            p = self.open_position
            pos_data = {
                "trade_id": p["trade_id"],
                "ticker": p["ticker"],
                "side": p["side"],
                "entry_price_cents": int(round(p["entry_price"] * 100)),
                "current_bid_cents": int(round(p.get("current_bid", p["entry_price"]) * 100)),
                "contracts": p["contracts"],
                "cost_dollars": round(p["cost_dollars"], 2),
                "unrealized_pnl_dollars": unrealized,
                "unrealized_pnl_pct": unrealized_pct,
                "take_profit_target_cents": int(round(p["entry_price"] * (1.0 + self.take_profit_roi_pct / 100.0) * 100)),
                "stop_loss_target_cents": int(round(p["entry_price"] * (1.0 - self.stop_loss_roi_pct / 100.0) * 100)),
                "rationale": p["rationale"]
            }

        return {
            "enabled": self.enabled,
            "initial_balance": self.initial_balance,
            "cash": round(self.cash, 2),
            "total_equity": round(equity, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": unrealized,
            "unrealized_pnl_pct": unrealized_pct,
            "total_pnl": round(self.realized_pnl + unrealized, 2),
            "total_return_pct": round(((equity - self.initial_balance) / self.initial_balance) * 100.0, 2),
            "total_trades": self.total_trades_count,
            "win_rate_pct": round(win_rate, 1),
            "active_position": pos_data,
            "settings": {
                "take_profit_roi_pct": self.take_profit_roi_pct,
                "stop_loss_roi_pct": self.stop_loss_roi_pct,
                "max_allocation_pct": self.max_allocation_pct,
                "min_edge_pct": self.min_edge_pct
            },
            "watchdog": self.watchdog.get_status() if self.watchdog else None,
            "recent_trades": self.db.get_recent_trades(limit=20)
        }
