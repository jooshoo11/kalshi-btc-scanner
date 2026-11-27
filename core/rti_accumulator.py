"""
core/rti_accumulator.py — Generic Real-Time Index (RTI) CF Benchmarks Accumulator.

Kalshi 15-minute binary contracts (KXBTC15M, KXETH15M, KXSOL15M, KXXRP15M) settle on the
60-second simple arithmetic mean of the underlying CF Benchmarks index:
- BTC: BRTI (CME CF Bitcoin Real Time Index)
- ETH: ETHUSD_RTI (CME CF Ether-Dollar Real Time Index)
- SOL: SOLUSD_RTI (CME CF Solana-Dollar Real Time Index)
- XRP: XRPUSD_RTI (CME CF Ripple-Dollar Real Time Index)

During the final 60 seconds (t <= 60), the spot price contributes to the final settlement mean.
This class generalizes accumulation for any asset index.
"""

import math
import time
from typing import Any, Dict, List, Tuple, Optional


class RtiAccumulator:
    """
    Generic Real-Time Index Accumulator for CF Benchmarks 60-Second Settlement Window.
    """

    def __init__(self, index_name: str = "BRTI", window_seconds: int = 60):
        self.index_name = index_name.upper()
        self.window_seconds = window_seconds
        self.samples: List[Tuple[float, float]] = []  # List of (timestamp, spot_price)
        self.active_candle_id: Optional[str] = None

    def reset_for_candle(self, candle_id: Optional[str] = None):
        """Reset the accumulator when transitioning into a new 15-minute candle."""
        self.samples.clear()
        self.active_candle_id = candle_id

    def add_sample(self, spot: float, seconds_left: float, candle_id: Optional[str] = None):
        """
        Record a spot tick if within the final settlement window (seconds_left <= window_seconds).
        """
        if candle_id and candle_id != self.active_candle_id:
            self.reset_for_candle(candle_id)

        if 0 <= seconds_left <= self.window_seconds and spot > 0:
            now = time.time()
            self.samples.append((now, float(spot)))
            # Memory safety: prune stale ticks if candle transition was missed
            if len(self.samples) > self.window_seconds * 2:
                cutoff = now - (self.window_seconds * 2)
                self.samples = [s for s in self.samples if (s[0] if isinstance(s, (tuple, list)) else now) >= cutoff]

    def get_mean(self) -> float:
        """Calculate the arithmetic mean of all recorded settlement samples."""
        if not self.samples:
            return 0.0
        return sum(s[1] if isinstance(s, (tuple, list)) else float(s) for s in self.samples) / len(self.samples)

    def get_samples(self) -> List[Any]:
        """Return a copy of the recorded samples, normalized to floats."""
        return [s[1] if isinstance(s, (tuple, list)) else float(s) for s in self.samples]

    def has_samples(self) -> bool:
        """Check if any settlement samples have been recorded."""
        return len(self.samples) > 0

    def sample_count(self) -> int:
        """Return the number of samples recorded."""
        return len(self.samples)

    def calculate_projected_settlement(
        self,
        current_spot: float,
        seconds_left: float,
        delta: float = 0.0,
        eff_vol: float = 0.0,
        drift: float = 0.0,
    ) -> float:
        """
        Calculate expected final settlement mean for CF Benchmarks 60s window:
        - When seconds_left > window_seconds (60s):
          Project expected settlement over final 60s window [T-60, T] using
          current spot, delta, eff_vol, and gated Hyperliquid lead-lag/OFI drift.
        - When seconds_left <= window_seconds (60s):
          Blend the already-elapsed portion of the 60s window from RtiAccumulator
          (weighted by (window - seconds_left) / window) with the remaining expected
          path (seconds_left / window) so mu_settle converges directly to the official
          CF Benchmarks 60s settlement window as seconds_left -> 0.
        """
        w = float(self.window_seconds)
        s = float(seconds_left)

        if s > w:
            # Expected settlement over [T-60, T] incorporating projected drift
            return float(current_spot + drift)

        s_clamped = max(0.0, min(w, s))
        w_elapsed = (w - s_clamped) / w
        w_remaining = s_clamped / w

        if self.has_samples():
            elapsed_mean = self.get_mean()
        else:
            elapsed_mean = float(current_spot)

        # Remaining expected path over s seconds
        remaining_expected = float(current_spot + drift * w_remaining)

        mu_settle = (w_elapsed * elapsed_mean) + (w_remaining * remaining_expected)
        return float(mu_settle)

    def evaluate_certainty_arb(
        self,
        strike: float,
        current_spot: float,
        seconds_left: float,
        dollar_vol_15m: float = 95.0,
        max_vel_per_sec: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Evaluate terminal certainty arbitrage within the final 60s settlement window.
        Checks:
        1. Deterministic non-negativity bound (Sigma_m >= 60 * strike)
        2. Physical velocity bound (worst-case spot trajectory still clears strike)
        3. Cubic Brownian settlement variance Z-score (|Z| >= 3.72 -> P >= 0.9999)
        """
        if seconds_left > self.window_seconds or seconds_left <= 0 or strike <= 0 or current_spot <= 0:
            return {"is_certainty_arb": False, "side": None, "prob": 0.5, "z_score": 0.0, "mode": None}

        vals = self.get_samples()
        m = min(len(vals), self.window_seconds)
        if m > 0:
            sum_m = sum(vals[-m:])
            k = max(0.0, float(self.window_seconds - m))
        else:
            k = max(0.0, min(float(self.window_seconds), float(seconds_left)))
            sum_m = (float(self.window_seconds) - k) * current_spot

        target_sum = float(self.window_seconds) * strike

        # 1. Deterministic non-negativity bound (spot >= 0)
        if m > 0 and sum_m >= target_sum:
            return {"is_certainty_arb": True, "side": "YES", "prob": 1.0, "z_score": 99.0, "mode": "DETERMINISTIC"}

        # 2. Physical velocity bound
        if max_vel_per_sec is None:
            max_vel_per_sec = 50.0 if "BTC" in self.index_name or "BRTI" in self.index_name else 2.5
        worst_yes_sum = sum_m + k * max(0.0, current_spot - max_vel_per_sec * k)
        worst_no_sum = sum_m + k * (current_spot + max_vel_per_sec * k)
        if m >= 15 and worst_yes_sum > target_sum:
            return {"is_certainty_arb": True, "side": "YES", "prob": 0.9999, "z_score": 10.0, "mode": "VELOCITY_BOUND"}
        if m >= 15 and worst_no_sum < target_sum:
            return {"is_certainty_arb": True, "side": "NO", "prob": 0.9999, "z_score": -10.0, "mode": "VELOCITY_BOUND"}

        # 3. Cubic Brownian settlement variance bound
        mu_settle = (sum_m + k * current_spot) / float(self.window_seconds)
        sigma_1s = max(0.001, dollar_vol_15m / 30.0)
        if k > 0.0:
            var_settle = (sigma_1s ** 2) * k * (k + 1.0) * (2.0 * k + 1.0) / 21600.0
            sigma_settle = max(0.001, math.sqrt(var_settle))
        else:
            sigma_settle = 0.001

        z_score = (mu_settle - strike) / sigma_settle
        if abs(z_score) >= 3.72 and m >= 15:
            side = "YES" if z_score > 0 else "NO"
            return {"is_certainty_arb": True, "side": side, "prob": 0.9999, "z_score": round(z_score, 2), "mode": "CUBIC_Z_BOUND"}

        return {"is_certainty_arb": False, "side": None, "prob": 0.5, "z_score": round(z_score, 2), "mode": None}


# Backward-compatible alias for existing BTC code
BRTIAccumulator = RtiAccumulator
