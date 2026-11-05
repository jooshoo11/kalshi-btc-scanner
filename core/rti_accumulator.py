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

import time
from typing import List, Tuple, Optional


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
            self.samples.append((time.time(), float(spot)))

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

    def calculate_projected_settlement(self, current_spot: float, seconds_left: float) -> float:
        """
        Calculate expected final settlement mean:
        Projected = (accumulated_sum + remaining_seconds * current_spot) / total_window_seconds
        """
        if seconds_left > self.window_seconds:
            return current_spot

        if not self.samples:
            return current_spot

        accum_sum = sum(s[1] if isinstance(s, (tuple, list)) else float(s) for s in self.samples)
        accum_count = len(self.samples)
        # Approximate 1 sample per second remaining
        rem_seconds = max(0, int(seconds_left))
        total_projected_count = accum_count + rem_seconds
        if total_projected_count == 0:
            return current_spot
        return (accum_sum + (rem_seconds * current_spot)) / total_projected_count


# Backward-compatible alias for existing BTC code
BRTIAccumulator = RtiAccumulator
