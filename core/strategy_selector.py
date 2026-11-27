#!/usr/bin/env python3
"""
core/strategy_selector.py — Adaptive Strategy Selection.

Maps a Regime (from RegimeDetector) to a concrete StrategyConfig. The bot calls
select() each evaluation cycle; when the regime changes, the engine's entry/exit
parameters are reloaded to match the current market conditions.

The five strategy profiles mirror scripts/streaming_backtest.py:
  S2_TRIAL4_DEPLOYED_L2   — conservative, L2 VWAP fills, 120s time exit, -25% SL
  S3_REC_CF_DISLOC_NOSL   — CF dislocation, +55% maker TP, no noise SL
  S4_REC_STRUCT_STOP_55TP — CF dislocation + structural stop (z<=-1.8)
  S5_REC_LEADLAG_ENSEMBLE — lead-lag filter + +55% TP + structural SL (best OOS)
"""

from dataclasses import dataclass, field
from typing import Dict, Optional

from core.regime_detector import Regime, RegimeDetector


@dataclass
class StrategyConfig:
    """A single strategy's executable parameters."""
    name: str
    max_entry_price: float = 0.35
    min_entry_price: float = 0.20
    certainty_arb_cap: float = 0.78
    min_edge_pct: float = 4.0
    min_prob_btc: float = 65.0
    min_prob_eth: float = 60.0
    min_sec_btc: int = 360
    min_sec_eth: int = 240
    max_sec_entry: int = 500
    take_profit_roi_pct: float = 25.0
    stop_loss_roi_pct: float = 25.0
    stop_loss_hard_roi_pct: float = 35.0
    stop_grace_seconds: int = 30
    max_hold_seconds: int = 120
    maker_take_profit: bool = True
    use_l2_vwap_fills: bool = True
    enforce_depth_cap: bool = True
    max_spread_dollars: float = 0.10
    max_trades_per_candle: int = 3
    signal_mode: str = "raw_hl_vs_cf_strike"
    min_cf_edge_dollars: float = 0.08
    min_dir_z: float = 0.0
    min_vel10_bps: float = 0.0
    min_leadlag_imp_bps: float = -99.0
    structural_stop_z: Optional[float] = -1.8
    structural_stop_roi_pct: float = 45.0


# ---- Strategy profile definitions (mirrors streaming_backtest.py get_strategy_suite) ----
_STRATEGY_PROFILES: Dict[str, StrategyConfig] = {
    "S2": StrategyConfig(
        name="S2_TRIAL4_DEPLOYED_L2",
        max_entry_price=0.35,
        min_entry_price=0.20,
        certainty_arb_cap=0.78,
        min_edge_pct=4.0,
        min_prob_btc=65.0,
        min_prob_eth=60.0,
        min_sec_btc=360,
        min_sec_eth=240,
        max_sec_entry=500,
        take_profit_roi_pct=25.0,
        stop_loss_roi_pct=25.0,
        stop_loss_hard_roi_pct=35.0,
        stop_grace_seconds=30,
        max_hold_seconds=120,
        maker_take_profit=True,
        use_l2_vwap_fills=True,
        enforce_depth_cap=True,
        max_spread_dollars=0.10,
        max_trades_per_candle=3,
        signal_mode="raw_hl_vs_cf_strike",
    ),
    "S3": StrategyConfig(
        name="S3_REC_CF_DISLOC_NOSL",
        max_entry_price=0.45,
        min_entry_price=0.20,
        certainty_arb_cap=0.78,
        min_edge_pct=4.0,
        min_prob_btc=65.0,
        min_prob_eth=60.0,
        min_sec_btc=90,
        min_sec_eth=90,
        max_sec_entry=660,
        take_profit_roi_pct=55.0,
        stop_loss_roi_pct=25.0,
        stop_loss_hard_roi_pct=35.0,
        stop_grace_seconds=30,
        max_hold_seconds=600,
        maker_take_profit=True,
        use_l2_vwap_fills=True,
        enforce_depth_cap=True,
        max_spread_dollars=0.05,
        max_trades_per_candle=3,
        signal_mode="cf_disloc_structural",
        min_cf_edge_dollars=0.08,
        min_dir_z=0.0,
        min_vel10_bps=0.10,
        min_leadlag_imp_bps=-99.0,
        structural_stop_z=None,
    ),
    "S4": StrategyConfig(
        name="S4_REC_STRUCT_STOP_55TP",
        max_entry_price=0.48,
        min_entry_price=0.20,
        certainty_arb_cap=0.78,
        min_edge_pct=4.0,
        min_prob_btc=65.0,
        min_prob_eth=60.0,
        min_sec_btc=90,
        min_sec_eth=90,
        max_sec_entry=660,
        take_profit_roi_pct=55.0,
        stop_loss_roi_pct=25.0,
        stop_loss_hard_roi_pct=35.0,
        stop_grace_seconds=30,
        max_hold_seconds=600,
        maker_take_profit=True,
        use_l2_vwap_fills=True,
        enforce_depth_cap=True,
        max_spread_dollars=0.05,
        max_trades_per_candle=3,
        signal_mode="cf_disloc_structural",
        min_cf_edge_dollars=0.08,
        min_dir_z=0.05,
        min_vel10_bps=0.20,
        min_leadlag_imp_bps=-99.0,
        structural_stop_z=-1.8,
        structural_stop_roi_pct=45.0,
    ),
    "S5": StrategyConfig(
        name="S5_REC_LEADLAG_ENSEMBLE",
        max_entry_price=0.64,
        min_entry_price=0.20,
        certainty_arb_cap=0.78,
        min_edge_pct=4.0,
        min_prob_btc=63.0,
        min_prob_eth=62.0,
        min_sec_btc=120,
        min_sec_eth=120,
        max_sec_entry=660,
        take_profit_roi_pct=50.0,
        stop_loss_roi_pct=25.0,
        stop_loss_hard_roi_pct=35.0,
        stop_grace_seconds=30,
        max_hold_seconds=600,
        maker_take_profit=True,
        use_l2_vwap_fills=True,
        enforce_depth_cap=True,
        max_spread_dollars=0.05,
        max_trades_per_candle=1,
        signal_mode="cf_disloc_structural",
        min_cf_edge_dollars=0.09,
        min_dir_z=0.05,
        min_vel10_bps=0.0,
        min_leadlag_imp_bps=-0.50,
        structural_stop_z=-0.65,
        structural_stop_roi_pct=35.0,
    ),
}


class StrategySelector:
    """
    Selects the active strategy config based on the current Regime.

    Usage:
        selector = StrategySelector(detector)
        regime = detector.classify(ts, spot, strike)
        cfg = selector.select(regime)
        # apply cfg to engine for this evaluation cycle
    """

    def __init__(self, detector: Optional[RegimeDetector] = None,
                 default_strategy: str = "S5"):
        self.detector = detector or RegimeDetector.get_regime_detector()
        self.default_strategy = default_strategy
        self._active: Optional[StrategyConfig] = None
        self._active_regime: Optional[Regime] = None
        self._switch_count = 0
        self._history: list = []

    def select(self, regime: Regime) -> StrategyConfig:
        """
        Return the StrategyConfig for the given regime.
        Caches the active config; only switches when regime's recommendation changes.
        """
        recommended = regime.recommended_strategy
        if recommended not in _STRATEGY_PROFILES:
            recommended = self.default_strategy

        cfg = _STRATEGY_PROFILES[recommended]

        # Log switches for auditability
        if self._active is None or self._active.name != cfg.name:
            self._switch_count += 1
            self._history.append({
                "strategy": cfg.name,
                "regime": {
                    "volatility": regime.volatility,
                    "liquidity": regime.liquidity,
                    "trend": regime.trend,
                    "depth": regime.depth,
                },
                "confidence": regime.confidence,
                "rationale": regime.rationale,
            })
            self._active = cfg
            self._active_regime = regime

        return cfg

    def select_from_ticks(self, ts: float, spot: float, strike: float,
                          yes_bid: float, yes_ask: float,
                          yes_bid_sz: float, yes_ask_sz: float,
                          signed_flow_usd: float = 0.0) -> tuple[StrategyConfig, Regime]:
        """
        Convenience: ingest ticks, classify regime, select strategy.
        Returns (StrategyConfig, Regime).
        """
        self.detector.ingest(ts, spot, yes_bid, yes_ask, yes_bid_sz, yes_ask_sz, signed_flow_usd)
        regime = self.detector.classify(ts, spot, strike)
        cfg = self.select(regime)
        return cfg, regime

    @property
    def active(self) -> Optional[StrategyConfig]:
        return self._active

    @property
    def active_regime(self) -> Optional[Regime]:
        return self._active_regime

    @property
    def switch_count(self) -> int:
        return self._switch_count

    @property
    def history(self) -> list:
        return list(self._history)

    def force(self, strategy_name: str) -> StrategyConfig:
        """Force a specific strategy (override regime). Useful for testing or manual override."""
        if strategy_name not in _STRATEGY_PROFILES:
            raise ValueError(f"Unknown strategy: {strategy_name}. Available: {list(_STRATEGY_PROFILES)}")
        cfg = _STRATEGY_PROFILES[strategy_name]
        self._active = cfg
        self._switch_count += 1
        return cfg

    def list_strategies(self) -> list[str]:
        return list(_STRATEGY_PROFILES.keys())


# Module-level singleton
_selector: Optional[StrategySelector] = None


def get_strategy_selector(detector: Optional[RegimeDetector] = None) -> StrategySelector:
    global _selector
    if _selector is None:
        _selector = StrategySelector(detector=detector)
    return _selector


def reset_strategy_selector() -> None:
    global _selector
    _selector = None
