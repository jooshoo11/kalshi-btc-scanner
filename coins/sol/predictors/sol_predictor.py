"""
coins/sol/predictors/sol_predictor.py — Solana Quantitative 15-Minute Pricing Model.
Settlement Index: CME CF Solana-Dollar Real Time Index (SOLUSD_RTI).
Feeds: Coinbase SOL-USD, Kraken SOLUSD, Hyperliquid SOL Mark.
"""

import math
import logging
from typing import Dict, Any, Optional
from core.base_predictor import BaseAssetPredictor

logger = logging.getLogger("SOLPredictor")


class SOLPredictor(BaseAssetPredictor):
    """
    Solana specific predictor implementation.
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = config or {
            "asset": "SOL",
            "symbol": "SOL/USD",
            "kalshi_series": "KXSOL15M",
            "cf_index": "SOLUSD_RTI",
            "spot_feeds": {"coinbase": "SOL-USD", "kraken": "SOLUSD", "hyperliquid": "SOL"}
        }
        super().__init__(cfg)

    async def update_spot_feeds(self, http_client: Any) -> float:
        """Fetch subsecond spot from Coinbase, Kraken, and Hyperliquid."""
        spots = []

        try:
            r = await http_client.get("https://api.coinbase.com/v2/prices/SOL-USD/spot", timeout=2.0)
            if r.status_code == 200:
                p = float(r.json()["data"]["amount"])
                if p > 0:
                    spots.append(p)
        except Exception:
            pass

        try:
            r = await http_client.get("https://api.kraken.com/0/public/Ticker?pair=SOLUSD", timeout=2.0)
            if r.status_code == 200:
                res = r.json().get("result", {})
                pair_data = res.get("SOLUSD", {})
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
                if "SOL" in mids:
                    p = float(mids["SOL"])
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
            r = await http_client.get("https://api.kraken.com/0/public/OHLC?pair=SOLUSD&interval=15", timeout=3.0)
            if r.status_code == 200:
                res = r.json().get("result", {})
                candles = res.get("SOLUSD", [])
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
                        self.vol_15m_dollar = max(0.25, stdev_15m * self.spot_price)
        except Exception as e:
            logger.debug(f"Error fetching SOL technicals: {e}")
