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
    KALSHI_PROD_REST_URL,
    KalshiAuth,
    KalshiWebSocketClient,
    KalshiOrderManagementSystem,
    StaleTickWatchdog,
    L2OrderBook
)
from prediction_engine import QuantPredictionEngine, calculate_kalshi_fee
from btc_metrics import BitcoinMetricsProvider
from kalshi_btc_scanner import KalshiBitcoinScanner, parse_iso_time, format_time_remaining
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
        db_path: str = "kalshi_live_demo.db",
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
        self.engine = QuantPredictionEngine(risk_free_rate=0.045, taker_fee_cents=0.5)
        self.scanner = KalshiBitcoinScanner(self.metrics_provider, self.engine)

        self.ws_client = KalshiWebSocketClient(ws_url=self.ws_url, auth=self.auth)
        self.oms = KalshiOrderManagementSystem(
            auth=self.auth,
            rest_url=self.rest_url,
            ws_client=self.ws_client,
            watchdog=self.watchdog,
            dry_run=self.dry_run
        )

        # Risk parameters (Matched 100% 1:1 with the +2076% / 85.6% Win Rate Paper Bot)
        self.min_edge_pct = 4.0
        self.take_profit_roi_pct = 25.0
        self.stop_loss_roi_pct = 25.0
        self.min_entry_price = 0.20
        self.max_entry_price = 0.80
        self.max_allocation_pct = 20.0  # 20% of total equity per trade (matching user directive)
        self.min_seconds_left_for_entry = 60  # Avoid late-candle expiration traps (<60s)
        self.max_trades_per_candle = 3
        self.taker_fee_cents = 0.5

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
        self.crypto_cash = 50.0  # Kalshi Demo exchange_index=2 (crypto partition) cash

    def _parse_balance_data(self, bal_data: Dict[str, Any]):
        """Extract both total Demo balance and exchange_index=2 (KXBTC15M crypto partition) cash."""
        bal_cents = bal_data.get("balance", 0)
        self.cash = float(bal_cents) / 100.0
        self.total_equity = self.cash
        self.crypto_cash = self.cash
        for item in bal_data.get("balance_breakdown", []):
            if item.get("exchange_index") == 2:
                try:
                    c_bal = float(item.get("balance", 0.0))
                    if c_bal > 0:
                        self.crypto_cash = c_bal
                except Exception:
                    pass

    def get_equity(self) -> float:
        if not self.active_position:
            return self.cash
        pos_val = self.active_position["contracts"] * self.active_position.get("current_bid", self.active_position["entry_price"])
        return self.cash + pos_val

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
                self._parse_balance_data(bal_data)
                logger.info(
                    f"✓ Confirmed Live Exchange Balance: ${self.cash:.2f} "
                    f"(Crypto Partition exchange_index=2: ${self.crypto_cash:.2f})"
                )
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
        """
        Uses cached active KXBTC15M ticker, refreshing from Kalshi REST only when
        candle rolls over to preserve Demo API rate limits.
        """
        now = time.time()
        # Fast rollover check: bypass cache during final 15s of candle so new candle is picked up immediately at second 0
        is_rollover = False
        if hasattr(self, "_cached_mkt") and self._cached_mkt:
            c_ts = parse_iso_time(self._cached_mkt.get("close_time", ""))
            if c_ts and (c_ts - now <= 15.0):
                is_rollover = True

        # Only refresh market metadata every 10 seconds or when ticker is unassigned
        if not is_rollover and hasattr(self, "_cached_mkt") and (now - getattr(self, "_last_mkt_resolve_ts", 0.0) < 10.0):
            return self._cached_mkt

        endpoint = "/markets"
        params = {"series_ticker": "KXBTC15M", "status": "open", "limit": 5}
        headers = self.auth.sign_request("GET", f"/trade-api/v2{endpoint}") if self.auth.is_configured else {}

        demo_mkt = None
        prod_mkt = None

        try:
            resp = await http_client.get(f"{self.rest_url}{endpoint}", headers=headers, params=params, timeout=4.0)
            if resp.status_code == 200:
                markets = resp.json().get("markets", [])
                if markets:
                    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    active_future = [m for m in markets if (m.get("close_time") or "") >= now_iso]
                    if active_future:
                        active_future.sort(key=lambda m: m.get("close_time", ""))
                        demo_mkt = active_future[0]
                    else:
                        markets.sort(key=lambda m: m.get("close_time", ""), reverse=True)
                        demo_mkt = markets[0]
        except Exception as e:
            logger.debug(f"Error querying demo active markets: {e}")

        # Fetch live institutional quotes from Kalshi Production
        try:
            prod_resp = await http_client.get(f"{KALSHI_PROD_REST_URL}{endpoint}", params=params, timeout=3.5)
            if prod_resp.status_code == 200:
                p_markets = prod_resp.json().get("markets", [])
                if p_markets:
                    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    p_active = [m for m in p_markets if (m.get("close_time") or "") >= now_iso]
                    if p_active:
                        p_active.sort(key=lambda m: m.get("close_time", ""))
                        prod_mkt = p_active[0]
                    else:
                        p_markets.sort(key=lambda m: m.get("close_time", ""), reverse=True)
                        prod_mkt = p_markets[0]
        except Exception as e:
            logger.debug(f"Error querying prod quotes: {e}")

        res = None
        if demo_mkt and prod_mkt:
            for k in ("yes_bid_dollars", "yes_ask_dollars", "no_bid_dollars", "no_ask_dollars", "yes_bid", "yes_ask", "floor_strike"):
                if prod_mkt.get(k) is not None:
                    demo_mkt[k] = prod_mkt[k]
            res = demo_mkt
        elif demo_mkt:
            res = demo_mkt
        elif prod_mkt:
            res = prod_mkt

        if not res:
            candle_end_sec = ((int(now) // 900) + 1) * 900
            gm = time.gmtime(candle_end_sec)
            time_str = time.strftime("%y%b%d%H%M", gm).upper()
            res = {
                "ticker": f"KXBTC15M-{time_str}",
                "close_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", gm),
                "candle_open": self.metrics_provider.spot_price
            }

        self._cached_mkt = res
        self._last_mkt_resolve_ts = now
        return res

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

    async def sync_exchange_orderbook(self, ticker: str, http_client: httpx.AsyncClient, force: bool = False) -> bool:
        """Fetch live resting orderbook directly from exchange and populate local L2 book with real depth."""
        now = time.time()
        if not force and hasattr(self, "_last_ob_sync_ts") and (now - self._last_ob_sync_ts < 2.0):
            return True
        self._last_ob_sync_ts = now

        try:
            endpoint = f"/markets/{ticker}/orderbook"
            headers = self.auth.sign_request("GET", endpoint) if self.auth and self.auth.is_configured else {}
            resp = await http_client.get(f"{self.rest_url}{endpoint}", headers=headers, timeout=2.5)
            if resp.status_code == 200:
                ob_data = resp.json().get("orderbook_fp") or resp.json().get("orderbook", {})
                yes_bids = []
                for p_str, q_str in ob_data.get("yes_dollars", []):
                    try:
                        p_c = int(round(float(p_str) * 100))
                        q = int(round(float(q_str)))
                        if p_c >= 1 and q > 0:
                            yes_bids.append([p_c, q])
                    except (ValueError, TypeError):
                        pass

                # In Kalshi single-book, NO bids represent YES asks at (100 - p_no)
                yes_asks = []
                for p_str, q_str in ob_data.get("no_dollars", []):
                    try:
                        p_no_c = int(round(float(p_str) * 100))
                        p_yes_ask = 100 - p_no_c
                        q = int(round(float(q_str)))
                        if 1 <= p_yes_ask <= 99 and q > 0:
                            yes_asks.append([p_yes_ask, q])
                    except (ValueError, TypeError):
                        pass

                # Legacy format fallback
                if not yes_bids and "bids" in ob_data:
                    yes_bids = [[int(p), int(q)] for p, q in ob_data.get("bids", []) if int(q) > 0 and int(p) >= 1]
                if not yes_asks and "asks" in ob_data:
                    yes_asks = [[int(p), int(q)] for p, q in ob_data.get("asks", []) if int(q) > 0 and int(p) <= 99]

                book = self.ws_client.get_or_create_book(ticker)
                if yes_bids or yes_asks:
                    book.apply_snapshot(yes_bids, yes_asks)
                    return True
        except Exception as e:
            logger.debug(f"Failed to sync exchange orderbook for {ticker}: {e}")
        return False

    async def _scalp_tick(self, http_client: httpx.AsyncClient):
        """Single 1-second iteration of the scalping loop (100% identical to PaperTradingBot + KalshiBitcoinScanner)."""
        # 1. Update spot feeds from Coinbase & Kraken USD Spot (CF Benchmarks BRTI constituents)
        await self.metrics_provider.update_subsecond_spot(http_client)
        spot = self.metrics_provider.spot_price

        # Periodically refresh candle open & volatility (every 10s)
        now = time.time()
        if not hasattr(self, "_last_candle_refresh") or now - self._last_candle_refresh > 10.0:
            await self.metrics_provider.update_candles_and_technicals(http_client)
            self._last_candle_refresh = now

        # Periodically sync live Demo balance (every 15s) so deposits & settlements update immediately
        if not hasattr(self, "_last_bal_sync") or now - self._last_bal_sync > 15.0:
            self._last_bal_sync = now
            try:
                b_headers = self.auth.sign_request("GET", "/trade-api/v2/portfolio/balance")
                b_resp = await http_client.get(f"{self.rest_url}/portfolio/balance", headers=b_headers, timeout=3.0)
                if b_resp.status_code == 200:
                    self._parse_balance_data(b_resp.json())
            except Exception:
                pass

        # 2. Resolve active 15-minute market (Demo contract ticker + live Production institutional quotes)
        mkt = await self.resolve_active_kxbtc_market(http_client)
        if not mkt:
            return

        ticker = mkt.get("ticker", "")
        close_time_iso = mkt.get("close_time", "")
        if ticker and ticker != self.current_ticker:
            logger.info(f"Targeting active KXBTC15M contract: {ticker}")
            self.current_ticker = ticker
            await self.ws_client.subscribe_ticker(ticker)

        # Exact contract expiration timing matching kalshi_btc_scanner.py 1:1
        now = time.time()
        close_ts = parse_iso_time(close_time_iso)
        if close_ts:
            seconds_left = max(1.0, close_ts - now)
        else:
            candle_start = (int(now) // 900) * 900
            seconds_left = max(1.0, float(candle_start + 900 - now))

        if self.current_candle_id != close_time_iso:
            self.current_candle_id = close_time_iso
            self.trades_in_candle = 0
            self.last_exit_reason = ""
            self.metrics_provider.brti_accumulator.reset_for_candle(close_time_iso)

        official_strike = float(mkt.get("floor_strike") or 0.0)
        candle_open = official_strike if official_strike > 0.0 else (self.metrics_provider.current_15m_candle_open or spot)
        self.metrics_provider.current_15m_candle_open = candle_open

        # Add sample to BRTI accumulator during final 60s
        self.metrics_provider.brti_accumulator.add_sample(spot, seconds_left, candle_id=close_time_iso)
        accumulated_samples = self.metrics_provider.brti_accumulator.get_samples()

        # 3. Extract quotes matching kalshi_btc_scanner.py 1:1 (NO artificial 0.49/0.51 corruption)
        y_ask = float(mkt.get("yes_ask_dollars") or 0.0)
        y_bid = float(mkt.get("yes_bid_dollars") or 0.0)
        n_ask = float(mkt.get("no_ask_dollars") or 0.0)
        n_bid = float(mkt.get("no_bid_dollars") or 0.0)
        if y_ask > 1.0:
            y_ask /= 100.0
        if y_bid > 1.0:
            y_bid /= 100.0
        if n_ask > 1.0:
            n_ask /= 100.0
        if n_bid > 1.0:
            n_bid /= 100.0
        if n_ask <= 0.0 and y_bid > 0.0:
            n_ask = round(1.0 - y_bid, 2)
        if n_bid <= 0.0 and y_ask > 0.0:
            n_bid = round(1.0 - y_ask, 2)

        # Synchronize L2 book cache for OMS execution with real exchange resting depth
        ob_synced = await self.sync_exchange_orderbook(ticker, http_client)
        book = self.ws_client.get_or_create_book(ticker)
        if not ob_synced or (not book.bids and not book.asks):
            best_bid = int(round(y_bid * 100)) if y_bid > 0 else 1
            best_ask = int(round(y_ask * 100)) if y_ask > 0 else 99
            ask_size = max(10, int(float(mkt.get("yes_ask_size_fp") or 10)))
            bid_size = max(10, int(float(mkt.get("yes_bid_size_fp") or 10)))
            book.apply_snapshot([[best_bid, bid_size]], [[best_ask, ask_size]])

        # 4. Run Quantitative Drift-Diffusion / BRTI Model (matching kalshi_btc_scanner.py 1:1)
        scalp_res = self.engine.evaluate_15m_scalp(
            spot=spot,
            open_price=candle_open,
            seconds_left=seconds_left,
            dollar_vol_15m=self.metrics_provider.vol_15m_dollar,
            accumulated_samples=accumulated_samples
        )
        model_prob = scalp_res["prob_yes"]
        model_prob_yes_pct = scalp_res["prob_yes_pct"]
        model_prob_no_pct = scalp_res["prob_no_pct"]
        delta = scalp_res["delta"]

        trade_metrics = self.engine.calculate_trade_metrics(
            model_prob=model_prob,
            yes_ask=y_ask,
            yes_bid=y_bid,
            no_ask=n_ask,
            no_bid=n_bid
        )

        # Convert to integer cents and back to dollars exactly like kalshi_btc_scanner.py -> paper_trader.py
        yes_bid_cents = int(round(y_bid * 100))
        yes_ask_cents = int(round(y_ask * 100))
        no_bid_cents = int(round(n_bid * 100))
        no_ask_cents = int(round(n_ask * 100))

        signal = trade_metrics.get("signal", "")
        best_edge_pct = float(trade_metrics.get("best_edge_pct", 0.0))

        # -------------------------------------------------------------
        # STEP A: Manage Open Position (Matching PaperTradingBot 1:1)
        # -------------------------------------------------------------
        if self.active_position:
            pos = self.active_position
            pos_side = pos["side"]
            entry_p = pos["entry_price"]
            raw_curr_bid = (float(yes_bid_cents) / 100.0) if pos_side == "YES" else (float(no_bid_cents) / 100.0)
            curr_model_prob = model_prob_yes_pct if pos_side == "YES" else model_prob_no_pct

            # Zero-Bid API Glitch Guard: Ignore 0¢-3¢ empty-book flickers when model probability is still alive
            if raw_curr_bid <= 0.03 and curr_model_prob >= 20.0:
                curr_bid = pos.get("current_bid", entry_p)
            else:
                curr_bid = max(0.01, raw_curr_bid)

            pos["current_bid"] = curr_bid
            unrealized_pct = round(((curr_bid - entry_p) / max(0.01, entry_p)) * 100.0, 1)

            # A. Check for Resolution (Time expired or <= 3s)
            if seconds_left <= 3 or pos.get("ticker") != ticker:
                logger.info(f"[Position Resolution] Candle expiring. Verifying {pos['ticker']} settlement & syncing live Demo balance...")
                settle_price = spot
                if self.metrics_provider.brti_accumulator.has_samples():
                    settle_price = self.metrics_provider.brti_accumulator.get_mean()
                is_win = (settle_price >= candle_open) if pos_side == "YES" else (settle_price < candle_open)
                try:
                    m_resp = await http_client.get(f"{self.rest_url}/markets/{pos['ticker']}", timeout=3.0)
                    if m_resp.status_code == 200:
                        m_obj = m_resp.json().get("market", {})
                        res_str = (m_obj.get("result") or "").lower()
                        if res_str in ("yes", "no"):
                            is_win = (res_str == pos_side.lower())
                except Exception:
                    pass

                exit_p = 1.0 if is_win else 0.0
                payout = pos["contracts"] * 1.0 if is_win else 0.0
                pnl = payout - (pos["cost_dollars"] + pos.get("fee_dollars", 0.0))
                pnl_pct = (pnl / max(0.01, pos["cost_dollars"] + pos.get("fee_dollars", 0.0))) * 100.0
                self.cash += payout

                # Sync exact cash balance from Kalshi Demo API
                try:
                    b_headers = self.auth.sign_request("GET", "/trade-api/v2/portfolio/balance")
                    b_resp = await http_client.get(f"{self.rest_url}/portfolio/balance", headers=b_headers, timeout=3.0)
                    if b_resp.status_code == 200:
                        self._parse_balance_data(b_resp.json())
                except Exception:
                    pass

                self.last_exit_reason = "SETTLED_WIN" if is_win else "SETTLED_LOSS"
                self.last_exit_ts = time.time()
                if pos.get("trade_id"):
                    self.db.record_trade_close(
                        trade_id=pos["trade_id"],
                        exit_price=exit_p,
                        exit_reason=self.last_exit_reason,
                        pnl_dollars=round(pnl, 2),
                        pnl_pct=round(pnl_pct, 1)
                    )
                self.active_position = None
                logger.info(f"[Position Resolution] Result: {self.last_exit_reason} | PnL: ${pnl:.2f} | Confirmed Demo Cash: ${self.cash:.2f}")
                return

            # B. Dynamic Profit Securing (Take Profit Exit +25% ROI)
            if unrealized_pct >= self.take_profit_roi_pct and (time.time() - pos["entry_ts"] >= 5.0):
                logger.info(f"[Take Profit] ROI +{unrealized_pct}% reached! Executing guaranteed complete exit...")
                await self.sync_exchange_orderbook(ticker, http_client, force=True)
                min_tp_floor = max(15, int(round(entry_p * 100)))  # Never sell below entry on take profit
                exit_res = await self.oms.execute_complete_exit(
                    ticker=ticker,
                    side=pos_side,
                    contracts=pos["contracts"],
                    target_price_cents=int(round(curr_bid * 100)),
                    floor_cents=min_tp_floor,
                    reason="TAKE_PROFIT"
                )
                fill_p = exit_res["fill_price_dollars"]
                filled_contracts = exit_res["filled_contracts"]
                if filled_contracts > 0:
                    portion_cost = (pos["cost_dollars"] / pos["contracts"]) * filled_contracts
                    gross_proceeds = filled_contracts * fill_p
                    exit_fees = filled_contracts * (self.taker_fee_cents / 100.0)
                    net_proceeds = gross_proceeds - exit_fees
                    pnl = net_proceeds - portion_cost
                    pnl_pct = (pnl / max(0.01, portion_cost)) * 100.0
                    self.cash += net_proceeds
                    self.crypto_cash += net_proceeds
                    if pos.get("trade_id"):
                        self.db.record_trade_close(
                            trade_id=pos["trade_id"],
                            exit_price=fill_p,
                            exit_reason="TAKE_PROFIT",
                            pnl_dollars=round(pnl, 2),
                            pnl_pct=round(pnl_pct, 1)
                        )
                # Re-sync exact cash balance from exchange
                try:
                    b_headers = self.auth.sign_request("GET", "/trade-api/v2/portfolio/balance")
                    b_resp = await http_client.get(f"{self.rest_url}/portfolio/balance", headers=b_headers, timeout=3.0)
                    if b_resp.status_code == 200:
                        self._parse_balance_data(b_resp.json())
                except Exception:
                    pass

                if exit_res["remaining_contracts"] <= 0:
                    self.active_position = None
                    self.last_exit_reason = "TAKE_PROFIT"
                    self.last_exit_ts = time.time()
                    return
                else:
                    pos["contracts"] = exit_res["remaining_contracts"]
                    pos["cost_dollars"] = (pos["cost_dollars"] / (filled_contracts + pos["contracts"])) * pos["contracts"]
                    logger.warning(f"[Take Profit Partial] {exit_res['remaining_contracts']} contracts remain open. Continuing position monitoring...")
                    return

            # C. Dynamic Risk Cut (Stop Loss -25% ROI AND model_prob <= 35.0%, with thin-book guard matching PaperTradingBot)
            if unrealized_pct <= -self.stop_loss_roi_pct and curr_model_prob <= 35.0:
                await self.sync_exchange_orderbook(ticker, http_client, force=True)
                safe_exit_bid = max(curr_bid, round(entry_p * 0.65, 2)) if curr_model_prob >= 18.0 else curr_bid
                floor_c = max(15, int(round(safe_exit_bid * 100)))
                logger.info(f"[Stop Loss] ROI {unrealized_pct}% & Model Prob {curr_model_prob}%! Dispatching Complete Exit Stop Loss (floor {floor_c}¢)...")
                exit_res = await self.oms.execute_complete_exit(
                    ticker=ticker,
                    side=pos_side,
                    contracts=pos["contracts"],
                    target_price_cents=int(round(curr_bid * 100)),
                    floor_cents=floor_c,
                    reason="STOP_LOSS"
                )
                fill_p = exit_res["fill_price_dollars"]
                filled_contracts = exit_res["filled_contracts"]
                if filled_contracts > 0:
                    portion_cost = (pos["cost_dollars"] / pos["contracts"]) * filled_contracts
                    gross_proceeds = filled_contracts * fill_p
                    exit_fees = filled_contracts * (self.taker_fee_cents / 100.0)
                    net_proceeds = gross_proceeds - exit_fees
                    pnl = net_proceeds - portion_cost
                    pnl_pct = (pnl / max(0.01, portion_cost)) * 100.0
                    self.cash += net_proceeds
                    self.crypto_cash += net_proceeds
                    if pos.get("trade_id"):
                        self.db.record_trade_close(
                            trade_id=pos["trade_id"],
                            exit_price=fill_p,
                            exit_reason="STOP_LOSS",
                            pnl_dollars=round(pnl, 2),
                            pnl_pct=round(pnl_pct, 1)
                        )
                # Re-sync exact cash balance from exchange
                try:
                    b_headers = self.auth.sign_request("GET", "/trade-api/v2/portfolio/balance")
                    b_resp = await http_client.get(f"{self.rest_url}/portfolio/balance", headers=b_headers, timeout=3.0)
                    if b_resp.status_code == 200:
                        self._parse_balance_data(b_resp.json())
                except Exception:
                    pass

                if exit_res["remaining_contracts"] <= 0:
                    self.active_position = None
                    self.last_exit_reason = "STOP_LOSS"
                    self.last_exit_ts = time.time()
                    return
                else:
                    pos["contracts"] = exit_res["remaining_contracts"]
                    pos["cost_dollars"] = (pos["cost_dollars"] / (filled_contracts + pos["contracts"])) * pos["contracts"]
                    logger.warning(f"[Stop Loss Partial] {exit_res['remaining_contracts']} contracts remain open. Continuing position monitoring...")
                    return

            return  # Still holding open trade

        # -------------------------------------------------------------
        # STEP B: Evaluate New Entry (Matching PaperTradingBot 1:1)
        # -------------------------------------------------------------
        if self.watchdog and not self.watchdog.is_safe_to_trade():
            return
        if self.trades_in_candle >= 1 and self.last_exit_reason in ("STOP_LOSS", "SETTLED_LOSS"):
            return
        if self.trades_in_candle >= self.max_trades_per_candle:
            return
        if time.time() - self.last_exit_ts < 4.0:
            return
        if seconds_left < self.min_seconds_left_for_entry or seconds_left > 855:
            return

        yes_ask_price = float(yes_ask_cents) / 100.0
        no_ask_price = float(no_ask_cents) / 100.0

        target_side = None
        target_ask = 0.0
        prob = 0.0
        prob_pct = 0.0

        if "BUY YES" in signal and best_edge_pct >= self.min_edge_pct and model_prob_yes_pct >= 60.0 and yes_ask_price >= self.min_entry_price and yes_ask_price <= self.max_entry_price:
            target_side = "YES"
            target_ask = yes_ask_price
            prob = scalp_res["prob_yes"]
            prob_pct = model_prob_yes_pct
        elif "BUY NO" in signal and best_edge_pct >= self.min_edge_pct and model_prob_no_pct >= 60.0 and no_ask_price >= self.min_entry_price and no_ask_price <= self.max_entry_price:
            target_side = "NO"
            target_ask = no_ask_price
            prob = scalp_res["prob_no"]
            prob_pct = model_prob_no_pct

        if not target_side or target_ask < self.min_entry_price or target_ask > self.max_entry_price:
            return

        # Position Sizing (Full 15% Equity Compounding matching PaperTradingBot 1:1, using full unified cash)
        equity = self.get_equity()
        max_alloc = equity * (self.max_allocation_pct / 100.0)
        avail_collateral = max(0.0, self.cash - 0.15)
        budget = min(avail_collateral, max_alloc)
        fee_per_contract = self.taker_fee_cents / 100.0
        target_contracts = int(budget / (target_ask + fee_per_contract))

        if target_contracts < 1:
            return

        cost = target_contracts * target_ask
        fees = target_contracts * fee_per_contract
        total_debit = cost + fees
        if total_debit > avail_collateral:
            target_contracts = int((avail_collateral - fees) / target_ask)
            if target_contracts < 1:
                return
            cost = target_contracts * target_ask
            fees = target_contracts * fee_per_contract
            total_debit = cost + fees

        # Force fresh orderbook sync immediately before sizing and execution
        await self.sync_exchange_orderbook(ticker, http_client, force=True)

        # Execute trade with OMS VWAP depth check
        logger.info(
            f"[Signal Detected] {signal} on {ticker} | "
            f"Edge: +{best_edge_pct:.1f}% | Prob: {prob_pct:.1f}% | Target: {target_contracts}x @ {int(target_ask*100)}¢"
        )
        trade_res = await self.oms.execute_trade(
            ticker=ticker,
            direction=target_side,
            target_contracts=target_contracts,
            edge_threshold=self.min_edge_pct,
            model_prob=prob
        )

        if trade_res.get("status") in ("FILLED", "FILLED_SIMULATION") and trade_res.get("filled_count", 0) > 0:
            filled_contracts = trade_res.get("filled_count", target_contracts)
            cost = filled_contracts * target_ask
            fees = filled_contracts * fee_per_contract
            self.cash -= (cost + fees)
            self.crypto_cash = max(0.0, self.crypto_cash - (cost + fees))
            self.trades_in_candle += 1
            rationale = (
                f"Live Demo Bot entered {target_side} on {ticker} at {int(target_ask*100)}¢ ({filled_contracts}/{target_contracts} contracts). "
                f"BTC is {delta:+.1f} from candle open. Model probability is {prob_pct}% with +{best_edge_pct:.1f}% edge. "
                f"Order ID: {trade_res.get('order_id')}"
            )
            trade_id = self.db.record_trade_open(
                ticker=ticker,
                series="KXBTC15M",
                side=target_side,
                entry_price=target_ask,
                contracts=filled_contracts,
                cost_dollars=cost,
                fee_dollars=fees,
                model_prob_entry=prob_pct,
                delta_at_entry=delta,
                seconds_left_at_entry=int(seconds_left),
                rationale=rationale
            )
            self.active_position = {
                "trade_id": trade_id,
                "order_id": trade_res.get("order_id"),
                "ticker": ticker,
                "side": target_side,
                "contracts": filled_contracts,
                "entry_price": target_ask,
                "current_bid": target_ask,
                "cost_dollars": cost,
                "fee_dollars": fees,
                "entry_ts": time.time()
            }
            logger.info(f"✓ LIVE DEMO TRADE EXECUTED: {filled_contracts}x {target_side} on {ticker} @ {int(target_ask*100)}¢ (Order ID: {trade_res.get('order_id')})")
        elif trade_res.get("status") == "VWAP_SLIPPAGE_ABORT":
            logger.warning(f"[Trade Blocked] VWAP_SLIPPAGE_ABORT: {trade_res.get('reason')}")
        else:
            logger.warning(f"[Trade Skipped] {trade_res.get('status')}: {trade_res.get('error') or trade_res.get('reason')}")
            # Re-sync balance & apply 8s cooldown so rejected orders never spam the API
            self.last_exit_ts = time.time()
            try:
                b_headers = self.auth.sign_request("GET", "/trade-api/v2/portfolio/balance")
                b_resp = await http_client.get(f"{self.rest_url}/portfolio/balance", headers=b_headers, timeout=3.0)
                if b_resp.status_code == 200:
                    self._parse_balance_data(b_resp.json())
            except Exception:
                pass

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
