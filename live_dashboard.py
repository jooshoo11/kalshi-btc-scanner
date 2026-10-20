#!/usr/bin/env python3
"""
live_dashboard.py — Real-Time Terminal HUD Dashboard for Kalshi Bitcoin Scalper.

Displays live in your console:
- Kalshi Demo Account Balance ($50.00 confirmed) & Live WebSocket state
- Real-Time L2 Order Book Depth Ladder & VWAP Sweep Preview
- Spot price, 15m candle countdown, and BRTI 60s Accumulator variance collapse
- Model probabilities, mathematical edge %, and Net Half-Kelly sizing
- Stale-Tick Latency Watchdog & Cold-Boot Reconciliation status
"""

import os
import sys
import time
import math
import asyncio
from typing import Dict, Any, Optional

from dotenv import load_dotenv
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
from btc_metrics import BitcoinMetricsProvider
from prediction_engine import QuantPredictionEngine, calculate_kalshi_fee


# ANSI Color Codes
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_GREEN = "\033[38;5;82m"
C_RED = "\033[38;5;196m"
C_YELLOW = "\033[38;5;220m"
C_CYAN = "\033[38;5;51m"
C_BLUE = "\033[38;5;39m"
C_MAGENTA = "\033[38;5;207m"
C_DIM = "\033[2m"
C_BG_DARK = "\033[48;5;234m"


class TerminalLiveDashboard:
    def __init__(self):
        self.auth = KalshiAuth.from_env()
        self.watchdog = StaleTickWatchdog(max_allowed_delta_ms=1500.0)
        self.metrics = BitcoinMetricsProvider(watchdog=self.watchdog)
        self.engine = QuantPredictionEngine()
        self.ws_client = KalshiWebSocketClient(ws_url=KALSHI_DEMO_WS_URL, auth=self.auth)
        self.oms = KalshiOrderManagementSystem(
            auth=self.auth,
            rest_url=KALSHI_DEMO_REST_URL,
            ws_client=self.ws_client,
            watchdog=self.watchdog,
            dry_run=False
        )

        self.demo_balance_dollars = 50.0
        self.active_ticker = "KXBTC15M-ACTIVE"
        self.is_running = False

    async def fetch_demo_balance(self, client: httpx.AsyncClient):
        if not self.auth.is_configured:
            return
        headers = self.auth.sign_request("GET", "/trade-api/v2/portfolio/balance")
        try:
            resp = await client.get(f"{KALSHI_DEMO_REST_URL}/portfolio/balance", headers=headers, timeout=3.0)
            if resp.status_code == 200:
                data = resp.json()
                self.demo_balance_dollars = float(data.get("balance", 5000)) / 100.0
        except Exception:
            pass

    async def run(self):
        self.is_running = True
        ws_task = asyncio.create_task(self.ws_client.connect_and_listen())

        async with httpx.AsyncClient(timeout=4.0) as http_client:
            await self.fetch_demo_balance(http_client)
            await self.metrics.update_subsecond_spot(http_client)
            await self.metrics.update_candles_and_technicals(http_client)

            # Resolve active market
            try:
                headers = self.auth.sign_request("GET", "/trade-api/v2/markets")
                resp = await http_client.get(
                    f"{KALSHI_DEMO_REST_URL}/markets",
                    headers=headers,
                    params={"series_ticker": "KXBTC15M", "status": "open", "limit": 5}
                )
                if resp.status_code == 200:
                    markets = resp.json().get("markets", [])
                    if markets:
                        self.active_ticker = markets[0].get("ticker", self.active_ticker)
                        await self.ws_client.subscribe_ticker(self.active_ticker)
            except Exception:
                pass

            tick_count = 0
            while self.is_running:
                try:
                    await self.metrics.update_subsecond_spot(http_client)
                    tick_count += 1
                    if tick_count % 15 == 0:
                        await self.metrics.update_candles_and_technicals(http_client)
                        await self.fetch_demo_balance(http_client)

                    self.render_console()
                except Exception as e:
                    pass

                await asyncio.sleep(1.0)

        self.ws_client.stop()
        ws_task.cancel()

    def render_console(self):
        # Clear screen and return cursor to home
        sys.stdout.write("\033[2J\033[H")

        now = time.time()
        candle_start = (int(now) // 900) * 900
        candle_end = candle_start + 900
        seconds_left = max(0.0, float(candle_end - now))
        mins, secs = divmod(int(seconds_left), 60)

        spot = self.metrics.spot_price
        candle_open = self.metrics.current_15m_candle_open or spot
        delta = spot - candle_open
        delta_color = C_GREEN if delta >= 0 else C_RED
        delta_sign = "+" if delta >= 0 else ""

        # BRTI Accumulator sampling
        self.metrics.brti_accumulator.add_sample(spot, seconds_left)
        accum_samples = self.metrics.brti_accumulator.get_samples()

        # Pricing model
        scalp_res = self.engine.evaluate_15m_scalp(
            spot=spot,
            open_price=candle_open,
            seconds_left=seconds_left,
            dollar_vol_15m=self.metrics.vol_15m_dollar,
            accumulated_samples=accum_samples
        )

        prob_yes = scalp_res["prob_yes"]
        prob_no = scalp_res["prob_no"]

        # L2 Book
        book = self.ws_client.get_or_create_book(self.active_ticker)
        best_bid, bid_qty = book.get_best_bid()
        best_ask, ask_qty = book.get_best_ask()

        y_bid = float(best_bid) / 100.0 if best_bid > 0 else 0.50
        y_ask = float(best_ask) / 100.0 if best_ask < 100 else 0.52
        n_bid = round(1.0 - y_ask, 2)
        n_ask = round(1.0 - y_bid, 2)

        trade_metrics = self.engine.calculate_trade_metrics(
            model_prob=prob_yes,
            yes_ask=y_ask,
            yes_bid=y_bid,
            no_ask=n_ask,
            no_bid=n_bid
        )

        vwap_sweep_10 = book.calculate_sweep_vwap("YES", 10)
        vwap_sweep_25 = book.calculate_sweep_vwap("YES", 25)

        ws_status_str = f"{C_GREEN}CONNECTED (LIVE WS V2){C_RESET}" if self.ws_client.is_ws_connected else f"{C_YELLOW}CONNECTING...{C_RESET}"
        watchdog_status = f"{C_GREEN}HEALTHY ({self.watchdog.last_delta_ms:.0f}ms){C_RESET}" if not self.watchdog.is_halted else f"{C_RED}HALTED ({self.watchdog.halt_reason}){C_RESET}"

        # ----------------------------------------------------------------------
        # HEADER BOX
        # ----------------------------------------------------------------------
        print(f"{C_BOLD}{C_CYAN}╔═══════════════════════════════════════════════════════════════════════════════════════════╗{C_RESET}")
        print(f"{C_BOLD}{C_CYAN}║            KALSHI BITCOIN QUANTITATIVE LIVE SCALPER — DEMO TERMINAL HUD           ║{C_RESET}")
        print(f"{C_BOLD}{C_CYAN}╚═══════════════════════════════════════════════════════════════════════════════════════════╝{C_RESET}")

        # Account & Telemetry Row
        print(f" {C_BOLD}Demo Account:{C_RESET} ${self.demo_balance_dollars:.2f} USD   │ {C_BOLD}WebSocket:{C_RESET} {ws_status_str} │ {C_BOLD}Watchdog:{C_RESET} {watchdog_status}")
        print(f" {C_BOLD}Active Ticker:{C_RESET} {C_YELLOW}{self.active_ticker}{C_RESET} │ {C_BOLD}Window Remaining:{C_RESET} {C_BOLD}{mins:02d}:{secs:02d}{C_RESET} ({seconds_left:.0f}s left)")
        print(f"{C_DIM}─────────────────────────────────────────────────────────────────────────────────────────────{C_RESET}")

        # Spot & BRTI Accumulator Row
        print(f" {C_BOLD}BTC Spot (Hyperliquid):{C_RESET} {C_BOLD}${spot:,.2f}{C_RESET} │ {C_BOLD}Candle Open:{C_RESET} ${candle_open:,.2f} │ {C_BOLD}Delta:{C_RESET} {delta_color}{delta_sign}${delta:.2f}{C_RESET}")
        if seconds_left <= 60.0:
            print(f" {C_MAGENTA}{C_BOLD}[BRTI 60s ACCUMULATOR ACTIVE]{C_RESET} Samples: {len(accum_samples)}/60 │ Settle Mean: ${scalp_res.get('mu_settle', spot):,.2f} │ Sigma: ${scalp_res.get('sigma_settle', 0.0):.2f}")
        else:
            print(f" {C_DIM}BRTI 60s Accumulator: Inactive (Arms automatically at T-60s with cubic variance decay){C_RESET}")
        print(f"{C_DIM}─────────────────────────────────────────────────────────────────────────────────────────────{C_RESET}")

        # L2 Order Book Ladder vs Models
        print(f" {C_BOLD}{C_BLUE}LEVEL-2 ORDER BOOK DEPTH{C_RESET}                        │ {C_BOLD}{C_GREEN}QUANT PREDICTION ENGINE SIGNALS{C_RESET}")
        
        # Format Top Asks
        ask_lines = []
        sorted_asks = sorted(book.asks.items())[:4]
        if sorted_asks:
            for p, q in reversed(sorted_asks):
                bar = "█" * min(20, max(1, q // 2))
                ask_lines.append(f"  {C_RED}{p:2d}¢{C_RESET}  [{q:3d}] {C_RED}{bar:<20}{C_RESET}")
        else:
            ask_lines = [f"  {C_DIM}Asks empty (listening on WS...){C_RESET}"]

        # Format Top Bids
        bid_lines = []
        sorted_bids = sorted(book.bids.items(), reverse=True)[:4]
        if sorted_bids:
            for p, q in reversed(sorted_bids):
                bar = "█" * min(20, max(1, q // 2))
                bid_lines.append(f"  {C_GREEN}{p:2d}¢{C_RESET}  [{q:3d}] {C_GREEN}{bar:<20}{C_RESET}")
        else:
            bid_lines = [f"  {C_DIM}Bids empty (listening on WS...){C_RESET}"]

        spread_str = f"Spread: {y_ask*100 - y_bid*100:.0f}¢"
        signal_str = trade_metrics["signal"]
        edge_pct = trade_metrics.get("best_edge_pct", 0.0)
        kelly_pct = trade_metrics.get("half_kelly_pct", 0.0)

        # Print Side by Side
        left_col = ask_lines[0] if ask_lines else ""
        print(f"{left_col:<45} │  {C_BOLD}Signal:{C_RESET} {signal_str}")
        left_col2 = ask_lines[1] if len(ask_lines) > 1 else ""
        print(f"{left_col2:<45} │  {C_BOLD}Model Prob YES:{C_RESET} {C_GREEN}{prob_yes*100:.1f}%{C_RESET} │ {C_BOLD}NO:{C_RESET} {C_RED}{prob_no*100:.1f}%{C_RESET}")
        left_col3 = f"  {C_YELLOW}─── Top of Book: {best_bid}¢ / {best_ask}¢ ({spread_str}) ───{C_RESET}"
        print(f"{left_col3:<54} │  {C_BOLD}Net EV Edge:{C_RESET} {C_BOLD}{C_GREEN}+{edge_pct:.1f}%{C_RESET} (post-fee & 3¢ slippage)")
        left_col4 = bid_lines[0] if bid_lines else ""
        print(f"{left_col4:<45} │  {C_BOLD}Net Half-Kelly Size:{C_RESET} {C_CYAN}{kelly_pct:.1f}%{C_RESET} (max $75 cap)")
        left_col5 = bid_lines[1] if len(bid_lines) > 1 else ""
        print(f"{left_col5:<45} │  {C_BOLD}Z-Score:{C_RESET} {scalp_res['z_score']:+.2f}σ")

        print(f"{C_DIM}─────────────────────────────────────────────────────────────────────────────────────────────{C_RESET}")
        print(f" {C_BOLD}OMS VWAP Sweep Simulation:{C_RESET}")
        if vwap_sweep_10 and vwap_sweep_10.get("can_fill"):
            print(f"  • 10 Contracts Sweep: Expected VWAP = {C_BOLD}{vwap_sweep_10['vwap_cents']:.1f}¢{C_RESET} | Cap Limit = {vwap_sweep_10['worst_price_cents']}¢ | Levels = {vwap_sweep_10['levels_swept']}")
        else:
            print(f"  • 10 Contracts Sweep: {C_DIM}Calculating depth...{C_RESET}")

        if vwap_sweep_25 and vwap_sweep_25.get("can_fill"):
            print(f"  • 25 Contracts Sweep: Expected VWAP = {C_BOLD}{vwap_sweep_25['vwap_cents']:.1f}¢{C_RESET} | Cap Limit = {vwap_sweep_25['worst_price_cents']}¢ | Levels = {vwap_sweep_25['levels_swept']}")

        print(f"{C_DIM}─────────────────────────────────────────────────────────────────────────────────────────────{C_RESET}")
        print(f" {C_BOLD}Risk Management Rules:{C_RESET} Stop Loss: -25% (Limit-Chasing) │ Take Profit: +25% │ Penny Floor: 25¢ - 78¢")
        print(f" {C_DIM}Press Ctrl+C to exit dashboard.{C_RESET}")


def main():
    dashboard = TerminalLiveDashboard()
    try:
        asyncio.run(dashboard.run())
    except KeyboardInterrupt:
        print("\n[!] Dashboard stopped by user.")


if __name__ == "__main__":
    main()
