"""
coins/xrp/predictors/xrp_predictor.py — Ripple Quantitative 15-Minute Pricing Model.
Settlement Index: CME CF Ripple-Dollar Real Time Index (XRPUSD_RTI).
Feeds: Coinbase XRP-USD, Kraken XRPUSD.
"""

import math
import logging
from typing import Dict, Any, Optional
from core.base_predictor import BaseAssetPredictor

logger = logging.getLogger("XRPPredictor")


class XRPPredictor(BaseAssetPredictor):
    """
    Ripple specific predictor implementation.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = config or {
            "asset": "XRP",
            "symbol": "XRP/USD",
            "kalshi_series": "KXXRP15M",
            "cf_index": "XRPUSD_RTI",
            "spot_feeds": {"coinbase": "XRP-USD", "kraken": "XRPUSD"}
        }
        super().__init__(cfg)

    async def update_spot_feeds(self, http_client: Any) -> float:
        """Fetch subsecond spot from Coinbase and Kraken."""
        spots = []

        try:
            r = await http_client.get("https://api.coinbase.com/v2/prices/XRP-USD/spot", timeout=2.0)
            if r.status_code == 200:
                p = float(r.json()["data"]["amount"])
                if p > 0:
                    spots.append(p)
        except Exception:
            pass

        try:
            r = await http_client.get("https://api.kraken.com/0/public/Ticker?pair=XRPUSD", timeout=2.0)
            if r.status_code == 200:
                res = r.json().get("result", {})
                pair_data = res.get("XXRPZUSD") or res.get("XRPUSD", {})
                if "c" in pair_data:
                    p = float(pair_data["c"][0])
                    if p > 0:
                        spots.append(p)
        except Exception:
            pass

        if spots:
            self.spot_price = sum(spots) / len(spots)
        return self.spot_price

    async def update_technicals(self, http_client: Any):
        """Fetch 15M candle open and realized volatility from Kraken."""
        try:
            r = await http_client.get("https://api.kraken.com/0/public/OHLC?pair=XRPUSD&interval=15", timeout=3.0)
            if r.status_code == 200:
                res = r.json().get("result", {})
                candles = res.get("XXRPZUSD") or res.get("XRPUSD", [])
                if candles:
                    curr_candle = candles[-1]
                    self.current_15m_candle_open = float(curr_candle[1])

                    returns = []
                    for i in range(1, min(len(candles), 97)):
                        c_prev = float(candles[i-1][4])
                        c_curr = float(candles[i][4])
                        if c_prev > 0 and c_curr > 0:
                            returns.append(math.log(c_curr / c_prev))
                    if len(returns) >= 10:
                        variance = sum(r * r for r in returns) / len(returns)
                        stdev_15m = math.sqrt(variance)
                        self.vol_15m_dollar = max(0.005, stdev_15m * self.spot_price)
        except Exception as e:
            logger.debug(f"Error fetching XRP technicals: {e}")
