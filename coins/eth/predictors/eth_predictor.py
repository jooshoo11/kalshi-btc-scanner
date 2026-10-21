"""
coins/eth/predictors/eth_predictor.py — Ethereum Quantitative 15-Minute Pricing Model.
Settlement Index: CME CF Ether-Dollar Real Time Index (ETHUSD_RTI).
Feeds: Coinbase ETH-USD, Kraken ETHUSD, Hyperliquid ETH Mark.
"""

import math
import logging
from typing import Dict, Any, Optional
from core.base_predictor import BaseAssetPredictor

logger = logging.getLogger("ETHPredictor")


class ETHPredictor(BaseAssetPredictor):
    """
    Ethereum specific predictor implementation.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = config or {
            "asset": "ETH",
            "symbol": "ETH/USD",
            "kalshi_series": "KXETH15M",
            "cf_index": "ETHUSD_RTI",
            "spot_feeds": {"coinbase": "ETH-USD", "kraken": "ETHUSD", "hyperliquid": "ETH"}
        }
        super().__init__(cfg)

    async def update_spot_feeds(self, http_client: Any) -> float:
        """Fetch subsecond spot from Coinbase, Kraken, and Hyperliquid."""
        spots = []

        try:
            r = await http_client.get("https://api.coinbase.com/v2/prices/ETH-USD/spot", timeout=2.0)
            if r.status_code == 200:
                p = float(r.json()["data"]["amount"])
                if p > 0:
                    spots.append(p)
        except Exception:
            pass

        try:
            r = await http_client.get("https://api.kraken.com/0/public/Ticker?pair=ETHUSD", timeout=2.0)
            if r.status_code == 200:
                res = r.json().get("result", {})
                pair_data = res.get("XETHZUSD") or res.get("ETHUSD", {})
                if "c" in pair_data:
                    p = float(pair_data["c"][0])
                    if p > 0:
                        spots.append(p)
        except Exception:
            pass

        try:
            r = await http_client.post("https://api.hyperliquid.xyz/info", json={"type": "allMids"}, timeout=2.0)
            if r.status_code == 200:
                mids = r.json()
                if "ETH" in mids:
                    p = float(mids["ETH"])
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
            r = await http_client.get("https://api.kraken.com/0/public/OHLC?pair=ETHUSD&interval=15", timeout=3.0)
            if r.status_code == 200:
                res = r.json().get("result", {})
                candles = res.get("XETHZUSD") or res.get("ETHUSD", [])
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
                        self.vol_15m_dollar = max(1.5, stdev_15m * self.spot_price)
        except Exception as e:
            logger.debug(f"Error fetching ETH technicals: {e}")
