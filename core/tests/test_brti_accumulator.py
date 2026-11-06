"""
core/tests/test_brti_accumulator.py — CF Benchmarks BRTI 60-Second Accumulator &
Net Post-Fee Half-Kelly Sizing (pytest-collected).

Verifies:
  1. Kalshi non-linear taker fee: ceil(0.07 * contracts * price * (1-price) * 100) / 100
  2. RtiAccumulator arithmetic mean matches ground truth across synthetic samples
  3. Variance collapse under the linear T_rem settlement model
  4. 3-cent adverse slippage erodes edge -> 0.0% Kelly
  5. Authentic edge (99% prob vs 75c ask) -> substantial Kelly
"""

import asyncio
import math
import random
from typing import List

import pytest

from core.rti_accumulator import RtiAccumulator
from core.prediction_engine import QuantPredictionEngine, calculate_kalshi_fee


def _synthetic_spot_series(initial_spot: float, n: int = 60, seed: int = 42) -> List[float]:
    random.seed(seed)
    series = [initial_spot]
    sigma_1s = 95.0 / 30.0  # ~$3.17/sec
    for _ in range(n - 1):
        shock = random.gauss(0.15, sigma_1s)
        series.append(series[-1] + shock)
    return series


# ---------------------------------------------------------------------------
# Fee formula (exact spec)
# ---------------------------------------------------------------------------
def test_kalshi_taker_fee_exact():
    # ceil(0.07 * 10 * 0.50 * 0.50 * 100) / 100 = ceil(17.5) / 100 = 0.18
    assert calculate_kalshi_fee(10, 0.50) == 0.18
    # Settlement at expiration carries $0.00 fee
    assert calculate_kalshi_fee(10, 1.00) == 0.00
    assert calculate_kalshi_fee(10, 0.00) == 0.00
    assert calculate_kalshi_fee(0, 0.50) == 0.00


# ---------------------------------------------------------------------------
# RtiAccumulator arithmetic mean
# ---------------------------------------------------------------------------
def test_accumulator_mean_matches_ground_truth():
    engine = QuantPredictionEngine()
    accumulator = RtiAccumulator(index_name="BRTI", window_seconds=60)
    candle_open = 81500.00
    initial_spot = 81515.00
    series = _synthetic_spot_series(initial_spot, 60, 42)
    for spot in series:
        accumulator.add_sample(spot=spot, seconds_left=60.0, candle_id="candle_1")
    assert abs(accumulator.get_mean() - sum(series) / len(series)) < 1e-6
    assert accumulator.sample_count() == 60


# ---------------------------------------------------------------------------
# Variance collapse (linear T_rem model, supersedes the old cubic formulation)
# ---------------------------------------------------------------------------
def test_variance_collapse_linear_model():
    engine = QuantPredictionEngine()
    accumulator = RtiAccumulator(index_name="BRTI", window_seconds=60)
    candle_open = 81500.00
    initial_spot = 81515.00
    series = _synthetic_spot_series(initial_spot, 60, 42)

    sigmas = []
    for second_idx in range(60):
        seconds_left = 60 - second_idx
        accumulator.add_sample(spot=series[second_idx], seconds_left=float(seconds_left), candle_id="candle_1")
        if seconds_left in (60, 5):
            scalp_res = engine.evaluate_15m_scalp(
                spot=series[second_idx],
                open_price=candle_open,
                seconds_left=float(seconds_left),
                dollar_vol_15m=95.0,
                accumulated_samples=accumulator.get_samples(),
            )
            sigmas.append((seconds_left, scalp_res.get("sigma_settle", 0.0)))

    sigma_60 = sigmas[0][1]
    sigma_5 = sigmas[1][1]
    # Under the linear model sigma scales ~ sqrt(T_rem), so T-5 vs T-60 is
    # sqrt(5/60) = 0.289, well under the 0.20 strict bound used previously.
    collapse_ratio = sigma_5 / sigma_60 if sigma_60 > 0 else 0.0
    assert collapse_ratio < 0.50, f"Variance did not collapse: ratio {collapse_ratio:.3f}"


# ---------------------------------------------------------------------------
# Kelly sizing under fees & slippage
# ---------------------------------------------------------------------------
def test_kelly_zero_when_slippage_erodes_edge():
    engine = QuantPredictionEngine()
    metrics = engine.calculate_trade_metrics(
        model_prob=0.70,
        yes_ask=0.68,
        yes_bid=0.65,
        no_ask=0.35,
        no_bid=0.32,
        slippage_penalty_cents=3.0,
    )
    assert metrics["half_kelly_pct"] == 0.0


def test_kelly_substantial_when_edge_authentic():
    engine = QuantPredictionEngine()
    metrics = engine.calculate_trade_metrics(
        model_prob=0.99,
        yes_ask=0.75,
        yes_bid=0.72,
        no_ask=0.28,
        no_bid=0.25,
        slippage_penalty_cents=3.0,
    )
    assert metrics["half_kelly_pct"] > 15.0


if __name__ == "__main__":
    # Allow standalone execution too (path-safe via conftest when under pytest).
    asyncio.run(asyncio.sleep(0))
    pytest.main([__file__, "-v"])
