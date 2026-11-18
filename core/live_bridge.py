"""
core/live_bridge.py — Safe Multi-Coin Paper-to-Live OMS Execution Bridge.

Routes validated multi-coin signals from `multi_coin_production_loop` in `app.py`
to `KalshiOrderManagementSystem` (`live_oms`) ONLY when `KALSHI_LIVE_TRADING=1`
(`live_oms.dry_run is False`).

Safety Guarantees:
1. Complete no-op when `live_oms.dry_run` is True (Paper Mode).
2. Checks `kill_switch.is_tripped()` and `live_oms.blocked_by_orphans` before every entry.
3. Enforces `PortfolioManager.evaluate_and_reserve_capital(...)` under asyncio.Lock.
4. Enforces conservative initial live collateral cap (`KALSHI_LIVE_MAX_TRADE_DOLLARS`, default $25)
   and contract cap (`KALSHI_LIVE_MAX_CONTRACTS`, default 50).
5. Strictly enforces the 35¢ normal / 78¢ late-candle certainty-arb VWAP ceiling.
6. Releases capital reservations cleanly on any abort, rejection, or zero-fill.
7. Routes exits through `live_oms.execute_complete_exit(...)` (or automatic expiration settlement)
   and logs live vs. paper fill slippage into `BlueGreenComparator` when provided.
"""

import os
import time
import logging
from typing import Any, Dict, Optional

try:
    from core.prediction_engine import calculate_kalshi_fee as calculate_kalshi_taker_fee
except ImportError:
    from .prediction_engine import calculate_kalshi_fee as calculate_kalshi_taker_fee

try:
    from core.kill_switch import kill_switch
except ImportError:
    kill_switch = None

logger = logging.getLogger("LiveMultiCoinBridge")


class LiveMultiCoinBridge:
    """Bridges multi-coin entry/exit signals to KalshiOrderManagementSystem when armed."""

    def __init__(
        self,
        oms: Any,
        portfolio_manager: Any,
        comparator: Optional[Any] = None,
        live_max_allocation_pct: Optional[float] = None,
        max_live_trade_dollars: Optional[float] = None,
        max_live_contracts: Optional[int] = None,
        normal_max_price_cents: int = 35,
        certainty_max_price_cents: int = 78,
    ):
        self.oms = oms
        self.portfolio_manager = portfolio_manager
        self.comparator = comparator
        self.live_max_allocation_pct = (
            float(live_max_allocation_pct)
            if live_max_allocation_pct is not None
            else float(os.getenv("KALSHI_LIVE_MAX_ALLOC_PCT", "15.0"))
        )
        self.max_live_trade_dollars = (
            float(max_live_trade_dollars)
            if max_live_trade_dollars is not None
            else float(os.getenv("KALSHI_LIVE_MAX_TRADE_DOLLARS", "25.0"))
        )
        self.max_live_contracts = (
            int(max_live_contracts)
            if max_live_contracts is not None
            else int(os.getenv("KALSHI_LIVE_MAX_CONTRACTS", "50"))
        )
        self.normal_max_price_cents = int(normal_max_price_cents)
        self.certainty_max_price_cents = int(certainty_max_price_cents)
        self.live_positions: Dict[str, Dict[str, Any]] = {}
        self._last_balance_sync_ts: float = 0.0

    @property
    def is_live_armed(self) -> bool:
        return bool(self.oms is not None and not getattr(self.oms, "dry_run", True))

    async def on_entry(
        self,
        coin: str,
        ticker: str,
        side: str,
        entry_price: float,
        model_prob_pct: float,
        is_certainty_arb: bool = False,
        http_client: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Mirror a validated entry signal onto the live Kalshi OMS if armed."""
        if not self.is_live_armed:
            return {"status": "PAPER_ONLY", "reason": "OMS dry_run=True"}

        coin = coin.upper()
        side = side.upper()

        if coin in self.live_positions:
            return {"status": "SKIPPED_ALREADY_OPEN", "reason": f"Live position already open for {coin}"}

        if kill_switch is not None:
            tripped, ks_reason = kill_switch.is_tripped()
            if tripped:
                logger.warning(f"[LiveBridge] Blocked {coin} entry: kill switch tripped ({ks_reason})")
                return {"status": "ABORTED_KILL_SWITCH", "reason": ks_reason}

        if getattr(self.oms, "blocked_by_orphans", False):
            logger.error(f"[LiveBridge] Blocked {coin} entry: OMS blocked_by_orphans=True")
            return {"status": "ABORTED_ORPHAN_ORDERS", "reason": "OMS blocked by untracked resting orders"}

        # Sync live cash from exchange if stale (>30s)
        now = time.time()
        if http_client is not None and (now - self._last_balance_sync_ts) > 30.0:
            try:
                await self.portfolio_manager.sync_balance(http_client)
                self._last_balance_sync_ts = now
            except Exception as e:
                logger.warning(f"[LiveBridge] Balance sync warning before {coin} entry: {e}")

        fee_per_contract = calculate_kalshi_taker_fee(1, entry_price) if 0.0 < entry_price < 1.0 else 0.0
        reserve = await self.portfolio_manager.evaluate_and_reserve_capital(
            asset=coin,
            entry_price=entry_price,
            fee_per_contract=fee_per_contract,
            max_allocation_pct=self.live_max_allocation_pct,
        )
        if not reserve.get("approved"):
            logger.info(f"[LiveBridge] Skipped live {coin} entry: {reserve.get('reason')}")
            return {"status": "SKIPPED_RISK", "reason": reserve.get("reason")}

        reserved_budget = float(reserve.get("budget", 0.0))
        target_contracts = int(reserve.get("target_contracts", 0))

        # Apply conservative live dollar & contract caps
        unit_cost = max(0.01, entry_price + fee_per_contract)
        dollar_cap_contracts = int(self.max_live_trade_dollars / unit_cost)
        contracts = min(target_contracts, dollar_cap_contracts, self.max_live_contracts)
        if contracts < 1:
            await self.portfolio_manager.release_reservation(coin, reserved_budget)
            return {"status": "SKIPPED_TOO_SMALL", "reason": f"Capped live size < 1 contract (budget=${reserved_budget:.2f})"}

        # Strictly enforce 35¢ normal / 78¢ certainty-arb VWAP ceiling
        ceiling_cents = self.certainty_max_price_cents if is_certainty_arb else self.normal_max_price_cents
        max_vwap_cents = min(int(round(entry_price * 100)) + 2, ceiling_cents)
        if int(round(entry_price * 100)) > ceiling_cents:
            await self.portfolio_manager.release_reservation(coin, reserved_budget)
            return {
                "status": "SKIPPED_PRICE_CAP",
                "reason": f"Entry price {int(round(entry_price*100))}¢ > {ceiling_cents}¢ cap",
            }

        try:
            res = await self.oms.execute_entry_with_vwap_protection(
                ticker=ticker,
                side=side,
                contracts=contracts,
                model_prob=model_prob_pct / 100.0,
                max_acceptable_vwap_cents=max_vwap_cents,
                min_net_edge_pct=3.0 if is_certainty_arb else 4.0,
            )
        except Exception as e:
            logger.error(f"[LiveBridge] OMS entry exception for {coin} {ticker}: {e}")
            await self.portfolio_manager.release_reservation(coin, reserved_budget)
            return {"status": "ERROR", "reason": str(e)}

        if res.get("status") == "FILLED" and int(res.get("filled_count", 0)) > 0:
            filled = int(res["filled_count"])
            fill_cents = int(res.get("fill_price_cents", int(round(entry_price * 100))))
            fill_price = fill_cents / 100.0
            cost_dollars = filled * fill_price
            fee_dollars = calculate_kalshi_taker_fee(filled, fill_price)
            live_pos = {
                "coin": coin,
                "ticker": ticker,
                "side": side,
                "contracts": filled,
                "entry_price": fill_price,
                "cost_dollars": cost_dollars,
                "fee_dollars": fee_dollars,
                "order_id": res.get("order_id"),
                "entry_ts": time.time(),
            }
            # Adjust reservation delta if actual fill cost < reserved_budget
            actual_total = cost_dollars + fee_dollars
            if reserved_budget > actual_total:
                await self.portfolio_manager.release_reservation(coin, reserved_budget - actual_total)
            await self.portfolio_manager.commit_fill(coin, live_pos)
            self.live_positions[coin] = live_pos
            logger.info(
                f"[LiveBridge] LIVE FILLED {coin} {side} {filled}x @ {fill_cents}¢ on {ticker} "
                f"(order_id={res.get('order_id')}, cost=${cost_dollars:.2f}, fee=${fee_dollars:.2f})"
            )
            return {"status": "FILLED", "position": live_pos, "oms": res}

        await self.portfolio_manager.release_reservation(coin, reserved_budget)
        logger.warning(f"[LiveBridge] Live {coin} entry not filled ({res.get('status')}): {res.get('reason')}")
        return res

    async def on_exit(
        self,
        coin: str,
        exit_reason: str,
        current_bid: float,
        paper_trade: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Close an open live position corresponding to a paper exit signal."""
        if not self.is_live_armed:
            return {"status": "PAPER_ONLY", "reason": "OMS dry_run=True"}

        coin = coin.upper()
        live_pos = self.live_positions.get(coin)
        if not live_pos:
            return {"status": "NO_LIVE_POSITION"}

        # Expiration settlements settle automatically on Kalshi ($1.00 or $0.00)
        if "SETTLED" in str(exit_reason).upper():
            is_win = "WIN" in str(exit_reason).upper()
            payout = float(live_pos["contracts"]) * (1.0 if is_win else 0.0)
            await self.portfolio_manager.release_position(coin, payout_dollars=payout, exit_reason=exit_reason)
            self.live_positions.pop(coin, None)
            logger.info(f"[LiveBridge] LIVE SETTLED {coin} ({exit_reason}): est payout=${payout:.2f}")
            return {"status": "SETTLED", "payout_dollars": payout}

        bid_cents = max(1, min(99, int(round(current_bid * 100))))
        floor_cents = 5 if "STOP" in str(exit_reason).upper() else max(10, bid_cents - 5)

        try:
            exit_res = await self.oms.execute_complete_exit(
                ticker=live_pos["ticker"],
                side=live_pos["side"],
                contracts=live_pos["contracts"],
                target_price_cents=bid_cents,
                floor_cents=floor_cents,
                reason=exit_reason,
            )
        except Exception as e:
            logger.error(f"[LiveBridge] OMS exit exception for {coin} {live_pos['ticker']}: {e}")
            return {"status": "ERROR", "reason": str(e)}

        avg_exit_cents = float(exit_res.get("avg_exit_price_cents", bid_cents))
        filled_exit = int(exit_res.get("filled_count", live_pos["contracts"]))
        proceeds = (filled_exit * avg_exit_cents) / 100.0

        await self.portfolio_manager.release_position(coin, payout_dollars=proceeds, exit_reason=exit_reason)
        self.live_positions.pop(coin, None)

        if self.comparator is not None and paper_trade is not None:
            try:
                paper_fill = float(paper_trade.get("exit_price", current_bid))
                self.comparator.record_comparison(
                    ticker=live_pos["ticker"],
                    paper_price=paper_fill,
                    live_price=avg_exit_cents / 100.0,
                    contracts=filled_exit,
                )
            except Exception:
                pass

        logger.info(
            f"[LiveBridge] LIVE EXITED {coin} ({exit_reason}): {filled_exit}x @ {avg_exit_cents:.1f}¢ "
            f"(proceeds=${proceeds:.2f})"
        )
        return {"status": "EXITED", "proceeds_dollars": proceeds, "oms": exit_res}
