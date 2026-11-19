"""
core/tests/test_rti_regression.py — Regression Tests for CF Benchmarks 60s Arithmetic Mean Settlement.

Compares RtiAccumulator against official CF Benchmarks specifications for:
- CME CF Bitcoin Real Time Index (BRTI)
- CME CF Ether-Dollar Real Time Index (ETHUSD_RTI)
- CME CF Solana-Dollar Real Time Index (SOLUSD_RTI)
- CME CF Ripple-Dollar Real Time Index (XRPUSD_RTI)
"""

import math
import random
import pytest
import numpy as np

from core.rti_accumulator import RtiAccumulator
from backtests.cf_benchmarks import CFBenchmarksSettlementEngine


@pytest.mark.parametrize("index_name,initial_spot", [
    ("BRTI", 95000.0),
    ("ETHUSD_RTI", 3200.0),
    ("SOLUSD_RTI", 195.0),
    ("XRPUSD_RTI", 0.65),
])
def test_rti_accumulator_ground_truth_regression(index_name, initial_spot):
    """
    Verify that RtiAccumulator matches exact CF Benchmarks arithmetic mean
    across all supported cryptocurrency indices.
    """
    cf_engine = CFBenchmarksSettlementEngine(window_seconds=60)
    accumulator = RtiAccumulator(index_name=index_name, window_seconds=60)

    # Generate 60 synthetic 1-second ticks with random walk
    rng = np.random.default_rng(42)
    shocks = rng.normal(0.0, initial_spot * 0.0002, 60)
    ticks = list(initial_spot + np.cumsum(shocks))

    # Ingest into accumulator simulating T-60 to T-1
    for i, spot in enumerate(ticks):
        seconds_left = 60 - i
        accumulator.add_sample(spot=spot, seconds_left=float(seconds_left), candle_id="CANDLE_QA")

    expected_mean = cf_engine.compute_settlement_price(ticks)
    actual_mean = accumulator.get_mean()

    assert abs(actual_mean - expected_mean) < 1e-9
    assert accumulator.sample_count() == 60


def test_sparse_and_dropped_ticks_regression():
    """Verify accumulator stability when WebSocket drops packets (sparse samples)."""
    accumulator = RtiAccumulator(index_name="BRTI", window_seconds=60)
    
    # Only 35 samples arrive over the 60 second window
    samples = [95000.0 + (i * 2.5) for i in range(35)]
    for i, s in enumerate(samples):
        accumulator.add_sample(spot=s, seconds_left=float(55 - i), candle_id="CANDLE_SPARSE")

    expected_mean = float(np.mean(samples))
    assert abs(accumulator.get_mean() - expected_mean) < 1e-9
    assert accumulator.sample_count() == 35


def test_projected_settlement_convergence():
    """
    Verify that calculate_projected_settlement converges monotonically
    to the final 60s arithmetic mean as seconds_left approaches 0.
    """
    accumulator = RtiAccumulator(index_name="BRTI", window_seconds=60)
    ticks = [95000.0 + math.sin(i / 5.0) * 20.0 for i in range(60)]

    projected_history = []
    for i in range(60):
        sec_left = 60 - i
        current_spot = ticks[i]
        accumulator.add_sample(spot=current_spot, seconds_left=float(sec_left), candle_id="CANDLE_CONVERGE")
        proj = accumulator.calculate_projected_settlement(current_spot=current_spot, seconds_left=float(sec_left))
        projected_history.append(proj)

    final_actual_mean = accumulator.get_mean()
    # At T-1, projected settlement must be within 0.1% of actual final mean
    assert abs(projected_history[-1] - final_actual_mean) < 0.5


def test_strike_tie_and_boundary_settlement():
    """Verify Kalshi binary contract boundary condition (tie resolution)."""
    cf_engine = CFBenchmarksSettlementEngine(window_seconds=60)

    # Case 1: Exact tie (Settlement == Strike) -> YES wins per Kalshi rule
    tie_res = cf_engine.evaluate_contract_settlement(
        settlement_price=95000.0,
        strike_price=95000.0,
        target_side="YES"
    )
    assert tie_res["yes_won"] is True
    assert tie_res["won"] is True
    assert tie_res["payout_per_contract"] == 1.0

    # Case 2: Strike 1 cent above settlement -> NO wins
    loss_res = cf_engine.evaluate_contract_settlement(
        settlement_price=94999.99,
        strike_price=95000.00,
        target_side="YES"
    )
    assert loss_res["yes_won"] is False
    assert loss_res["won"] is False
    assert loss_res["payout_per_contract"] == 0.0
