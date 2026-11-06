"""
btc_metrics.py — BitcoinMetricsProvider (sub-second spot & market data feed).

Spot feed priority (lead -> fallback):
  1. Hyperliquid (allMids API) — lead spot price, open interest, funding, 15m candles
  2. Kraken (ticker API) — secondary spot price, volume
  3. Coinbase (ticker API) — tertiary spot price

Also computes:
  - 15-minute candle open / delta / realized volatility
  - Open interest (OI) and OI change
  - Liquidation proximity (distance from spot to estimated liquidation cluster)
  - 24h volume
  - Funding rate (annualized %)
  - RSI-14
  - Momentum bias (short-term price acceleration)

Integrates with core.rti_accumulator.RtiAccumulator for CF Benchmarks
60-second settlement window sampling during the final minute of a KXBTC15M candle.
"""

import time
import math
import logging
from typing import Dict, Any, Optional, List
from collections import deque

import httpx

from core.rti_accumulator import RtiAccumulator

logger = logging.getLogger("BitcoinMetricsProvider")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [BTCMetrics] %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)


class BitcoinMetricsProvider:
    """
    Aggregates sub-second spot and market data for BTC across multiple exchanges,
    with Hyperliquid as the lead feed and Kraken/Coinbase as fallbacks.

    Attributes exposed for the dashboard and prediction engine:
      spot_price, candle_15m_open, candle_15m_delta, vol_24h_ann_pct,
      rsi_14, funding_rate_ann_pct, open_interest, oi_change_pct,
      liq_proximity_pct, volume_24h, brti_accumulator, last_update_ts
    """

    # ------------------------------------------------------------------ init
    def __init__(self, watchdog: Optional[Any] = None):
        self.watchdog = watchdog

        # Spot price state
        self.spot_price: float = 0.0
        self.spot_source: str = ""
        self.spot_prices: Dict[str, float] = {}  # source -> price

        # Candle state (15-minute)
        self.candle_15m_open: float = 0.0
        self.candle_15m_high: float = 0.0
        self.candle_15m_low: float = 0.0
        self.candle_15m_close: float = 0.0
        self.candle_15m_delta: float = 0.0
        self.candle_15m_start_ms: int = 0
        self.candle_15m_end_ms: int = 0

        # Volatility / technicals
        self.vol_24h_ann_pct: float = 0.0
        self.realized_vol_15m: float = 0.0
        self.rsi_14: float = 50.0
        self.momentum_bias: float = 0.0

        # Derivatives context
        self.funding_rate_ann_pct: float = 0.0
        self.open_interest: float = 0.0
        self.oi_change_pct: float = 0.0
        self.volume_24h: float = 0.0
        self.liq_proximity_pct: float = 100.0  # 100% = far from liq, 0% = at liq

        # Price history for RSI / vol / momentum
        self._price_history: deque = deque(maxlen=1000)  # (timestamp, price)
        self._return_history: deque = deque(maxlen=100)

        # BRTI accumulator (CF Benchmarks 60-second settlement window)
        self.brti_accumulator = RtiAccumulator(index_name="BRTI", window_seconds=60)
        self.brti_active_candle_id: Optional[str] = None

        # Timestamps
        self.last_update_ts: float = 0.0
        self.last_candle_fetch_ts: float = 0.0

        # HTTP client session (reused)
        self._client: Optional[httpx.AsyncClient] = None

    # --------------------------------------------------------------- http
    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=4.0)
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ----------------------------------------------------- spot feed: hyperliquid (lead)
    async def _fetch_hyperliquid_spot(self, client: httpx.AsyncClient) -> Optional[Dict[str, Any]]:
        """
        Hyperliquid allMids -> spot price. Also returns per-coin universe data
        for OI, funding, volume when available via separate endpoints.
        """
        try:
            resp = await client.post(
                "https://api.hyperliquid.xyz/info",
                json={"type": "allMids"},
            )
            if resp.status_code != 200:
                return None
            mids = resp.json()
            btc = mids.get("BTC")
            if btc is None:
                return None
            price = float(btc)
            return {
                "source": "hyperliquid",
                "price": price,
                "timestamp": time.time(),
            }
        except Exception as e:
            logger.debug(f"Hyperliquid spot fetch error: {e}")
            return None

    async def _fetch_hyperliquid_candle(self, client: httpx.AsyncClient, start_ms: int, end_ms: int) -> Optional[float]:
        """
        Hyperliquid 15m candle snapshot -> candle open price.
        """
        try:
            resp = await client.post(
                "https://api.hyperliquid.xyz/info",
                json={
                    "type": "candleSnapshot",
                    "req": {
                        "coin": "BTC",
                        "interval": "15m",
                        "startTime": start_ms,
                        "endTime": end_ms,
                    },
                },
            )
            if resp.status_code != 200:
                return None
            candles = resp.json()
            if not candles:
                return None
            return float(candles[-1].get("o", 0.0))
        except Exception as e:
            logger.debug(f"Hyperliquid candle fetch error: {e}")
            return None

    # ----------------------------------------------------- spot feed: kraken (secondary)
    async def _fetch_kraken_spot(self, client: httpx.AsyncClient) -> Optional[Dict[str, Any]]:
        """
        Kraken REST ticker for XBT/USD.
        """
        try:
            resp = await client.get("https://api.kraken.com/0/public/Ticker", params={"pair": "XBTUSD"})
            if resp.status_code != 200:
                return None
            data = resp.json()
            result = data.get("result", {})
            # Kraken returns pair names like XXBTZUSD
            for key, val in result.items():
                if "XBT" in key.upper():
                    c = val.get("c", [])
                    if c:
                        price = float(c[0])
                        return {
                            "source": "kraken",
                            "price": price,
                            "timestamp": time.time(),
                            "volume_24h": float(val.get("v", [0, 0])[1]) if val.get("v") else 0.0,
                        }
            return None
        except Exception as e:
            logger.debug(f"Kraken spot fetch error: {e}")
            return None

    # ----------------------------------------------------- spot feed: coinbase (tertiary)
    async def _fetch_coinbase_spot(self, client: httpx.AsyncClient) -> Optional[Dict[str, Any]]:
        """
        Coinbase public ticker for BTC-USD.
        """
        try:
            resp = await client.get("https://api.exchange.coinbase.com/products/BTC-USD/ticker")
            if resp.status_code != 200:
                return None
            data = resp.json()
            price = float(data.get("price", 0.0))
            volume = float(data.get("volume_24h", 0.0))
            return {
                "source": "coinbase",
                "price": price,
                "timestamp": time.time(),
                "volume_24h": volume,
            }
        except Exception as e:
            logger.debug(f"Coinbase spot fetch error: {e}")
            return None

    # ----------------------------------------------------------------- spot update
    async def update_subsecond_spot(self, client: Optional[httpx.AsyncClient] = None):
        """
        Fetch spot from lead (Hyperliquid) with Kraken -> Coinbase fallback.
        Updates spot_price, spot_source, spot_prices, and feeds the watchdog.
        """
        own_client = False
        if client is None:
            client = await self._get_client()
            own_client = True

        result = None
        # Lead: Hyperliquid
        result = await self._fetch_hyperliquid_spot(client)
        if result is None:
            # Fallback: Kraken
            result = await self._fetch_kraken_spot(client)
        if result is None:
            # Fallback: Coinbase
            result = await self._fetch_coinbase_spot(client)

        if result is None:
            logger.warning("All spot feeds failed — retaining last spot_price")
            return

        price = result["price"]
        source = result["source"]

        # Track all available source prices
        self.spot_prices[source] = price

        # Only update primary spot if the source changed or price moved meaningfully
        if self.spot_price == 0.0 or source == self.spot_source or abs(price - self.spot_price) / max(self.spot_price, 1e-9) > 0.0001:
            self.spot_price = price
            self.spot_source = source

        self._price_history.append((time.time(), price))
        self.last_update_ts = time.time()

        # Feed watchdog if available (tick timestamp = now, latency ~0 for REST)
        if self.watchdog is not None:
            try:
                self.watchdog.record_tick(feed_timestamp=time.time())
            except Exception:
                pass

        if own_client:
            await client.aclose()

    # ----------------------------------------------------- candles & technicals
    async def update_candles_and_technicals(self, client: Optional[httpx.AsyncClient] = None):
        """
        Fetch the current 15m candle open from Hyperliquid, compute:
          - candle_15m_open, high, low, close
          - candle_15m_delta (spot - open)
          - realized_vol_15m
          - vol_24h_ann_pct
          - rsi_14
          - momentum_bias
        """
        own_client = False
        if client is None:
            client = await self._get_client()
            own_client = True

        now_ms = int(time.time() * 1000)
        candle_15m_start = (now_ms // (15 * 60 * 1000)) * (15 * 60 * 1000)
        candle_15m_end = candle_15m_start + (15 * 60 * 1000)

        # Only refetch candle open if we're in a new candle or cache is stale (>20s)
        if (
            self.candle_15m_start_ms != candle_15m_start
            or self.candle_15m_open == 0.0
            or time.time() - self.last_candle_fetch_ts > 20.0
        ):
            open_p = await self._fetch_hyperliquid_candle(client, candle_15m_start, now_ms)
            if open_p is not None and open_p > 0:
                self.candle_15m_open = open_p
                self.candle_15m_start_ms = candle_15m_start
                self.candle_15m_end_ms = candle_15m_end
                self.last_candle_fetch_ts = time.time()
            elif self.candle_15m_open == 0.0:
                # No candle data yet — fall back to spot as open
                self.candle_15m_open = self.spot_price
                self.candle_15m_start_ms = candle_15m_start
                self.candle_15m_end_ms = candle_15m_end
                self.last_candle_fetch_ts = time.time()

        self.candle_15m_close = self.spot_price
        self.candle_15m_delta = self.spot_price - self.candle_15m_open

        # High / low from price history within this candle
        candle_prices = [p for ts, p in self._price_history if ts * 1000 >= candle_15m_start]
        if candle_prices:
            self.candle_15m_high = max(candle_prices)
            self.candle_15m_low = min(candle_prices)
        else:
            self.candle_15m_high = self.spot_price
            self.candle_15m_low = self.spot_price

        # Realized volatility (15m, annualized %)
        if len(self._return_history) >= 2:
            mean_ret = sum(self._return_history) / len(self._return_history)
            var = sum((r - mean_ret) ** 2 for r in self._return_history) / len(self._return_history)
            self.realized_vol_15m = math.sqrt(max(var, 1e-9))
            # Annualize: 15m -> 1 year = 365.25 * 24 * 4 = 35052 periods
            self.vol_24h_ann_pct = self.realized_vol_15m * math.sqrt(35052.0) * 100.0
        else:
            self.vol_24h_ann_pct = 95.0  # sensible default for BTC

        # RSI-14
        self.rsi_14 = self._compute_rsi(14)

        # Momentum bias: short-term acceleration (last 30s vs prior 30s)
        self.momentum_bias = self._compute_momentum_bias()

        if own_client:
            await client.aclose()

    def _compute_rsi(self, period: int = 14) -> float:
        if len(self._price_history) < period + 1:
            return 50.0
        prices = [p for _, p in list(self._price_history)]
        gains = []
        losses = []
        for i in range(1, len(prices)):
            change = prices[i] - prices[i - 1]
            if change >= 0:
                gains.append(change)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(-change)
        if len(gains) < period:
            return 50.0
        recent_gains = gains[-period:]
        recent_losses = losses[-period:]
        avg_gain = sum(recent_gains) / period
        avg_loss = sum(recent_losses) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100.0 - (100.0 / (1.0 + rs)), 2)

    def _compute_momentum_bias(self) -> float:
        """
        Momentum bias = (recent_avg_price - earlier_avg_price) / earlier_avg_price,
        computed over two 30-second windows. Positive = upward acceleration.
        """
        now = time.time()
        recent = [p for ts, p in self._price_history if now - ts <= 30.0]
        earlier = [p for ts, p in self._price_history if 30.0 < now - ts <= 60.0]
        if not recent or not earlier:
            return 0.0
        recent_avg = sum(recent) / len(recent)
        earlier_avg = sum(earlier) / len(earlier)
        if earlier_avg == 0:
            return 0.0
        return round((recent_avg - earlier_avg) / earlier_avg * 100.0, 4)

    # ----------------------------------------------------- derivatives context
    async def _fetch_hyperliquid_meta_and_ctx(
        self, client: httpx.AsyncClient
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch Hyperliquid metaAndAssetCtxs for BTC.
        Returns the asset context dict containing open interest, funding, volume, etc.
        """
        try:
            resp = await client.post(
                "https://api.hyperliquid.xyz/info",
                json={"type": "metaAndAssetCtxs", "coin": "BTC"},
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            if not data or len(data) < 2:
                return None
            # data[0] = meta, data[1] = asset ctxs (list)
            asset_ctxs = data[1] if isinstance(data[1], list) else []
            for ctx in asset_ctxs:
                if ctx.get("coin") == "BTC":
                    return ctx
            return None
        except Exception as e:
            logger.debug(f"Hyperliquid metaAndAssetCtxs error: {e}")
            return None

    async def update_derivatives_context(self, client: Optional[httpx.AsyncClient] = None):
        """
        Fetch funding rate, open interest, and volume from Hyperliquid metaAndAssetCtxs.
        Computes liquidation proximity from order book levels.
        """
        own_client = False
        if client is None:
            client = await self._get_client()
            own_client = True

        ctx = await self._fetch_hyperliquid_meta_and_ctx(client)

        if ctx:
            # Funding rate (per 8h typically, convert to annualized %)
            funding = ctx.get("funding", 0.0)
            try:
                funding = float(funding)
                # Hyperliquid funding is per funding interval (typically 1 hour or 8h)
                # Annualize: funding * 24 * 365.25 * 100
                self.funding_rate_ann_pct = round(funding * 24.0 * 365.25 * 100.0, 2)
            except (TypeError, ValueError):
                self.funding_rate_ann_pct = 8.0

            # Open interest
            oi = ctx.get("openInterest", 0.0)
            try:
                new_oi = float(oi)
                if self.open_interest > 0:
                    self.oi_change_pct = round(
                        (new_oi - self.open_interest) / self.open_interest * 100.0, 2
                    )
                self.open_interest = new_oi
            except (TypeError, ValueError):
                pass

            # Volume (24h)
            vol = ctx.get("volume", 0.0)
            try:
                self.volume_24h = float(vol)
            except (TypeError, ValueError):
                pass

            # Mark price for liquidation proximity
            mark = ctx.get("markPx", 0.0)
            try:
                mark_px = float(mark)
                if mark_px > 0 and self.spot_price > 0:
                    # Liquidation proximity: distance from spot to ±5% band around mark
                    # 0% = at edge of danger zone, 100% = at spot (safe)
                    band = 0.05
                    upper = mark_px * (1.0 + band)
                    lower = mark_px * (1.0 - band)
                    if self.spot_price >= upper or self.spot_price <= lower:
                        self.liq_proximity_pct = 0.0
                    else:
                        # Interpolate: closer to spot = higher proximity
                        dist_from_edge = min(
                            abs(self.spot_price - lower),
                            abs(upper - self.spot_price),
                        )
                        band_half_width = mark_px * band
                        self.liq_proximity_pct = round(
                            (dist_from_edge / max(band_half_width, 1e-9)) * 100.0, 2
                        )
            except (TypeError, ValueError):
                pass
        else:
            # Fallback heuristics when ctx is unavailable
            if self.funding_rate_ann_pct == 0.0:
                self.funding_rate_ann_pct = 8.0
            if self.volume_24h == 0.0 and self.spot_price > 0:
                self.open_interest = self.volume_24h * 7.5
                self.oi_change_pct = 0.0
            self.liq_proximity_pct = 100.0

        if own_client:
            await client.aclose()

    # ----------------------------------------------------- summary
    def get_summary_dict(self) -> Dict[str, Any]:
        """Return a flat dict of all metrics for the dashboard and prediction engine."""
        return {
            "timestamp": time.time(),
            "spot_price": round(self.spot_price, 2),
            "spot_source": self.spot_source,
            "spot_prices": {k: round(v, 2) for k, v in self.spot_prices.items()},
            "candle_15m_open": round(self.candle_15m_open, 2),
            "candle_15m_high": round(self.candle_15m_high, 2),
            "candle_15m_low": round(self.candle_15m_low, 2),
            "candle_15m_close": round(self.candle_15m_close, 2),
            "candle_15m_delta": round(self.candle_15m_delta, 2),
            "candle_15m_delta_pct": round(
                (self.candle_15m_delta / max(self.candle_15m_open, 1e-9)) * 100.0, 3
            ),
            "vol_24h_ann_pct": round(self.vol_24h_ann_pct, 2),
            "realized_vol_15m": round(self.realized_vol_15m, 4),
            "rsi_14": self.rsi_14,
            "momentum_bias": self.momentum_bias,
            "funding_rate_ann_pct": round(self.funding_rate_ann_pct, 2),
            "open_interest": round(self.open_interest, 2),
            "oi_change_pct": round(self.oi_change_pct, 2),
            "volume_24h": round(self.volume_24h, 2),
            "liq_proximity_pct": round(self.liq_proximity_pct, 2),
            "brti_accumulator": {
                "index_name": self.brti_accumulator.index_name,
                "sample_count": self.brti_accumulator.sample_count(),
                "has_samples": self.brti_accumulator.has_samples(),
                "mean": round(self.brti_accumulator.get_mean(), 2) if self.brti_accumulator.has_samples() else 0.0,
            },
            "last_update_ts": self.last_update_ts,
        }

    # ----------------------------------------------------- brti integration
    def record_brti_sample(self, spot: float, seconds_left: float, candle_id: Optional[str] = None):
        """
        Record a 1-second spot sample into the BRTI accumulator during the final
        60 seconds of a KXBTC15M candle (CF Benchmarks settlement window).
        """
        if candle_id and candle_id != self.brti_active_candle_id:
            self.brti_accumulator.reset_for_candle(candle_id)
            self.brti_active_candle_id = candle_id
        self.brti_accumulator.add_sample(spot=spot, seconds_left=seconds_left, candle_id=candle_id)

    def get_brti_mean(self) -> float:
        """Return the current BRTI 60-second arithmetic mean."""
        return self.brti_accumulator.get_mean()

    def get_market_data_dict(self) -> Dict[str, Any]:
        """
        Return the market_data dict expected by QuantPredictionEngine.evaluate_15m_scalp
        and evaluate_15m_brti_accumulator.
        """
        seconds_left = max(1.0, float((self.candle_15m_end_ms - int(time.time() * 1000)) / 1000.0))
        return {
            "spot": self.spot_price,
            "open_price": self.candle_15m_open,
            "seconds_left": seconds_left,
            "dollar_vol_15m": self.vol_24h_ann_pct / math.sqrt(35052.0) * 900.0 / 100.0 * self.spot_price / 4.0,
            "momentum_bias": self.momentum_bias,
            "accumulated_samples": self.brti_accumulator.get_samples() if self.brti_accumulator.has_samples() else None,
        }
