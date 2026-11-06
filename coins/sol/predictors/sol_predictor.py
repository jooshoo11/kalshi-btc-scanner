"""
coins/sol/predictors/sol_predictor.py — SOL (KXSOL15M / SOLUSD_RTI) drift-diffusion predictor.

Spot feed priority: Hyperliquid (lead) -> Kraken (secondary) -> Coinbase (tertiary).
"""

import time
import json
from typing import Any, Optional

import httpx

from core.base_predictor import BaseAssetPredictor


class SOLPredictor(BaseAssetPredictor):
    """
    SOL-specific implementation of BaseAssetPredictor.
    - Lead spot: Hyperliquid allMids (SOL)
    - Secondary: Kraken SOLUSD ticker
    - Tertiary: Coinbase SOL-USD ticker
    - RTI accumulator: SOLUSD_RTI, 60-second window
    """

    def __init__(self, config: Optional[dict] = None, engine: Optional[Any] = None):
        if config is None:
            with open("coins/sol/config.json", "r") as f:
                config = json.load(f)
        super().__init__(config=config, engine=engine)
        # SOL typical 15m dollar volatility
        self.vol_15m_dollar = 2.0

    async def update_spot_feeds(self, http_client: Any) -> float:
        price = None
        source = ""

        # 1. Hyperliquid (lead)
        try:
            resp = await http_client.post(
                "https://api.hyperliquid.xyz/info",
                json={"type": "allMids"},
            )
            if resp.status_code == 200:
                mids = resp.json()
                sol = mids.get("SOL")
                if sol is not None:
                    price = float(sol)
                    source = "hyperliquid"
        except Exception:
            pass

        # 2. Kraken (secondary)
        if price is None:
            try:
                resp = await http_client.get(
                    "https://api.kraken.com/0/public/Ticker",
                    params={"pair": "SOLUSD"},
                )
                if resp.status_code == 200:
                    result = resp.json().get("result", {})
                    for key, val in result.items():
                        if "SOL" in key.upper():
                            c = val.get("c", [])
                            if c:
                                price = float(c[0])
                                source = "kraken"
                                break
            except Exception:
                pass

        # 3. Coinbase (tertiary)
        if price is None:
            try:
                resp = await http_client.get(
                    "https://api.exchange.coinbase.com/products/SOL-USD/ticker"
                )
                if resp.status_code == 200:
                    data = resp.json()
                    price = float(data.get("price", 0.0))
                    source = "coinbase"
            except Exception:
                pass

        if price is not None and price > 0:
            self.spot_price = price
            self.spot_source = getattr(self, "spot_source", source) or source

        return self.spot_price

    async def update_technicals(self, http_client: Any):
        now_ms = int(time.time() * 1000)
        candle_start = (now_ms // (15 * 60 * 1000)) * (15 * 60 * 1000)

        try:
            resp = await http_client.post(
                "https://api.hyperliquid.xyz/info",
                json={
                    "type": "candleSnapshot",
                    "req": {
                        "coin": "SOL",
                        "interval": "15m",
                        "startTime": candle_start,
                        "endTime": now_ms,
                    },
                },
            )
            if resp.status_code == 200:
                candles = resp.json()
                if candles:
                    self.current_15m_candle_open = float(candles[-1].get("o", self.spot_price))
        except Exception:
            pass

        if self.current_15m_candle_open == 0.0 or self.current_15m_candle_open is None:
            self.current_15m_candle_open = self.spot_price

        if self.vol_15m_dollar == 0.0:
            self.vol_15m_dollar = 2.0

    def evaluate_15m_scalp(
        self,
        seconds_left: float,
        yes_ask: float,
        yes_bid: float,
        no_ask: float,
        no_bid: float,
        candle_open: Optional[float] = None,
    ) -> dict:
        open_p = candle_open or self.current_15m_candle_open or self.spot_price
        accumulated = self.accumulator.get_samples() if self.accumulator.has_samples() else None

        scalp_res = self.engine.evaluate_15m_scalp(
            spot=self.spot_price,
            open_price=open_p,
            seconds_left=seconds_left,
            dollar_vol_15m=self.vol_15m_dollar,
            accumulated_samples=accumulated,
        )

        trade_metrics = self.engine.calculate_trade_metrics(
            model_prob=scalp_res["prob_yes"],
            yes_ask=yes_ask,
            yes_bid=yes_bid,
            no_ask=no_ask,
            no_bid=no_bid,
        )

        return {
            "asset": "SOL",
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
            "brti_mean": (
                self.accumulator.get_mean() if self.accumulator.has_samples() else None
            ),
        }
