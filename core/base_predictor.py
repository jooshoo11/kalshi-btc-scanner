"""
core/base_predictor.py — Standard Interface for Asset-Specific Prediction Engines.

All asset predictors (BTC, ETH, SOL, XRP) inherit from BaseAssetPredictor. The base
class owns the shared plumbing so subclasses supply only their asset-specific bits:

  * ``CONFIG_PATH``               — path to the asset ``coins/<asset>/config.json``
  * ``DEFAULT_VOL_15M_DOLLAR``    — fallback when config omits ``vol_15m_dollar``
  * ``DEFAULT_SPOT_FEEDS``        — fallback when config omits ``spot_feeds``

Everything else (spot-feed priority, exchange products/pairs, candle fetch, settlement
accumulation, drift-diffusion evaluation, trade metrics) is implemented once here and
driven by configuration:

  * ``spot_feeds``         — maps feed name -> exchange-specific symbol
                             (``coinbase`` product, ``kraken`` pair, ``hyperliquid`` coin)
  * ``spot_feed_priority`` — ordered list of feed names to try (config-driven; falls
                             back to ``SPOT_FEED_PRIORITY``)
  * ``vol_15m_dollar``     — 15-minute dollar volatility fed into the pricing model

Candle open is fetched from the Hyperliquid ``candleSnapshot`` endpoint when a
``hyperliquid`` feed is configured. Assets without Hyperliquid (currently XRP) use the
Kraken ``/0/public/OHLC`` endpoint (15-minute interval) instead.
"""

import json
import time
from abc import ABC
from typing import Any, Dict, List, Optional

from core.rti_accumulator import RtiAccumulator
from core.prediction_engine import QuantPredictionEngine


class BaseAssetPredictor(ABC):
    """
    Standard interface for asset-specific pricing models.
    """

    # Overridden by each subclass.
    CONFIG_PATH: Optional[str] = None
    DEFAULT_VOL_15M_DOLLAR: float = 0.0
    DEFAULT_SPOT_FEEDS: Dict[str, str] = {}
    SPOT_FEED_PRIORITY: tuple = ("hyperliquid", "kraken", "coinbase")

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        engine: Optional[QuantPredictionEngine] = None
    ):
        if config is None:
            config = self._load_default_config()

        self.asset: str = config.get("asset", "UNKNOWN").upper()
        self.symbol: str = config.get("symbol", f"{self.asset}/USD")
        self.kalshi_series: str = config.get("kalshi_series", f"KX{self.asset}15M")
        self.cf_index: str = config.get("cf_index", f"{self.asset}USD_RTI")

        # Spot-feed plumbing comes from config so editing coins/<asset>/config.json
        # actually changes which exchanges are queried and in what order.
        self.spot_feeds: Dict[str, str] = dict(self.DEFAULT_SPOT_FEEDS)
        configured_feeds = config.get("spot_feeds") or {}
        if isinstance(configured_feeds, dict):
            for feed, symbol in configured_feeds.items():
                if symbol:
                    self.spot_feeds[str(feed)] = str(symbol)

        configured_priority = config.get("spot_feed_priority")
        if not configured_priority:
            configured_priority = self.SPOT_FEED_PRIORITY
        self.feed_priority: List[str] = [
            str(feed) for feed in configured_priority if str(feed) in self.spot_feeds
        ]
        if not self.feed_priority:
            # No priority entry matched a configured feed: fall back to config order.
            self.feed_priority = list(self.spot_feeds.keys())

        # 15m dollar volatility is configuration-driven, with a per-asset fallback.
        raw_vol: Any = config.get("vol_15m_dollar")
        try:
            parsed_vol = float(raw_vol) if raw_vol not in (None, "") else 0.0
        except (TypeError, ValueError):
            parsed_vol = 0.0
        self.vol_15m_dollar: float = (
            parsed_vol if parsed_vol > 0.0 else float(self.DEFAULT_VOL_15M_DOLLAR)
        )

        self.accumulator = RtiAccumulator(index_name=self.cf_index, window_seconds=60)
        self.engine = engine or QuantPredictionEngine(risk_free_rate=0.045, taker_fee_cents=0.5)

        self.spot_price: float = 0.0
        self.spot_source: str = ""
        self.current_15m_candle_open: Optional[float] = None

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------
    def _load_default_config(self) -> Dict[str, Any]:
        if not self.CONFIG_PATH:
            return {}
        with open(self.CONFIG_PATH, "r") as f:
            return json.load(f)

    @staticmethod
    def _select_kraken_result(result: Dict[str, Any], pair: str) -> Optional[Dict[str, Any]]:
        """
        Kraken returns canonical keys (``XXBTZUSD``, ``XETHZUSD``, ``XXRPZUSD``) that
        rarely match the requested pair (``XBTUSD``, ``ETHUSD``, ``XRPUSD``). Match on
        the requested base token, falling back to the first non-``last`` entry.
        """
        entries = {k: v for k, v in (result or {}).items() if k != "last"}
        if not entries:
            return None
        token = str(pair).upper().replace("/", "").replace("-", "")
        for quote in ("USD", "USDT", "USDC", "EUR"):
            if token.endswith(quote) and len(token) > len(quote):
                token = token[: -len(quote)]
                break
        for key, value in entries.items():
            if token and token.upper() in key.upper():
                return value
        return next(iter(entries.values()))

    # ------------------------------------------------------------------
    # Spot feeds
    # ------------------------------------------------------------------
    async def _fetch_hyperliquid_spot(self, http_client: Any) -> Optional[float]:
        coin = self.spot_feeds.get("hyperliquid")
        if not coin:
            return None
        resp = await http_client.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "allMids"},
        )
        if resp.status_code != 200:
            return None
        mids = resp.json() or {}
        mid = mids.get(coin)
        if mid is None:
            return None
        return float(mid)

    async def _fetch_kraken_spot(self, http_client: Any) -> Optional[float]:
        pair = self.spot_feeds.get("kraken")
        if not pair:
            return None
        resp = await http_client.get(
            "https://api.kraken.com/0/public/Ticker",
            params={"pair": pair},
        )
        if resp.status_code != 200:
            return None
        entry = self._select_kraken_result((resp.json() or {}).get("result", {}), pair)
        if not entry:
            return None
        close = entry.get("c", [])
        if not close:
            return None
        return float(close[0])

    async def _fetch_coinbase_spot(self, http_client: Any) -> Optional[float]:
        product = self.spot_feeds.get("coinbase")
        if not product:
            return None
        resp = await http_client.get(
            f"https://api.exchange.coinbase.com/products/{product}/ticker"
        )
        if resp.status_code != 200:
            return None
        return float((resp.json() or {}).get("price", 0.0) or 0.0)

    async def update_spot_feeds(self, http_client: Any) -> float:
        """
        Fetch spot from the configured exchanges in ``feed_priority`` order.
        The first successful feed wins for this tick; ``spot_source`` is refreshed on
        every successful fetch so it never goes stale between fallbacks.
        """
        fetchers = {
            "hyperliquid": self._fetch_hyperliquid_spot,
            "kraken": self._fetch_kraken_spot,
            "coinbase": self._fetch_coinbase_spot,
        }

        for feed in self.feed_priority:
            fetcher = fetchers.get(feed)
            if fetcher is None:
                continue
            try:
                price = await fetcher(http_client)
            except Exception:
                price = None
            if price is not None and price > 0.0:
                self.spot_price = price
                self.spot_source = feed
                return self.spot_price

        return self.spot_price

    # ------------------------------------------------------------------
    # Technicals
    # ------------------------------------------------------------------
    async def _fetch_hyperliquid_candle_open(self, http_client: Any, now_ms: int) -> Optional[float]:
        coin = self.spot_feeds.get("hyperliquid")
        if not coin:
            return None
        candle_start = (now_ms // (15 * 60 * 1000)) * (15 * 60 * 1000)
        resp = await http_client.post(
            "https://api.hyperliquid.xyz/info",
            json={
                "type": "candleSnapshot",
                "req": {
                    "coin": coin,
                    "interval": "15m",
                    "startTime": candle_start,
                    "endTime": now_ms,
                },
            },
        )
        if resp.status_code != 200:
            return None
        candles = resp.json() or []
        if not candles:
            return None
        # Hyperliquid returns the in-progress candle last; its "o" is the candle open.
        return float(candles[-1].get("o", 0.0) or 0.0) or None

    async def _fetch_kraken_candle_open(self, http_client: Any) -> Optional[float]:
        pair = self.spot_feeds.get("kraken")
        if not pair:
            return None
        resp = await http_client.get(
            "https://api.kraken.com/0/public/OHLC",
            params={"pair": pair, "interval": 15},
        )
        if resp.status_code != 200:
            return None
        rows = self._select_kraken_result((resp.json() or {}).get("result", {}), pair)
        if not rows:
            return None
        # Kraken OHLC row: [time, open, high, low, close, vwap, volume, count]
        try:
            return float(rows[-1][1]) or None
        except (IndexError, TypeError, ValueError):
            return None

    async def update_technicals(self, http_client: Any):
        """
        Update the 15-minute candle open. Hyperliquid ``candleSnapshot`` is used when a
        ``hyperliquid`` feed is configured; otherwise (e.g. XRP) Kraken ``OHLC`` with a
        15-minute interval supplies the candle open.
        """
        now_ms = int(time.time() * 1000)
        candle_open: Optional[float] = None

        if "hyperliquid" in self.spot_feeds:
            try:
                candle_open = await self._fetch_hyperliquid_candle_open(http_client, now_ms)
            except Exception:
                candle_open = None

        if candle_open is None and self.spot_feeds.get("kraken"):
            try:
                candle_open = await self._fetch_kraken_candle_open(http_client)
            except Exception:
                candle_open = None

        if candle_open is not None and candle_open > 0.0:
            self.current_15m_candle_open = candle_open
        elif not self.current_15m_candle_open:
            # Last resort: treat spot as the candle open rather than leave delta undefined.
            self.current_15m_candle_open = self.spot_price or None

        if not self.vol_15m_dollar:
            self.vol_15m_dollar = float(self.DEFAULT_VOL_15M_DOLLAR)

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------
    def evaluate_15m_scalp(
        self,
        seconds_left: float,
        yes_ask: float,
        yes_bid: float,
        no_ask: float,
        no_bid: float,
        candle_open: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Evaluate drift-diffusion pricing and trade metrics for active 15M contract.
        """
        open_p = candle_open or self.current_15m_candle_open or self.spot_price
        accumulated = self.accumulator.get_samples() if self.accumulator.has_samples() else None

        scalp_res = self.engine.evaluate_15m_scalp(
            spot=self.spot_price,
            open_price=open_p,
            seconds_left=seconds_left,
            dollar_vol_15m=self.vol_15m_dollar,
            accumulated_samples=accumulated
        )

        trade_metrics = self.engine.calculate_trade_metrics(
            model_prob=scalp_res["prob_yes"],
            yes_ask=yes_ask,
            yes_bid=yes_bid,
            no_ask=no_ask,
            no_bid=no_bid
        )

        return {
            "asset": self.asset,
            "cf_index": self.cf_index,
            "spot_price": self.spot_price,
            "candle_open": open_p,
            "delta": scalp_res["delta"],
            "delta_pct": scalp_res["delta_pct"],
            "prob_yes": scalp_res["prob_yes"],
            "prob_no": scalp_res["prob_no"],
            "prob_yes_pct": scalp_res["prob_yes_pct"],
            "prob_no_pct": scalp_res["prob_no_pct"],
            "signal": trade_metrics["signal"],
            "side": trade_metrics["side"],
            "best_edge_pct": trade_metrics["best_edge_pct"],
            "best_ev_cents": trade_metrics["best_ev_cents"],
            "best_roi_pct": trade_metrics["best_roi_pct"],
            "half_kelly_pct": trade_metrics["half_kelly_pct"],
            "full_kelly_pct": trade_metrics["full_kelly_pct"],
            "price_cents": trade_metrics["price_cents"],
            "confidence": scalp_res["z_score"],
            # Historical key name kept for consumers (paper_trader settlement, dashboards).
            "brti_mean": (
                self.accumulator.get_mean() if self.accumulator.has_samples() else None
            ),
        }
