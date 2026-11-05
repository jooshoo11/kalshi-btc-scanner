"""
core/base_predictor.py — Standard Interface for Asset-Specific Prediction Engines.

All asset predictors (BTC, ETH, SOL, XRP) inherit from BaseAssetPredictor, implementing
standardized spot price feeds, CF Benchmarks settlement accumulation, and drift-diffusion jump modeling.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from core.rti_accumulator import RtiAccumulator
from core.prediction_engine import QuantPredictionEngine


class BaseAssetPredictor(ABC):
    """
    Standard interface for asset-specific pricing models.
    """

    def __init__(
        self,
        config: Dict[str, Any],
        engine: Optional[QuantPredictionEngine] = None
    ):
        self.asset: str = config.get("asset", "UNKNOWN").upper()
        self.symbol: str = config.get("symbol", f"{self.asset}/USD")
        self.kalshi_series: str = config.get("kalshi_series", f"KX{self.asset}15M")
        self.cf_index: str = config.get("cf_index", f"{self.asset}USD_RTI")
        self.spot_feeds: Dict[str, str] = config.get("spot_feeds", {})

        self.accumulator = RtiAccumulator(index_name=self.cf_index, window_seconds=60)
        self.engine = engine or QuantPredictionEngine(risk_free_rate=0.045, taker_fee_cents=0.5)

        self.spot_price: float = 0.0
        self.current_15m_candle_open: Optional[float] = None
        self.vol_15m_dollar: float = 0.0

    @abstractmethod
    async def update_spot_feeds(self, http_client: Any) -> float:
        """Fetch real-time spot price across configured exchange feeds."""
        pass

    @abstractmethod
    async def update_technicals(self, http_client: Any):
        """Update 15-minute candle open, realized volatility, and technical metrics."""
        pass

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
            model_prob_yes=scalp_res["prob_yes"],
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
            "confidence": scalp_res["z_score"]
        }
