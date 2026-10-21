"""
core/portfolio_manager.py — Multi-Asset Centralized Portfolio & Risk Manager.

Coordinates account buying power, exchange collateral partitioning, and concurrent trade sizing
across multiple assets (BTC, ETH, SOL, XRP) with asynchronous serialization locks to guarantee
zero margin rejections or over-allocation.
"""

import time
import asyncio
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger("PortfolioManager")


class PortfolioManager:
    """
    Centralized Capital Allocator & Concurrency Guard for Kalshi Multi-Asset Trading.
    """

    def __init__(
        self,
        auth: Any,
        rest_url: str,
        initial_balance: float = 50.0,
        max_portfolio_allocation_pct: float = 40.0,
        max_single_trade_pct: float = 20.0,
        max_concurrent_positions: int = 2
    ):
        self.auth = auth
        self.rest_url = rest_url
        self.total_cash: float = initial_balance
        self.total_equity: float = initial_balance
        self.allocated_capital: float = 0.0
        self.max_portfolio_allocation_pct = max_portfolio_allocation_pct
        self.max_single_trade_pct = max_single_trade_pct
        self.max_concurrent_positions = max_concurrent_positions

        # Asset allocations and active position trackers
        self.active_positions: Dict[str, Dict[str, Any]] = {}
        self.asset_allocations: Dict[str, float] = {}
        self._pending_reservations: set = set()

        # Concurrency Lock preventing race conditions on simultaneous signals
        self._lock = asyncio.Lock()
        self._last_sync_ts = 0.0

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
                async with self._lock:
                    self.total_cash = float(bal_cents) / 100.0
                    self.total_equity = self.total_cash + sum(
                        p.get("cost_dollars", 0.0) for p in self.active_positions.values()
                    )
                    self._last_sync_ts = now
                logger.debug(f"[PortfolioManager] Synced Live Balance: ${self.total_cash:.2f}")
        except Exception as e:
            logger.warning(f"[PortfolioManager] Failed to sync balance: {e}")

        return self.total_cash

    async def evaluate_and_reserve_capital(
        self,
        asset: str,
        entry_price: float,
        fee_per_contract: float,
        max_allocation_pct: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Thread-safe capital evaluation and reservation under asyncio.Lock().
        Prevents simultaneous signals from BTC and SOL from double-spending available margin.
        """
        async with self._lock:
            alloc_pct = max_allocation_pct or self.max_single_trade_pct

            # 1. Concurrency limit check
            active_count = len(self.active_positions) + len(self._pending_reservations)
            if active_count >= self.max_concurrent_positions and asset not in self.active_positions and asset not in self._pending_reservations:
                return {
                    "approved": False,
                    "reason": f"Max concurrent positions reached ({active_count}/{self.max_concurrent_positions})",
                    "target_contracts": 0,
                    "budget": 0.0
                }

            # 2. Portfolio-wide exposure cap
            max_portfolio_exposure = self.total_equity * (self.max_portfolio_allocation_pct / 100.0)
            available_portfolio_capacity = max(0.0, max_portfolio_exposure - self.allocated_capital)

            # 3. Single-asset budget
            single_trade_cap = self.total_equity * (alloc_pct / 100.0)
            free_cash = max(0.0, self.total_cash - self.allocated_capital - 0.20)

            budget = min(single_trade_cap, available_portfolio_capacity, free_cash)
            contract_cost = entry_price + fee_per_contract

            if budget < contract_cost or contract_cost <= 0:
                return {
                    "approved": False,
                    "reason": f"Insufficient unreserved margin (${budget:.2f} < ${contract_cost:.2f})",
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
            self._pending_reservations.add(asset)

            return {
                "approved": True,
                "asset": asset,
                "target_contracts": target_contracts,
                "budget": round(budget, 2),
                "total_reserve": round(total_reserve, 2),
                "entry_price": entry_price
            }

    async def commit_fill(self, asset: str, position_data: Dict[str, Any]):
        """Commit confirmed execution fill into active position tracking."""
        async with self._lock:
            self._pending_reservations.discard(asset)
            cost = position_data.get("cost_dollars", 0.0) + position_data.get("fee_dollars", 0.0)
            self.active_positions[asset] = position_data
            self.asset_allocations[asset] = cost

    async def release_reservation(self, asset: str, reserved_amount: float):
        """Release temporary reserved capital if an order was canceled or unfilled."""
        async with self._lock:
            self._pending_reservations.discard(asset)
            self.allocated_capital = max(0.0, self.allocated_capital - reserved_amount)

    async def release_position(self, asset: str, payout_dollars: float = 0.0):
        """Close active position and adjust live capital."""
        async with self._lock:
            pos = self.active_positions.pop(asset, None)
            alloc = self.asset_allocations.pop(asset, 0.0)
            self.allocated_capital = max(0.0, self.allocated_capital - alloc)
            self.total_cash += payout_dollars
            self.total_equity = self.total_cash + sum(
                p.get("cost_dollars", 0.0) for p in self.active_positions.values()
            )

    def get_status_dict(self) -> Dict[str, Any]:
        """Return snapshot summary of portfolio manager state."""
        return {
            "total_cash": round(self.total_cash, 2),
            "total_equity": round(self.total_equity, 2),
            "allocated_capital": round(self.allocated_capital, 2),
            "available_cash": round(max(0.0, self.total_cash - self.allocated_capital), 2),
            "active_positions_count": len(self.active_positions),
            "active_assets": list(self.active_positions.keys())
        }
