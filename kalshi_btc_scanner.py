"""
kalshi_btc_scanner.py — KalshiBitcoinScanner for BTC/ETH/SOL/XRP 15M contracts.

Scans Kalshi V2 markets for the active 15-minute candle contract per asset,
builds the scalp_data dict used by the multi-coin production loop, and surfaces
top-EV opportunities from the broader Kalshi market catalog.

Implements:
  - Active market discovery per series (KXBTC15M, KXETH15M, KXSOL15M, KXXRP15M)
  - Tick-level scalp data: bid/ask, spread, seconds_left, model inputs
  - Top EV opportunity ranking across all Kalshi events (arb, sports, monotonicity, spread)
  - Strike ladder and range bracket extraction for barrier contracts
"""

import asyncio
import time
import math
from typing import Dict, Any, List, Optional

import httpx

from core.scanner_core import KalshiScanner
from core.prediction_engine import QuantPredictionEngine, calculate_kalshi_fee
from core.rti_accumulator import RtiAccumulator

KALSHI_PROD_REST_URL = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_DEMO_REST_URL = "https://demo-api.kalshi.co/trade-api/v2"

# Series ticker -> asset mapping
SERIES_TO_ASSET: Dict[str, str] = {
    "KXBTC15M": "BTC",
    "KXETH15M": "ETH",
    "KXSOL15M": "SOL",
    "KXXRP15M": "XRP",
}

ASSET_TO_SERIES: Dict[str, str] = {v: k for k, v in SERIES_TO_ASSET.items()}


class KalshiBitcoinScanner:
    """
    Scans Kalshi markets for 15-minute crypto binary contracts and broader
    event-catalog opportunities.

    Attributes:
      cache: dict with latest scan results keyed by series ticker
      scanner: underlying KalshiScanner (event-catalog strategies)
      prediction_engine: QuantPredictionEngine for drift-diffusion pricing
      rti_accumulators: per-series RTI accumulators for settlement-window sampling
    """

    def __init__(
        self,
        metrics_provider: Optional[Any] = None,
        prediction_engine: Optional[QuantPredictionEngine] = None,
        demo: bool = True,
    ):
        self.metrics_provider = metrics_provider
        self.prediction_engine = prediction_engine or QuantPredictionEngine(
            risk_free_rate=0.045, taker_fee_cents=0.5
        )
        self.demo = demo
        self.base_url = KALSHI_DEMO_REST_URL if demo else KALSHI_PROD_REST_URL

        self.cache: Dict[str, Any] = {
            "timestamp": 0,
            "total_markets_scanned": 0,
            "kxbtc15m": {},
            "kxeth15m": {},
            "kxsol15m": {},
            "kxxrp15m": {},
            "top_ev_opportunities": [],
            "strike_ladders_by_expiry": {},
            "range_brackets": [],
            "barrier_targets": [],
            "arbitrage_alerts": [],
            "stats": {},
        }
        self.scanner = KalshiScanner(taker_fee_cents=0.5)

        # Per-series RTI accumulators for BRTI-style settlement windows
        self.rti_accumulators: Dict[str, RtiAccumulator] = {
            series: RtiAccumulator(
                index_name=f"{asset}USD_RTI", window_seconds=60
            )
            for series, asset in SERIES_TO_ASSET.items()
        }

    # ------------------------------------------------------------ market discovery
    async def _fetch_open_markets(
        self, client: httpx.AsyncClient, series_ticker: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Fetch open markets from Kalshi V2, optionally filtered by series."""
        params: Dict[str, Any] = {"status": "open", "limit": 200}
        if series_ticker:
            params["series_ticker"] = series_ticker
        try:
            resp = await client.get(f"{self.base_url}/markets", params=params)
            if resp.status_code != 200:
                return []
            return resp.json().get("markets", [])
        except Exception as e:
            print(f"[KalshiScanner] Error fetching markets: {e}")
            return []

    async def _find_active_15m_market(
        self, client: httpx.AsyncClient, series: str
    ) -> Optional[Dict[str, Any]]:
        """Find the currently-active (closest-to-expiry, still open) 15M market for a series."""
        now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        markets = await self._fetch_open_markets(client, series_ticker=series)
        if not markets:
            return None
        # Prefer markets whose close_time is in the future; pick the nearest
        active = [m for m in markets if (m.get("close_time") or "") >= now_iso]
        if not active:
            active = markets
        active.sort(key=lambda m: m.get("close_time") or "")
        return active[0] if active else None

    # ------------------------------------------------------------ fast scan (1s tick)
    async def scan_kxbtc15m_fast(
        self, client: Optional[httpx.AsyncClient] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Fast 1-second scan for the active KXBTC15M market. Returns a scalp_data
        dict consumed by the multi-coin production loop and legacy paper bot.

        Fields:
          ticker, series, close_time, seconds_left, candle_open,
          yes_bid_cents, yes_ask_cents, no_bid_cents, no_ask_cents,
          spread_cents, model_prob_yes_pct, model_prob_no_pct,
          best_edge_pct, trade_signal, delta
        """
        own_client = False
        if client is None:
            client = httpx.AsyncClient(timeout=4.0)
            own_client = True

        try:
            mkt = await self._find_active_15m_market(client, "KXBTC15M")
            if not mkt:
                return None

            ticker = mkt.get("ticker", "")
            close_time = mkt.get("close_time", "")
            now_ms = int(time.time() * 1000)
            close_ms = int(time.mktime(time.strptime(close_time, "%Y-%m-%dT%H:%M:%SZ")) * 1000) if close_time else 0
            seconds_left = max(1.0, float((close_ms - now_ms) / 1000.0)) if close_ms > 0 else 900.0

            y_ask = float(mkt.get("yes_ask_dollars") or 0.0)
            y_bid = float(mkt.get("yes_bid_dollars") or 0.0)
            n_ask = float(mkt.get("no_ask_dollars") or 0.0)
            n_bid = float(mkt.get("no_bid_dollars") or 0.0)

            # Spot / candle from metrics provider
            spot = 0.0
            candle_open = 0.0
            if self.metrics_provider is not None:
                spot = self.metrics_provider.spot_price
                candle_open = self.metrics_provider.candle_15m_open
                # Record BRTI sample if in the final 60s
                if seconds_left <= 60.0:
                    self.rti_accumulators["KXBTC15M"].add_sample(
                        spot=spot, seconds_left=seconds_left, candle_id=close_time
                    )
            if candle_open == 0.0:
                candle_open = spot

            delta = spot - candle_open if spot and candle_open else 0.0

            # Model probability via prediction engine
            model_prob_yes_pct = 50.0
            model_prob_no_pct = 50.0
            if self.prediction_engine is not None and spot > 0 and candle_open > 0:
                market_data = {
                    "spot": spot,
                    "open_price": candle_open,
                    "seconds_left": seconds_left,
                    "dollar_vol_15m": (
                        self.metrics_provider.vol_24h_ann_pct / 100.0 * spot / 4.0
                    )
                    if self.metrics_provider
                    else 95.0,
                    "momentum_bias": (
                        self.metrics_provider.momentum_bias
                        if self.metrics_provider
                        else 0.0
                    ),
                    "accumulated_samples": (
                        self.rti_accumulators["KXBTC15M"].get_samples()
                        if self.rti_accumulators["KXBTC15M"].has_samples()
                        else None
                    ),
                }
                scalp_res = self.prediction_engine.evaluate_15m_scalp(
                    spot=spot,
                    open_price=candle_open,
                    seconds_left=seconds_left,
                    dollar_vol_15m=market_data["dollar_vol_15m"],
                    momentum_bias=market_data["momentum_bias"],
                    accumulated_samples=market_data["accumulated_samples"],
                    market_data=market_data,
                )
                model_prob_yes_pct = scalp_res["prob_yes_pct"]
                model_prob_no_pct = scalp_res["prob_no_pct"]
                delta = scalp_res["delta"]

            # Trade signal & edge
            spread_cents = round(abs(y_ask - y_bid) * 100.0, 1) if (y_ask and y_bid) else 0.0
            edge_pct = 0.0
            signal = ""
            if model_prob_yes_pct >= 60.0 and y_ask > 0:
                fee_y = calculate_kalshi_fee(1, y_ask) if y_ask > 0 else 0.0
                edge_pct = (model_prob_yes_pct / 100.0 - (y_ask + fee_y)) * 100.0
                if edge_pct >= 4.0:
                    signal = "BUY YES"
            elif model_prob_no_pct >= 60.0 and n_ask > 0:
                fee_n = calculate_kalshi_fee(1, n_ask) if n_ask > 0 else 0.0
                edge_pct = (model_prob_no_pct / 100.0 - (n_ask + fee_n)) * 100.0
                if edge_pct >= 4.0:
                    signal = "BUY NO"

            scalp_data = {
                "ticker": ticker,
                "series": "KXBTC15M",
                "asset": "BTC",
                "close_time": close_time,
                "seconds_left": seconds_left,
                "candle_open": candle_open,
                "spot_price": spot,
                "delta": round(delta, 2),
                "yes_bid_cents": round(y_bid * 100, 1),
                "yes_ask_cents": round(y_ask * 100, 1),
                "no_bid_cents": round(n_bid * 100, 1),
                "no_ask_cents": round(n_ask * 100, 1),
                "spread_cents": spread_cents,
                "model_prob_yes_pct": round(model_prob_yes_pct, 1),
                "model_prob_no_pct": round(model_prob_no_pct, 1),
                "best_edge_pct": round(edge_pct, 1),
                "trade_signal": signal,
                "brti_mean": (
                    self.rti_accumulators["KXBTC15M"].get_mean()
                    if self.rti_accumulators["KXBTC15M"].has_samples()
                    else None
                ),
            }

            # Update cache
            self.cache["kxbtc15m"] = scalp_data
            self.cache["timestamp"] = time.time()

            return scalp_data
        finally:
            if own_client:
                await client.aclose()

    # ------------------------------------------------------------ full scan (15s)
    async def scan_all_btc_markets(self, client: Optional[httpx.AsyncClient] = None) -> Dict[str, Any]:
        """
        Broader scan across all open Kalshi markets. Runs event-catalog strategies
        (arbitrage, sports +EV, monotonicity, spread sniping) and ranks top-EV
        opportunities.
        """
        own_client = False
        if client is None:
            client = httpx.AsyncClient(timeout=30.0)
            own_client = True

        try:
            # Use KalshiScanner for event-catalog strategies
            scan_result = await self.scanner.run_full_scan(max_pages=5)
            self.cache["top_ev_opportunities"] = scan_result.get("opportunities", [])
            self.cache["stats"] = scan_result.get("stats", {})
            self.cache["total_markets_scanned"] = scan_result.get("markets_scanned", 0)
            return self.cache
        finally:
            if own_client:
                await client.aclose()

    # ------------------------------------------------------------ per-series scan
    async def scan_series_fast(
        self, client: httpx.AsyncClient, series: str
    ) -> Optional[Dict[str, Any]]:
        """
        Generic fast scan for any 15M series (BTC, ETH, SOL, XRP). Returns the
        same-shaped scalp_data dict as scan_kxbtc15m_fast.
        """
        asset = SERIES_TO_ASSET.get(series, series)
        own_client = False
        try:
            mkt = await self._find_active_15m_market(client, series)
            if not mkt:
                return None

            ticker = mkt.get("ticker", "")
            close_time = mkt.get("close_time", "")
            now_ms = int(time.time() * 1000)
            close_ms = int(time.mktime(time.strptime(close_time, "%Y-%m-%dT%H:%M:%SZ")) * 1000) if close_time else 0
            seconds_left = max(1.0, float((close_ms - now_ms) / 1000.0)) if close_ms > 0 else 900.0

            y_ask = float(mkt.get("yes_ask_dollars") or 0.0)
            y_bid = float(mkt.get("yes_bid_dollars") or 0.0)
            n_ask = float(mkt.get("no_ask_dollars") or 0.0)
            n_bid = float(mkt.get("no_bid_dollars") or 0.0)

            spot = 0.0
            candle_open = 0.0
            if self.metrics_provider is not None:
                spot = self.metrics_provider.spot_price
                candle_open = self.metrics_provider.candle_15m_open
                acc = self.rti_accumulators.get(series)
                if acc is not None and seconds_left <= 60.0:
                    acc.add_sample(spot=spot, seconds_left=seconds_left, candle_id=close_time)
            if candle_open == 0.0:
                candle_open = spot

            delta = spot - candle_open if spot and candle_open else 0.0

            model_prob_yes_pct = 50.0
            model_prob_no_pct = 50.0
            if self.prediction_engine is not None and spot > 0 and candle_open > 0:
                market_data = {
                    "spot": spot,
                    "open_price": candle_open,
                    "seconds_left": seconds_left,
                    "dollar_vol_15m": (
                        self.metrics_provider.vol_24h_ann_pct / 100.0 * spot / 4.0
                    )
                    if self.metrics_provider
                    else 95.0,
                    "momentum_bias": (
                        self.metrics_provider.momentum_bias
                        if self.metrics_provider
                        else 0.0
                    ),
                    "accumulated_samples": (
                        self.rti_accumulators[series].get_samples()
                        if self.rti_accumulators[series].has_samples()
                        else None
                    ),
                }
                scalp_res = self.prediction_engine.evaluate_15m_scalp(
                    spot=spot,
                    open_price=candle_open,
                    seconds_left=seconds_left,
                    dollar_vol_15m=market_data["dollar_vol_15m"],
                    momentum_bias=market_data["momentum_bias"],
                    accumulated_samples=market_data["accumulated_samples"],
                    market_data=market_data,
                )
                model_prob_yes_pct = scalp_res["prob_yes_pct"]
                model_prob_no_pct = scalp_res["prob_no_pct"]
                delta = scalp_res["delta"]

            spread_cents = round(abs(y_ask - y_bid) * 100.0, 1) if (y_ask and y_bid) else 0.0
            edge_pct = 0.0
            signal = ""
            if model_prob_yes_pct >= 60.0 and y_ask > 0:
                fee_y = calculate_kalshi_fee(1, y_ask) if y_ask > 0 else 0.0
                edge_pct = (model_prob_yes_pct / 100.0 - (y_ask + fee_y)) * 100.0
                if edge_pct >= 4.0:
                    signal = "BUY YES"
            elif model_prob_no_pct >= 60.0 and n_ask > 0:
                fee_n = calculate_kalshi_fee(1, n_ask) if n_ask > 0 else 0.0
                edge_pct = (model_prob_no_pct / 100.0 - (n_ask + fee_n)) * 100.0
                if edge_pct >= 4.0:
                    signal = "BUY NO"

            scalp_data = {
                "ticker": ticker,
                "series": series,
                "asset": asset,
                "close_time": close_time,
                "seconds_left": seconds_left,
                "candle_open": candle_open,
                "spot_price": spot,
                "delta": round(delta, 2),
                "yes_bid_cents": round(y_bid * 100, 1),
                "yes_ask_cents": round(y_ask * 100, 1),
                "no_bid_cents": round(n_bid * 100, 1),
                "no_ask_cents": round(n_ask * 100, 1),
                "spread_cents": spread_cents,
                "model_prob_yes_pct": round(model_prob_yes_pct, 1),
                "model_prob_no_pct": round(model_prob_no_pct, 1),
                "best_edge_pct": round(edge_pct, 1),
                "trade_signal": signal,
                "brti_mean": (
                    self.rti_accumulators[series].get_mean()
                    if self.rti_accumulators[series].has_samples()
                    else None
                ),
            }

            self.cache[series.lower()] = scalp_data
            return scalp_data
        finally:
            if own_client:
                await client.aclose()
