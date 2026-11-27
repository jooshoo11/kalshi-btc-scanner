#!/usr/bin/env python3
"""
core/regime_detector.py — Market Regime Detection for Adaptive Strategy Selection.

Classifies the current market regime from live tick data so the bot can
dynamically select the best-performing strategy configuration (S2 deployed,
S3 CF dislocation, S5 lead-lag ensemble, etc.) rather than running one fixed
parameter set regardless of conditions.

Regime dimensions:
  - Volatility: low / normal / high (realized 15m vol vs baseline)
  - Liquidity: tight / normal / wide (avg bid-ask spread)
  - Trend: ranging / trending_up / trending_down (spot vs strike, momentum)
  - Book depth: deep / normal / thin (visible L2 depth)

Output: Regime namedtuple + recommended strategy name.
"""

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple


@dataclass
class Regime:
    """Classified market regime with confidence and recommended strategy."""
    volatility: str          # "low", "normal", "high"
    liquidity: str           # "tight", "normal", "wide"
    trend: str               # "ranging", "trending_up", "trending_down"
    depth: str               # "deep", "normal", "thin"
    confidence: float        # 0.0 - 1.0, how certain the classification is
    recommended_strategy: str  # "S2", "S3", "S4", "S5"
    rationale: str           # human-readable explanation


class RegimeDetector:
    """
    Rolling-window regime detector.

    Feeds: per-second spot price, Kalshi best bid/ask, book size, signed flow.
    Maintains trailing windows for realized volatility, spread, momentum, depth.
    """

    # ---- thresholds (tunable) ----
    VOL_LOW_PCT = 0.6      # realized vol < 60% of baseline  -> low
    VOL_HIGH_PCT = 1.6     # realized vol > 160% of baseline -> high
    SPREAD_TIGHT_CENTS = 3.0   # avg spread <= 3c -> tight
    SPREAD_WIDE_CENTS = 8.0    # avg spread >= 8c -> wide
    MOMENTUM_STRONG_BPS = 25.0 # |20s velocity| >= 25 bps -> trending
    DEPTH_THIN_MULT = 0.5      # depth < 50% of baseline -> thin
    DEPTH_DEEP_MULT = 1.5      # depth > 150% of baseline -> deep

    def __init__(self, window_seconds: int = 300, baseline_vol_dollars: Optional[float] = None,
                 baseline_depth: Optional[float] = None):
        self.window_seconds = window_seconds
        self.baseline_vol_dollars = baseline_vol_dollars
        self.baseline_depth = baseline_depth

        # Rolling windows: deque of (ts, value)
        self._spot_window: Deque[Tuple[float, float]] = deque()
        self._spread_window: Deque[Tuple[float, float]] = deque()
        self._depth_window: Deque[Tuple[float, float]] = deque()
        self._flow_window: Deque[Tuple[float, float]] = deque()

        # Last regime for stability (avoid flip-flopping)
        self._last_regime: Optional[Regime] = None
        self._regime_hold_seconds = 60  # hold a regime for at least 60s before reclassifying
        self._last_classify_ts: float = 0.0

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------
    def ingest(self, ts: float, spot: float, yes_bid: float, yes_ask: float,
               yes_bid_sz: float, yes_ask_sz: float, signed_flow_usd: float = 0.0) -> None:
        """Add one second of market data."""
        now = ts
        # prune old
        cutoff = now - self.window_seconds
        while self._spot_window and self._spot_window[0][0] < cutoff:
            self._spot_window.popleft()
        while self._spread_window and self._spread_window[0][0] < cutoff:
            self._spread_window.popleft()
        while self._depth_window and self._depth_window[0][0] < cutoff:
            self._depth_window.popleft()
        while self._flow_window and self._flow_window[0][0] < cutoff:
            self._flow_window.popleft()

        self._spot_window.append((now, spot))
        spread_cents = (yes_ask - yes_bid) * 100.0 if (yes_bid > 0 and yes_ask > 0) else 0.0
        self._spread_window.append((now, spread_cents))
        depth = yes_bid_sz + yes_ask_sz
        self._depth_window.append((now, depth))
        self._flow_window.append((now, signed_flow_usd))

    # ------------------------------------------------------------------
    # Classify
    # ------------------------------------------------------------------
    def classify(self, ts: float, spot: float, strike: float) -> Regime:
        """Return the current regime. Holds previous regime within hold window."""
        now = ts
        if (self._last_regime is not None and
                now - self._last_classify_ts < self._regime_hold_seconds):
            return self._last_regime

        vol = self._classify_volatility(spot)
        liq = self._classify_liquidity()
        trend = self._classify_trend(spot, strike)
        depth = self._classify_depth()

        strategy, rationale, confidence = self._map_to_strategy(vol, liq, trend, depth, spot, strike)

        regime = Regime(
            volatility=vol,
            liquidity=liq,
            trend=trend,
            depth=depth,
            confidence=confidence,
            recommended_strategy=strategy,
            rationale=rationale,
        )
        self._last_regime = regime
        self._last_classify_ts = now
        return regime

    # ------------------------------------------------------------------
    # Dimension classifiers
    # ------------------------------------------------------------------
    def _classify_volatility(self, spot: float) -> str:
        if len(self._spot_window) < 10:
            return "normal"
        prices = [p for _, p in self._spot_window]
        # realized vol: sum of abs(log returns) scaled
        rets = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices)) if prices[i - 1] > 0]
        if not rets:
            return "normal"
        realized = sum(abs(r) for r in rets) * math.sqrt(len(rets))  # rough annualized proxy
        baseline = self.baseline_vol_dollars if self.baseline_vol_dollars else realized
        if baseline <= 0:
            return "normal"
        ratio = realized / baseline
        if ratio < self.VOL_LOW_PCT:
            return "low"
        if ratio > self.VOL_HIGH_PCT:
            return "high"
        return "normal"

    def _classify_liquidity(self) -> str:
        if not self._spread_window:
            return "normal"
        spreads = [s for _, s in self._spread_window if s > 0]
        if not spreads:
            return "normal"
        avg = sum(spreads) / len(spreads)
        if avg <= self.SPREAD_TIGHT_CENTS:
            return "tight"
        if avg >= self.SPREAD_WIDE_CENTS:
            return "wide"
        return "normal"

    def _classify_trend(self, spot: float, strike: float) -> str:
        if len(self._spot_window) < 5:
            return "ranging"
        prices = [p for _, p in self._spot_window]
        # 20s momentum (last vs first in window, or last 5 samples)
        n = min(5, len(prices))
        if n < 2:
            return "ranging"
        vel_bps = ((prices[-1] - prices[-n]) / max(0.01, prices[-n])) * 10000.0
        # distance from strike
        dist_bps = ((spot - strike) / max(0.01, strike)) * 10000.0
        if abs(vel_bps) >= self.MOMENTUM_STRONG_BPS:
            if vel_bps > 0:
                return "trending_up"
            return "trending_down"
        # strong distance from strike also implies directional bias
        if abs(dist_bps) >= 50.0:
            return "trending_up" if dist_bps > 0 else "trending_down"
        return "ranging"

    def _classify_depth(self) -> str:
        if not self._depth_window:
            return "normal"
        depths = [d for _, d in self._depth_window if d > 0]
        if not depths:
            return "normal"
        avg = sum(depths) / len(depths)
        baseline = self.baseline_depth if self.baseline_depth else avg
        if baseline <= 0:
            return "normal"
        ratio = avg / baseline
        if ratio < self.DEPTH_THIN_MULT:
            return "thin"
        if ratio > self.DEPTH_DEEP_MULT:
            return "deep"
        return "normal"

    # ------------------------------------------------------------------
    # Strategy mapping
    # ------------------------------------------------------------------
    def _map_to_strategy(self, vol: str, liq: str, trend: str, depth: str,
                         spot: float, strike: float) -> Tuple[str, str, float]:
        """
        Map regime dimensions to a strategy name.

        S2_TRIAL4_DEPLOYED_L2: conservative, L2 VWAP fills, 120s time exit, -25% SL
           Best for: normal vol, normal liquidity, ranging markets
        S3_REC_CF_DISLOC_NOSL: CF dislocation, +55% maker TP, no noise SL
           Best for: low vol, tight spreads, ranging (mean-reversion) markets
        S4_REC_STRUCT_STOP_55TP: CF dislocation + structural stop (z<=-1.8)
           Best for: high vol, trending markets (protect against breakdown)
        S5_REC_LEADLAG_ENSEMBLE: lead-lag filter + +55% TP + structural SL
           Best for: normal-high vol, tight/deep book, trending (momentum) markets
        """
        rationale_parts: List[str] = []
        confidence = 0.5

        # High volatility + trending -> structural protection (S4)
        if vol == "high" and trend != "ranging":
            return ("S4",
                    f"High vol ({vol}) + {trend} -> S4 structural stop protects against breakdown",
                    0.85)

        # Low volatility + tight spread + ranging -> mean reversion (S3)
        if vol == "low" and liq == "tight" and trend == "ranging":
            return ("S3",
                    f"Low vol + tight spread + ranging -> S3 CF dislocation, no noise SL",
                    0.85)

        # Trending + tight/deep book -> momentum ensemble (S5)
        if trend != "ranging" and liq in ("tight", "normal") and depth in ("deep", "normal"):
            return ("S5",
                    f"{trend} + {liq} liquidity + {depth} book -> S5 lead-lag ensemble",
                    0.80)

        # Wide spread / thin book -> conservative deployed (S2)
        if liq == "wide" or depth == "thin":
            return ("S2",
                    f"{liq} liquidity / {depth} depth -> S2 conservative L2 VWAP with depth cap",
                    0.75)

        # Default: normal conditions -> S5 ensemble (best overall OOS on 5-day data)
        return ("S5",
                f"Normal regime ({vol} vol, {liq} liq, {trend}, {depth} depth) -> S5 lead-lag ensemble",
                0.60)


# Singleton instance for use across the app
_regime_detector: Optional[RegimeDetector] = None


def get_regime_detector() -> RegimeDetector:
    global _regime_detector
    if _regime_detector is None:
        _regime_detector = RegimeDetector()
    return _regime_detector


def reset_regime_detector() -> None:
    global _regime_detector
    _regime_detector = None
