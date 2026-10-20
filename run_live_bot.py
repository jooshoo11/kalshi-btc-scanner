#!/usr/bin/env python3
"""
run_live_bot.py — Live Kalshi Bitcoin Quantitative Execution Bot (Demo Environment).

Architecture:
1. Cold-Boot State Reconciliation:
   - Pre-flight sync runs BEFORE WebSocket connection.
   - Cancels all orphaned resting orders on exchange.
   - Reconciles exchange positions with SQLite kalshi_audit.db.
   - Confirms live balance.
2. WebSocket Streaming & OMS:
   - Real-time L2 orderbook_delta cache for KXBTC15M contracts.
   - Stale-Tick Latency & Heartbeat Watchdog (<1500ms).
3. Scalping Loop:
   - 1-Second tick loop aggregating Hyperliquid spot and Kalshi L2 depth.
   - Quantitative Drift-Diffusion & BRTI 60-Second Accumulator pricing.
   - Net Post-Fee Half-Kelly position sizing with $75 hard cap and 25¢-78¢ price bounds.
   - OMS VWAP depth sweeps with VWAP_SLIPPAGE_ABORT safety gate.
   - Limit-Chasing Stop Loss routing (-25% SL / +25% TP).
"""

import os
import sys
import time
import math
import signal
import asyncio
import logging
from typing import Dict, Any, Optional, List

from dotenv import load_dotenv

# Load credentials from .env
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import httpx

from live_executor import (
    KALSHI_DEMO_REST_URL,
    KALSHI_DEMO_WS_URL,
    KalshiAuth,
    KalshiWebSocketClient,
    KalshiOrderManagementSystem,
    StaleTickWatchdog,
    L2OrderBook
)
from prediction_engine import QuantPredictionEngine, calculate_kalshi_fee
from btc_metrics import BitcoinMetricsProvider
from bot_database import AuditDatabase

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [LiveBot] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("LiveBot")


class KalshiLiveScalper:
    def __init__(
        self,
        auth: Optional[KalshiAuth] = None,
        rest_url: str = KALSHI_DEMO_REST_URL,
        ws_url: str = KALSHI_DEMO_WS_URL,
        db_path: str = "kalshi_audit.db",
        dry_run: bool = False
    ):
        self.auth = auth or KalshiAuth.from_env()
        self.rest_url = rest_url
        self.ws_url = ws_url
        self.db_path = db_path
        self.dry_run = dry_run

        self.db = AuditDatabase(db_path=db_path)
        self.watchdog = StaleTickWatchdog(max_allowed_delta_ms=1500.0)
        self.metrics_provider = BitcoinMetricsProvider(watchdog=self.watchdog)
        self.engine = QuantPredictionEngine()

        self.ws_client = KalshiWebSocketClient(ws_url=self.ws_url, auth=self.auth)
        self.oms = KalshiOrderManagementSystem(
            auth=self.auth,
            rest_url=self.rest_url,
            ws_client=self.ws_client,
            watchdog=self.watchdog,
            dry_run=self.dry_run
        )

        # Risk parameters
        self.min_edge_pct = 4.0
        self.take_profit_roi_pct = 25.0
        self.stop_loss_roi_pct = 25.0
        self.max_trade_dollars = 75.0
        self.min_entry_price = 0.25
        self.max_entry_price = 0.78
        self.max_allocation_pct = 0.15

        # Active state
        self.is_running = False
        self.current_ticker = ""
        self.current_candle_id = ""
        self.active_position: Optional[Dict[str, Any]] = None
        self.trades_in_candle = 0
        self.last_exit_reason = ""
        self.last_exit_ts = 0.0

        # Account equity cache
        self.cash = 50.0
        self.total_equity = 50.0

    async def initialize_and_reconcile(self):
        """Pre-flight sequence: reconciles state and syncs exchange before starting WS."""
        logger.info("=" * 80)
        logger.info("       INITIALIZING KALSHI LIVE SCALPER (DEMO ENVIRONMENT)")
        logger.info("=" * 80)

        if not self.auth.is_configured:
            logger.error("Authentication credentials not configured! Please check .env file.")
            sys.exit(1)

        logger.info(f"Using Kalshi API Key ID: {self.auth.key_id}")
        logger.info(f"Target REST Endpoint: {self.rest_url}")
        logger.info(f"Target WebSocket Endpoint: {self.ws_url}")

        # 1. Fetch live balance
        async with httpx.AsyncClient(timeout=5.0) as client:
            headers = self.auth.sign_request("GET", "/trade-api/v2/portfolio/balance")
            resp = await client.get(f"{self.rest_url}/portfolio/balance", headers=headers)
            if resp.status_code == 200:
                bal_data = resp.json()
                bal_cents = bal_data.get("balance", 0)
                self.cash = float(bal_cents) / 100.0
                self.total_equity = self.cash
                logger.info(f"✓ Confirmed Live Exchange Balance: ${self.cash:.2f} ({bal_cents} cents)")
            else:
                logger.warning(f"Could not fetch balance ({resp.status_code}): {resp.text}")

        # 2. Execute Cold-Boot State Reconciliation
        logger.info("Running cold-boot state reconciliation...")
        reconcile_report = await self.oms.reconcile_state_on_startup(
            db_path_or_db=self.db,
            cancel_resting_orders=True
        )
        logger.info(
            f"✓ Cold-Boot Reconciliation Finished: "
            f"{len(reconcile_report.get('resting_orders_canceled', []))} resting orders canceled, "
            f"{len(reconcile_report.get('discrepancies_found', []))} discrepancies resolved."
        )

    async def resolve_active_kxbtc_market(self, http_client: httpx.AsyncClient) -> Optional[Dict[str, Any]]:
        """Queries Kalshi REST API for the currently active KXBTC15M contract."""
        endpoint = "/markets"
        params = {"series_ticker": "KXBTC15M", "status": "open", "limit": 5}
        headers = self.auth.sign_request("GET", f"/trade-api/v2{endpoint}") if self.auth.is_configured else {}

        try:
            resp = await http_client.get(f"{self.rest_url}{endpoint}", headers=headers, params=params, timeout=4.0)
            if resp.status_code == 200:
                markets = resp.json().get("markets", [])
                if markets:
                    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    active_future = [m for m in markets if (m.get("close_time") or "") >= now_iso]
                    if active_future:
                        active_future.sort(key=lambda m: m.get("close_time", ""))
                        return active_future[0]
                    else:
                        markets.sort(key=lambda m: m.get("close_time", ""), reverse=True)
                        return markets[0]
        except Exception as e:
            logger.debug(f"Error querying active markets: {e}")

        # Synthetic fallback ticker based on current timestamp
        now = int(time.time())
        candle_end_sec = ((now // 900) + 1) * 900
        gm = time.gmtime(candle_end_sec)
        time_str = time.strftime("%y%b%d%H%M", gm).upper()
        return {
            "ticker": f"KXBTC15M-{time_str}",
            "close_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", gm),
            "candle_open": self.metrics_provider.spot_price
        }

    async def start(self):
        """Starts background WebSocket listener and enters main scalping loop."""
        await self.initialize_and_reconcile()

        # Start WebSocket listener in background
        self.is_running = True
        ws_task = asyncio.create_task(self.ws_client.connect_and_listen())

        logger.info("Waiting for WebSocket connection...")
        for _ in range(10):
            if self.ws_client.is_ws_connected:
                break
            await asyncio.sleep(0.5)

        logger.info("Entering 1-Second Quantitative Scalping Loop...")
        async with httpx.AsyncClient(timeout=4.0) as http_client:
            while self.is_running:
                loop_start = time.time()
                try:
                    await self._scalp_tick(http_client)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Error in scalp tick: {e}", exc_info=True)

                elapsed = time.time() - loop_start
                sleep_time = max(0.05, 1.0 - elapsed)
                await asyncio.sleep(sleep_time)

        self.ws_client.stop()
        ws_task.cancel()
        try:
            await ws_task
        except asyncio.CancelledError:
            pass
        logger.info("Scalper shutdown complete.")

    async def _scalp_tick(self, http_client: httpx.AsyncClient):
        """Single 1-second iteration of the scalping loop."""
        # 1. Update spot feeds from Hyperliquid / Kraken
        await self.metrics_provider.update_subsecond_spot(http_client)
        spot = self.metrics_provider.spot_price

        # Periodically refresh candle open & volatility (every 10s)
        now = time.time()
        if not hasattr(self, "_last_candle_refresh") or now - self._last_candle_refresh > 10.0:
            await self.metrics_provider.update_candles_and_technicals(http_client)
            self._last_candle_refresh = now

        # 2. Resolve active 15-minute market
        mkt = await self.resolve_active_kxbtc_market(http_client)
        if not mkt:
            return

        ticker = mkt.get("ticker", "")
        close_time_iso = mkt.get("close_time", "")
        if ticker and ticker != self.current_ticker:
            logger.info(f"Targeting active KXBTC15M contract: {ticker}")
            self.current_ticker = ticker
            await self.ws_client.subscribe_ticker(ticker)

        # Candle timing
        now = time.time()
        candle_start = (int(now) // 900) * 900
        candle_end = candle_start + 900
        seconds_left = max(1.0, float(candle_end - now))

        if self.current_candle_id != close_time_iso:
            self.current_candle_id = close_time_iso
            self.trades_in_candle = 0
            self.last_exit_reason = ""
            self.metrics_provider.brti_accumulator.reset_for_candle(close_time_iso)

        candle_open = self.metrics_provider.current_15m_candle_open or spot

        # Add sample to BRTI accumulator during final 60s
        self.metrics_provider.brti_accumulator.add_sample(spot, seconds_left, candle_id=close_time_iso)
        accumulated_samples = self.metrics_provider.brti_accumulator.get_samples()

        # 3. Read L2 Book from local WebSocket cache
        book = self.ws_client.get_or_create_book(ticker)
        best_bid, bid_qty = book.get_best_bid()
        best_ask, ask_qty = book.get_best_ask()

        # If book cache is unpopulated yet, fall back to REST quotes
        if best_bid == 0 and best_ask == 100:
            raw_bid = mkt.get("yes_bid_dollars") or mkt.get("yes_bid") or mkt.get("previous_yes_bid_dollars")
            raw_ask = mkt.get("yes_ask_dollars") or mkt.get("yes_ask") or mkt.get("previous_yes_ask_dollars")
            y_bid = float(raw_bid) if raw_bid is not None and float(raw_bid) > 0 else 0.50
            y_ask = float(raw_ask) if raw_ask is not None and float(raw_ask) > 0 else 0.52
            if y_bid > 1.0:
                y_bid /= 100.0
            if y_ask > 1.0:
                y_ask /= 100.0
            if y_ask <= y_bid:
                y_ask = min(0.99, y_bid + 0.02)
            best_bid = int(round(y_bid * 100))
            best_ask = int(round(y_ask * 100))
            ask_size = max(1, int(float(mkt.get("yes_ask_size_fp") or 50)))
            bid_size = max(1, int(float(mkt.get("yes_bid_size_fp") or 50)))
            book.apply_snapshot([[best_bid, bid_size]], [[best_ask, ask_size]])
        else:
            y_bid = float(best_bid) / 100.0
            y_ask = float(best_ask) / 100.0
        n_bid = round(1.0 - y_ask, 2)
        n_ask = round(1.0 - y_bid, 2)

        # 4. Run Quantitative Drift-Diffusion / BRTI Model
        scalp_res = self.engine.evaluate_15m_scalp(
            spot=spot,
            open_price=candle_open,
            seconds_left=seconds_left,
            dollar_vol_15m=self.metrics_provider.vol_15m_dollar,
            accumulated_samples=accumulated_samples
        )
        model_prob = scalp_res["prob_yes"]
        trade_metrics = self.engine.calculate_trade_metrics(
            model_prob=model_prob,
            yes_ask=y_ask,
            yes_bid=y_bid,
            no_ask=n_ask,
            no_bid=n_bid
        )

        signal = trade_metrics.get("signal", "")
        best_edge_pct = float(trade_metrics.get("best_edge_pct", 0.0))
        half_kelly_pct = float(trade_metrics.get("half_kelly_pct", 0.0))
        best_edge = best_edge_pct / 100.0
        half_kelly_frac = half_kelly_pct / 100.0

        # -------------------------------------------------------------
        # STEP A: Manage Open Position
        # -------------------------------------------------------------
        if self.active_position:
            pos = self.active_position
            pos_side = pos["side"]
            entry_p = pos["entry_price"]
            curr_bid = y_bid if pos_side == "YES" else n_bid
            unrealized_pct = round(((curr_bid - entry_p) / max(0.01, entry_p)) * 100.0, 1)

            # Expiry Resolution
            if seconds_left <= 3:
                logger.info(f"[Position Resolution] Candle expiring. Settling {ticker} at final price...")
                is_win = (spot >= candle_open) if pos_side == "YES" else (spot < candle_open)
                pnl = (pos["contracts"] * (1.0 - entry_p)) if is_win else (-pos["cost_dollars"])
                self.cash += (pos["cost_dollars"] + pnl) if is_win else 0.0
                self.active_position = None
                self.last_exit_reason = "SETTLED_WIN" if is_win else "SETTLED_LOSS"
                self.last_exit_ts = time.time()
                logger.info(f"[Position Resolution] Result: {self.last_exit_reason} | PnL: ${pnl:.2f}")
                return

            # Take Profit (+25%)
            if unrealized_pct >= self.take_profit_roi_pct and (time.time() - pos["entry_ts"] >= 5.0):
                logger.info(f"[Take Profit] ROI +{unrealized_pct}% reached! Executing market exit...")
                exit_res = await self.oms._place_rest_order(
                    ticker=ticker,
                    action="sell",
                    side=pos_side.lower(),
                    count=pos["contracts"],
                    price_cents=int(round(curr_bid * 100)),
                    order_type="limit"
                )
                self.cash += pos["contracts"] * curr_bid
                self.active_position = None
                self.last_exit_reason = "TAKE_PROFIT"
                self.last_exit_ts = time.time()
                return

            # Limit-Chasing Stop Loss (-25%)
            if unrealized_pct <= -self.stop_loss_roi_pct:
                logger.info(f"[Stop Loss] ROI {unrealized_pct}% hit! Dispatching Limit-Chasing Stop Loss...")
                sl_res = await self.oms.execute_limit_chasing_stop_loss(
                    ticker=ticker,
                    side=pos_side,
                    contracts=pos["contracts"],
                    floor_cents=15
                )
                fill_p = float(sl_res.get("fill_price_cents", 15)) / 100.0
                self.cash += pos["contracts"] * fill_p
                self.active_position = None
                self.last_exit_reason = "STOP_LOSS"
                self.last_exit_ts = time.time()
                return

            return  # Still holding open trade

        # -------------------------------------------------------------
        # STEP B: Evaluate New Entry
        # -------------------------------------------------------------
        # Re-entry lockout on adverse candle
        if self.trades_in_candle >= 1 and self.last_exit_reason in ("STOP_LOSS", "SETTLED_LOSS"):
            return
        if time.time() - self.last_exit_ts < 8.0:
            return
        if seconds_left < 10 or seconds_left > 850:
            return

        # Check entry signal
        target_side = None
        target_ask = 0.0
        prob = 0.0

        if "BUY YES" in signal and best_edge >= (self.min_edge_pct / 100.0) and scalp_res["prob_yes_pct"] >= 60.0:
            if y_ask >= self.min_entry_price and y_ask <= self.max_entry_price:
                target_side = "YES"
                target_ask = y_ask
                prob = scalp_res["prob_yes"]
        elif "BUY NO" in signal and best_edge >= (self.min_edge_pct / 100.0) and scalp_res["prob_no_pct"] >= 60.0:
            if n_ask >= self.min_entry_price and n_ask <= self.max_entry_price:
                target_side = "NO"
                target_ask = n_ask
                prob = scalp_res["prob_no"]

        if not target_side or half_kelly_frac <= 0.01:
            return

        # Position Sizing (Net Half-Kelly capped at $75 and 15% bankroll)
        alloc_pct = min(self.max_allocation_pct, half_kelly_frac)
        budget = min(self.cash, self.cash * alloc_pct, self.max_trade_dollars)
        fee_per_contract = calculate_kalshi_fee(1, target_ask)
        target_contracts = int(budget / (target_ask + fee_per_contract))

        if target_contracts < 1:
            return

        # Execute trade with OMS VWAP depth check
        logger.info(
            f"[Signal Detected] {signal} on {ticker} | "
            f"Edge: +{best_edge*100:.1f}% | Kelly: {half_kelly_frac*100:.1f}% | Target: {target_contracts}x @ {int(target_ask*100)}¢"
        )
        trade_res = await self.oms.execute_trade(
            ticker=ticker,
            direction=target_side,
            target_contracts=target_contracts,
            edge_threshold=self.min_edge_pct,
            model_prob=prob
        )

        if trade_res.get("status") in ("FILLED", "executed", "FILLED_SIMULATION") or "order_id" in trade_res:
            cost = target_contracts * target_ask
            fees = calculate_kalshi_fee(target_contracts, target_ask)
            self.cash -= (cost + fees)
            self.trades_in_candle += 1
            self.active_position = {
                "ticker": ticker,
                "side": target_side,
                "contracts": target_contracts,
                "entry_price": target_ask,
                "cost_dollars": cost,
                "entry_ts": time.time()
            }
            logger.info(f"✓ Trade Executed: {target_contracts}x {target_side} on {ticker} (Order: {trade_res.get('order_id')})")
        elif trade_res.get("status") == "VWAP_SLIPPAGE_ABORT":
            logger.warning(f"[Trade Blocked] VWAP_SLIPPAGE_ABORT: {trade_res.get('reason')}")
        else:
            logger.warning(f"[Trade Skipped] {trade_res.get('status')}: {trade_res.get('reason')}")

    def stop(self):
        self.is_running = False


async def main():
    scalper = KalshiLiveScalper()

    def handle_exit(sig, frame):
        logger.info("Shutdown signal received. Exiting gracefully...")
        scalper.stop()

    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    await scalper.start()


if __name__ == "__main__":
    asyncio.run(main())
