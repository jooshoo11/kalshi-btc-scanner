"""
core/portfolio_manager.py — Multi-Asset Centralized Portfolio & Risk Manager.

Coordinates account buying power, exchange collateral partitioning, and concurrent trade sizing
across multiple assets (BTC, ETH, SOL, XRP) with asynchronous serialization locks to guarantee
zero margin rejections, zero over-allocation, and strict enforcement of global_risk.json caps:
- 40% Portfolio Allocation Cap
- 20% Single-Asset Allocation Cap
- Max 2 Concurrent Positions
- 10% Global Daily Stop-Loss Circuit Breaker
- Real-time R-multiple tracking & expectancy
- Real-time per-asset drawdown tracking (BTC, ETH, SOL, XRP)
- Automated hedging recommendations when approaching the 10% daily stop loss
"""

import time
import asyncio
import logging
from typing import Dict, Any, Optional, List, Tuple

try:
    from .kill_switch import kill_switch
except ImportError:
    try:
        from core.kill_switch import kill_switch
    except ImportError:
        kill_switch = None

logger = logging.getLogger("PortfolioManager")

SUPPORTED_ASSETS = ["BTC", "ETH", "SOL", "XRP"]


class PortfolioManager:
    """
    Centralized Capital Allocator, Risk Guard & R-Multiple Tracker for Kalshi Multi-Asset Trading.
    """

    def __init__(
        self,
        auth: Any,
        rest_url: str,
        initial_balance: float = 50.0,
        max_portfolio_allocation_pct: float = 40.0,
        max_single_trade_pct: float = 20.0,
        max_concurrent_positions: int = 2,
        global_stop_loss_daily_pct: float = 10.0,
        day_start_equity: Optional[float] = None
    ):
        self.auth = auth
        self.rest_url = rest_url
        self.total_cash: float = initial_balance
        self.total_equity: float = initial_balance
        self.allocated_capital: float = 0.0

        # Global risk constraints
        self.max_portfolio_allocation_pct = max_portfolio_allocation_pct
        self.max_single_trade_pct = max_single_trade_pct
        self.max_concurrent_positions = max_concurrent_positions
        self.global_stop_loss_daily_pct = global_stop_loss_daily_pct

        # Daily baseline & high water marks
        self.day_start_equity: float = day_start_equity if day_start_equity is not None else initial_balance
        self.peak_portfolio_equity: float = max(self.total_equity, self.day_start_equity)

        # Asset allocations and active position trackers
        self.active_positions: Dict[str, Dict[str, Any]] = {}
        self.asset_allocations: Dict[str, float] = {}
        self._pending_reservations: set = set()

        # Real-time per-asset drawdown tracking (BTC, ETH, SOL, XRP)
        self.asset_stats: Dict[str, Dict[str, Any]] = {}
        for coin in SUPPORTED_ASSETS:
            coin_base = self.day_start_equity * (self.max_single_trade_pct / 100.0)
            self.asset_stats[coin] = {
                "peak_equity": coin_base,
                "current_equity": coin_base,
                "current_drawdown_pct": 0.0,
                "max_drawdown_pct": 0.0,
                "realized_pnl": 0.0,
                "trade_count": 0
            }

        # R-Multiple Trade Tracking
        self.trade_r_history: List[Dict[str, Any]] = []

        # Concurrency Lock preventing race conditions on simultaneous signals
        # Dedicated margin lock for atomic buying power reservation (microsecond critical section)
        self._margin_lock = asyncio.Lock()
        self._lock = self._margin_lock  # Backward compatibility alias
        self._asset_locks: Dict[str, asyncio.Lock] = {}
        self._last_sync_ts = 0.0

    def _get_asset_lock(self, asset: str) -> asyncio.Lock:
        """Returns or creates a per-asset lock to serialize state updates for a specific coin."""
        coin = asset.upper()
        if coin not in self._asset_locks:
            self._asset_locks[coin] = asyncio.Lock()
        return self._asset_locks[coin]

    # =========================================================================
    # 1. DRAWDOWN & RISK MONITORING
    # =========================================================================

    def get_daily_drawdown_pct(self) -> float:
        """Calculates current portfolio daily drawdown percentage relative to day-start equity."""
        if self.day_start_equity <= 0:
            return 0.0
        drawdown_dollars = self.day_start_equity - self.total_equity
        if drawdown_dollars <= 0:
            return 0.0
        return (drawdown_dollars / self.day_start_equity) * 100.0

    def update_asset_equity(self, asset: str, current_value: float) -> None:
        """Update per-asset high-water mark and real-time drawdown."""
        coin = asset.upper()
        if coin not in self.asset_stats:
            self.asset_stats[coin] = {
                "peak_equity": current_value,
                "current_equity": current_value,
                "current_drawdown_pct": 0.0,
                "max_drawdown_pct": 0.0,
                "realized_pnl": 0.0,
                "trade_count": 0
            }

        stats = self.asset_stats[coin]
        stats["current_equity"] = current_value
        if current_value > stats["peak_equity"]:
            stats["peak_equity"] = current_value

        if stats["peak_equity"] > 0:
            dd = ((stats["peak_equity"] - current_value) / stats["peak_equity"]) * 100.0
            stats["current_drawdown_pct"] = round(max(0.0, dd), 2)
            if stats["current_drawdown_pct"] > stats["max_drawdown_pct"]:
                stats["max_drawdown_pct"] = stats["current_drawdown_pct"]

    def get_asset_drawdowns(self) -> Dict[str, Dict[str, Any]]:
        """Return real-time drawdown metrics across all coins."""
        return {
            coin: {
                "peak_equity": round(data["peak_equity"], 2),
                "current_equity": round(data["current_equity"], 2),
                "current_drawdown_pct": data["current_drawdown_pct"],
                "max_drawdown_pct": data["max_drawdown_pct"],
                "realized_pnl": round(data["realized_pnl"], 2),
                "trade_count": data["trade_count"],
                "is_active": coin in self.active_positions
            }
            for coin, data in self.asset_stats.items()
        }

    # =========================================================================
    # 2. R-MULTIPLE TRACKING & EXPECTANCY
    # =========================================================================

    def record_trade_result(
        self,
        asset: str,
        net_pnl: float,
        initial_risk_dollars: Optional[float] = None,
        contracts: int = 1,
        exit_reason: str = "EXIT"
    ) -> Dict[str, Any]:
        """
        Record completed trade in R-multiples (where 1R = Initial Dollar Risk).
        
        Args:
            asset: Asset symbol (e.g. BTC)
            net_pnl: Realized dollar PnL after all fees and slippage.
            initial_risk_dollars: Dollar amount risked (defaults to trade cost * stop_loss_pct).
            contracts: Number of contracts traded.
            exit_reason: TAKE_PROFIT, STOP_LOSS, EXPIRATION, etc.
        """
        coin = asset.upper()
        # Default 1R risk if omitted is 25% of position cost or $2.00 min
        one_r = initial_risk_dollars if (initial_risk_dollars and initial_risk_dollars > 0) else max(2.0, abs(net_pnl))

        r_multiple = round(net_pnl / one_r, 2)

        trade_entry = {
            "timestamp": time.time(),
            "asset": coin,
            "contracts": contracts,
            "net_pnl": round(net_pnl, 2),
            "one_r_dollars": round(one_r, 2),
            "r_multiple": r_multiple,
            "is_win": (net_pnl > 0),
            "exit_reason": exit_reason
        }
        self.trade_r_history.append(trade_entry)
        # Memory safety: bound trade history in memory to prevent indefinite leak
        if len(self.trade_r_history) > 2000:
            self.trade_r_history.pop(0)

        # Update per-asset realized stats
        if coin in self.asset_stats:
            self.asset_stats[coin]["realized_pnl"] += net_pnl
            self.asset_stats[coin]["trade_count"] += 1
            new_sub_equity = self.asset_stats[coin]["current_equity"] + net_pnl
            self.update_asset_equity(coin, new_sub_equity)

        return trade_entry

    def get_r_multiple_stats(self) -> Dict[str, Any]:
        """Calculates aggregate R-multiple performance, expectancy, and profit factor."""
        if not self.trade_r_history:
            return {
                "total_trades": 0,
                "win_rate_pct": 0.0,
                "total_r": 0.0,
                "avg_win_r": 0.0,
                "avg_loss_r": 0.0,
                "expectancy_r": 0.0,
                "profit_factor": 0.0,
                "recent_r_multiples": []
            }

        wins = [t["r_multiple"] for t in self.trade_r_history if t["r_multiple"] > 0]
        losses = [abs(t["r_multiple"]) for t in self.trade_r_history if t["r_multiple"] <= 0]

        total_trades = len(self.trade_r_history)
        win_count = len(wins)
        loss_count = len(losses)

        win_rate = (win_count / total_trades) if total_trades > 0 else 0.0
        loss_rate = 1.0 - win_rate

        avg_win_r = (sum(wins) / win_count) if win_count > 0 else 0.0
        avg_loss_r = (sum(losses) / loss_count) if loss_count > 0 else 0.0

        # Expectancy = (Win% * Avg Win R) - (Loss% * Avg Loss R)
        expectancy_r = (win_rate * avg_win_r) - (loss_rate * avg_loss_r)

        # Profit Factor = Gross R Wins / Gross R Losses
        profit_factor = (sum(wins) / sum(losses)) if sum(losses) > 0 else (99.0 if sum(wins) > 0 else 1.0)

        return {
            "total_trades": total_trades,
            "win_rate_pct": round(win_rate * 100.0, 1),
            "total_r": round(sum(t["r_multiple"] for t in self.trade_r_history), 2),
            "avg_win_r": round(avg_win_r, 2),
            "avg_loss_r": round(avg_loss_r, 2),
            "expectancy_r": round(expectancy_r, 2),
            "profit_factor": round(profit_factor, 2),
            "recent_r_multiples": [t["r_multiple"] for t in self.trade_r_history[-10:]]
        }

    # =========================================================================
    # 3. POSITION LIMIT CHECKS & MARGIN SIZING
    # =========================================================================

    async def evaluate_and_reserve_capital(
        self,
        asset: str,
        entry_price: float,
        fee_per_contract: float,
        max_allocation_pct: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Thread-safe capital evaluation and reservation under asyncio.Lock().
        Enforces:
        1. Global daily stop loss circuit breaker (10%)
        2. Max concurrent positions (2)
        3. Portfolio-wide exposure cap (40%)
        4. Single-asset allocation cap (20%)
        5. Free unreserved margin availability
        """
        coin = asset.upper()
        contract_cost = entry_price + fee_per_contract
        if contract_cost <= 0:
            return {
                "approved": False,
                "reason": "Invalid contract cost <= 0",
                "target_contracts": 0,
                "budget": 0.0
            }

        # --- Check 0: Emergency Kill Switch State ---
        if kill_switch is not None:
            tripped, ks_reason = kill_switch.is_tripped()
            if tripped:
                return {
                    "approved": False,
                    "reason": f"Emergency kill switch active: {ks_reason}",
                    "target_contracts": 0,
                    "budget": 0.0
                }

        # --- Check 1: Global Daily Stop Loss Circuit Breaker ---
        daily_dd_pct = self.get_daily_drawdown_pct()
        if daily_dd_pct >= self.global_stop_loss_daily_pct:
            if kill_switch is not None:
                kill_switch.trip_sync(f"Global daily stop loss tripped ({daily_dd_pct:.1f}% >= {self.global_stop_loss_daily_pct:.1f}%)")
            return {
                "approved": False,
                "reason": f"Global daily stop loss tripped ({daily_dd_pct:.1f}% >= {self.global_stop_loss_daily_pct:.1f}%)",
                "target_contracts": 0,
                "budget": 0.0
            }

        # --- Check 2: Max Concurrent Positions Cap ---
        active_count = len(self.active_positions) + len(self._pending_reservations)
        if active_count >= self.max_concurrent_positions and coin not in self.active_positions and coin not in self._pending_reservations:
            return {
                "approved": False,
                "reason": f"Max concurrent positions reached ({active_count}/{self.max_concurrent_positions})",
                "target_contracts": 0,
                "budget": 0.0
            }

        # --- Check 3: Optimistic Margin Availability ---
        estimated_free_cash = self.total_cash - self.allocated_capital
        if estimated_free_cash < contract_cost:
            return {
                "approved": False,
                "reason": f"Insufficient unreserved margin (${max(0.0, estimated_free_cash):.2f} < ${contract_cost:.2f})",
                "target_contracts": 0,
                "budget": 0.0
            }

        # --- PHASE 2: Atomic Critical Section (Zero I/O inside lock) ---
        async with self._margin_lock:
            if kill_switch is not None:
                tripped, ks_reason = kill_switch.is_tripped()
                if tripped:
                    return {
                        "approved": False,
                        "reason": f"Emergency kill switch active: {ks_reason}",
                        "target_contracts": 0,
                        "budget": 0.0
                    }

            # Re-verify daily stop loss under lock
            daily_dd_pct = self.get_daily_drawdown_pct()
            if daily_dd_pct >= self.global_stop_loss_daily_pct:
                if kill_switch is not None:
                    kill_switch.trip_sync(f"Global daily stop loss tripped ({daily_dd_pct:.1f}% >= {self.global_stop_loss_daily_pct:.1f}%)")
                return {
                    "approved": False,
                    "reason": f"Global daily stop loss tripped ({daily_dd_pct:.1f}% >= {self.global_stop_loss_daily_pct:.1f}%)",
                    "target_contracts": 0,
                    "budget": 0.0
                }

            # Re-verify concurrent limit
            active_count = len(self.active_positions) + len(self._pending_reservations)
            if active_count >= self.max_concurrent_positions and coin not in self.active_positions and coin not in self._pending_reservations:
                return {
                    "approved": False,
                    "reason": f"Max concurrent positions reached ({active_count}/{self.max_concurrent_positions})",
                    "target_contracts": 0,
                    "budget": 0.0
                }

            alloc_pct = max_allocation_pct or self.max_single_trade_pct

            # 1. Portfolio-wide exposure cap (40%)
            max_portfolio_exposure = self.total_equity * (self.max_portfolio_allocation_pct / 100.0)
            available_portfolio_capacity = max(0.0, max_portfolio_exposure - self.allocated_capital)

            # 2. Single-asset allocation cap (20%)
            single_asset_limit = self.total_equity * (alloc_pct / 100.0)
            current_asset_alloc = self.asset_allocations.get(coin, 0.0)
            available_asset_capacity = max(0.0, single_asset_limit - current_asset_alloc)

            # 3. Available unreserved cash buffer
            free_cash = max(0.0, self.total_cash - self.allocated_capital - 0.20)

            budget = min(available_asset_capacity, available_portfolio_capacity, free_cash)

            if budget < contract_cost or contract_cost <= 0:
                reason = f"Insufficient unreserved margin (${budget:.2f} < ${contract_cost:.2f})"
                if available_portfolio_capacity < contract_cost:
                    reason = f"Max portfolio allocation capacity reached ({self.max_portfolio_allocation_pct}%)"
                elif available_asset_capacity < contract_cost:
                    reason = f"Single asset allocation capacity reached for {coin} ({alloc_pct}%)"

                return {
                    "approved": False,
                    "reason": reason,
                    "target_contracts": 0,
                    "budget": 0.0
                }

            target_contracts = int(budget / contract_cost)
            if target_contracts < 1:
                return {
                    "approved": False,
                    "reason": "Target contracts rounded to 0",
                    "target_contracts": 0,
                    "budget": 0.0
                }

            total_reserve = target_contracts * contract_cost
            self.allocated_capital += total_reserve
            self._pending_reservations.add(coin)

            return {
                "approved": True,
                "asset": coin,
                "target_contracts": target_contracts,
                "budget": round(budget, 2),
                "total_reserve": round(total_reserve, 2),
                "entry_price": entry_price
            }

    async def commit_fill(self, asset: str, position_data: Dict[str, Any]):
        """Commit confirmed execution fill into active position tracking under per-asset lock."""
        coin = asset.upper()
        asset_lock = self._get_asset_lock(coin)
        async with asset_lock:
            async with self._margin_lock:
                self._pending_reservations.discard(coin)
                cost = position_data.get("cost_dollars", 0.0) + position_data.get("fee_dollars", 0.0)
                self.active_positions[coin] = position_data
                self.asset_allocations[coin] = cost

    async def release_reservation(self, asset: str, reserved_amount: float):
        """Release temporary reserved capital if an order was canceled or unfilled."""
        coin = asset.upper()
        async with self._margin_lock:
            self._pending_reservations.discard(coin)
            self.allocated_capital = max(0.0, self.allocated_capital - reserved_amount)

    async def release_position(
        self,
        asset: str,
        payout_dollars: float = 0.0,
        exit_reason: str = "EXIT"
    ):
        """Close active position, adjust live capital, and record R-multiple."""
        coin = asset.upper()
        asset_lock = self._get_asset_lock(coin)
        async with asset_lock:
            async with self._margin_lock:
                pos = self.active_positions.pop(coin, None)
                alloc = self.asset_allocations.pop(coin, 0.0)
                self.allocated_capital = max(0.0, self.allocated_capital - alloc)
                self.total_cash += payout_dollars
                self.total_equity = self.total_cash + sum(
                    p.get("cost_dollars", 0.0) for p in self.active_positions.values()
                )

                # Record R-multiple if position data was tracked
                if pos:
                    cost = pos.get("cost_dollars", alloc)
                    fee = pos.get("fee_dollars", 0.0)
                    total_in = cost + fee
                    net_pnl = payout_dollars - total_in
                    # 1R = position risk at 25% stop loss or total in
                    one_r = total_in * 0.25
                    self.record_trade_result(
                        asset=coin,
                        net_pnl=net_pnl,
                        initial_risk_dollars=one_r,
                        contracts=pos.get("contracts", 1),
                        exit_reason=exit_reason
                    )

    async def sync_balance(self, http_client: Any) -> float:
        """Fetch confirmed live balance directly from Kalshi V2 REST endpoint."""
        now = time.time()
        if not self.auth or not self.auth.is_configured:
            return self.total_cash

        endpoint = "/portfolio/balance"
        headers = self.auth.sign_request("GET", f"/trade-api/v2{endpoint}")
        try:
            resp = await http_client.get(f"{self.rest_url}{endpoint}", headers=headers, timeout=3.5)
            if resp.status_code == 200:
                data = resp.json()
                bal_cents = data.get("balance", 0)
                async with self._margin_lock:
                    self.total_cash = float(bal_cents) / 100.0
                    self.total_equity = self.total_cash + sum(
                        p.get("cost_dollars", 0.0) for p in self.active_positions.values()
                    )
                    self._last_sync_ts = now
                logger.debug(f"[PortfolioManager] Synced Live Balance: ${self.total_cash:.2f}")
        except Exception as e:
            logger.warning(f"[PortfolioManager] Failed to sync balance: {e}")

        return self.total_cash

    # =========================================================================
    # 4. HEDGING RECOMMENDATIONS & RISK DASHBOARD
    # =========================================================================

    def get_hedging_recommendations(self) -> List[Dict[str, Any]]:
        """
        Generate systematic hedging actions when approaching the 10% daily stop loss.
        Warning trigger begins at 70% of limit (7.0% drawdown).
        """
        daily_dd_pct = self.get_daily_drawdown_pct()
        limit_pct = self.global_stop_loss_daily_pct
        utilization_pct = (daily_dd_pct / limit_pct) * 100.0 if limit_pct > 0 else 0.0

        recommendations = []

        if daily_dd_pct >= limit_pct:
            recommendations.append({
                "severity": "CRITICAL",
                "action": "EMERGENCY_CIRCUIT_BREAKER_HALT",
                "details": f"Daily loss ({daily_dd_pct:.1f}%) has met or exceeded the 10.0% limit. All trading halted. Cancel all resting limit orders."
            })
            return recommendations

        if utilization_pct >= 70.0:  # >= 7.0% drawdown
            recommendations.append({
                "severity": "HIGH",
                "action": "THROTTLE_POSITION_SIZING",
                "details": f"Daily loss is {daily_dd_pct:.1f}% (70%+ of 10% limit). Scale max single-asset allocation down from 20% to 10%."
            })
            recommendations.append({
                "severity": "HIGH",
                "action": "TIGHTEN_EXIT_THRESHOLDS",
                "details": "Lower take-profit threshold to +12% ROI to lock in quick scalps, and tighten stop-loss to -15% ROI."
            })
            if self.active_positions:
                for asset, pos in self.active_positions.items():
                    side = pos.get("side", "YES")
                    opp_side = "NO" if side == "YES" else "YES"
                    recommendations.append({
                        "severity": "MEDIUM",
                        "action": f"DELTA_HEDGE_{asset}",
                        "details": f"Open {side} position on {asset}. Consider purchasing low-delta {opp_side} contracts (< 15¢) on {asset} or correlated assets to hedge delta exposure."
                    })
        elif utilization_pct >= 50.0:  # >= 5.0% drawdown
            recommendations.append({
                "severity": "LOW",
                "action": "MONITOR_INCREASED_VOLATILITY",
                "details": f"Daily loss is {daily_dd_pct:.1f}%. Enforce strict 4.0% minimum edge filter and reject trades with conviction < 65%."
            })

        return recommendations

    def get_risk_dashboard(self) -> Dict[str, Any]:
        """
        Institutional Risk Dashboard Summary for FastAPI /app.py integration.
        """
        daily_dd = self.get_daily_drawdown_pct()
        is_halted = (daily_dd >= self.global_stop_loss_daily_pct)
        status = "HALTED_CIRCUIT_BREAKER" if is_halted else ("WARNING_HIGH_RISK" if daily_dd >= 7.0 else "HEALTHY")

        portfolio_utilization_pct = (self.allocated_capital / max(0.01, self.total_equity)) * 100.0

        return {
            "status": status,
            "timestamp": time.time(),
            "global_caps": {
                "max_portfolio_allocation_pct": self.max_portfolio_allocation_pct,
                "max_single_asset_allocation_pct": self.max_single_trade_pct,
                "max_concurrent_positions": self.max_concurrent_positions,
                "global_stop_loss_daily_pct": self.global_stop_loss_daily_pct,
            },
            "portfolio_state": {
                "total_cash": round(self.total_cash, 2),
                "total_equity": round(self.total_equity, 2),
                "day_start_equity": round(self.day_start_equity, 2),
                "allocated_capital": round(self.allocated_capital, 2),
                "portfolio_utilization_pct": round(portfolio_utilization_pct, 1),
                "available_cash": round(max(0.0, self.total_cash - self.allocated_capital), 2),
                "daily_drawdown_dollars": round(max(0.0, self.day_start_equity - self.total_equity), 2),
                "daily_drawdown_pct": round(daily_dd, 2),
                "active_positions_count": len(self.active_positions),
                "active_assets": list(self.active_positions.keys())
            },
            "per_asset_drawdowns": self.get_asset_drawdowns(),
            "r_multiple_metrics": self.get_r_multiple_stats(),
            "hedging_recommendations": self.get_hedging_recommendations()
        }

    def get_status_dict(self) -> Dict[str, Any]:
        """Legacy status summary dictionary."""
        return {
            "total_cash": round(self.total_cash, 2),
            "total_equity": round(self.total_equity, 2),
            "allocated_capital": round(self.allocated_capital, 2),
            "available_cash": round(max(0.0, self.total_cash - self.allocated_capital), 2),
            "active_positions_count": len(self.active_positions),
            "active_assets": list(self.active_positions.keys()),
            "daily_drawdown_pct": round(self.get_daily_drawdown_pct(), 2)
        }
