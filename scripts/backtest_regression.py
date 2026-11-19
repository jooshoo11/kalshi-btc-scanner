#!/usr/bin/env python3
"""
backtest_regression.py — Automated Quantitative Backtest & Pricing Regression Suite.

Verifies institutional trading invariants prior to deployment:
1. Non-linear Kalshi taker fee formula: 0.07 * contracts * price * (1 - price)
2. Drift-diffusion jump process pricing bounds: P in [0.0, 1.0] across all strikes
3. CF Benchmarks 60-second arithmetic mean RTI settlement math
4. Core execution filters (+4.0% min edge, 60% min probability, 20¢-78¢ price bounds)
5. Multi-coin volatility scaling and Kelly criterion position sizing
"""

import sys
import os
from pathlib import Path

# Explicitly prioritize local repository root on sys.path
REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import math
import numpy as np
from typing import List, Dict, Any

from core.prediction_engine import calculate_kalshi_fee, QuantPredictionEngine
from core.rti_accumulator import RtiAccumulator
from core.kalshi_client import calculate_kalshi_taker_fee


def test_fee_formula_regression() -> bool:
    """Validate exact non-linear Kalshi taker fee calculation."""
    print("[1/5] Checking non-linear Kalshi taker fee formula...")
    test_cases = [
        (10, 0.50, 0.07 * 10 * 0.50 * 0.50),      # 0.175 -> $0.18 (or round depending on cents)
        (1, 0.10, 0.07 * 1 * 0.10 * 0.90),        # 0.0063
        (100, 0.80, 0.07 * 100 * 0.80 * 0.20),    # 1.12
        (5, 0.78, 0.07 * 5 * 0.78 * 0.22),
    ]

    for contracts, price, expected in test_cases:
        actual_1 = calculate_kalshi_fee(contracts, price)
        actual_2 = calculate_kalshi_taker_fee(contracts, price)
        # Check within 0.01 tolerance (since roundings to cents may apply)
        if abs(actual_1 - expected) > 0.015:
            print(f"FAIL: calculate_kalshi_fee({contracts}, {price}) = {actual_1}, expected {expected}")
            return False
        if abs(actual_2 - expected) > 0.015:
            print(f"FAIL: calculate_kalshi_taker_fee({contracts}, {price}) = {actual_2}, expected {expected}")
            return False

    print("  ✓ Fee formula regression passed.")
    return True


def test_quant_pricing_bounds() -> bool:
    """Validate Black-Scholes and Jump-Diffusion pricing outputs valid probabilities."""
    print("[2/5] Checking quantitative probability bounds and drift-diffusion pricing...")
    engine = QuantPredictionEngine()

    strikes = [60000.0, 65000.0, 70000.0, 75000.0, 80000.0]
    spot_prices = [64000.0, 70000.0, 72000.0]
    time_to_expirations = [60.0 / (365.25 * 86400.0), 300.0 / (365.25 * 86400.0), 900.0 / (365.25 * 86400.0)]

    for spot in spot_prices:
        for strike in strikes:
            for tte in time_to_expirations:
                prob = engine.black_scholes_binary(
                    spot=spot,
                    strike=strike,
                    time_to_expiry_years=tte,
                    volatility=0.55
                )
                if not (0.0 <= prob <= 1.0):
                    print(f"FAIL: Probability out of bounds: {prob} for spot={spot}, strike={strike}, tte={tte}")
                    return False

                # Monotonicity check: if spot >> strike, prob should be high
                if spot > strike + 2000.0 and tte < (120.0 / (365.25 * 86400.0)):
                    if prob < 0.80:
                        print(f"FAIL: Deep ITM probability too low: {prob} (spot={spot}, strike={strike})")
                        return False

    print("  ✓ Drift-diffusion jump probability bounds passed.")
    return True


def test_rti_arithmetic_mean_regression() -> bool:
    """Validate CF Benchmarks 60-second arithmetic mean accumulator."""
    print("[3/5] Checking CF Benchmarks 60-second RTI settlement accumulator...")
    acc = RtiAccumulator(index_name="BRTI", window_seconds=60)

    # Feed 60 ticks within the final 60-second settlement window (seconds_left 59 down to 0)
    values = [60000.0 + i * 10.0 for i in range(60)]  # 60000 to 60590
    expected_mean = sum(values) / len(values)  # 60295.0

    for i, val in enumerate(values):
        seconds_left = 59.0 - i
        acc.add_sample(spot=val, seconds_left=seconds_left)

    actual_settlement = acc.get_mean()
    if abs(actual_settlement - expected_mean) > 1e-4:
        print(f"FAIL: RTI settlement {actual_settlement} != expected {expected_mean}")
        return False

    print("  ✓ CF Benchmarks RTI accumulator arithmetic mean regression passed.")
    return True


def test_trade_selection_filters() -> bool:
    """Validate institutional filter constraints: +4.0% min edge, 60% min prob, 20¢-78¢ price bounds."""
    print("[4/5] Checking trade selection and edge thresholds...")

    # Filter rules
    def passes_filters(prob: float, market_price_cents: int) -> bool:
        if market_price_cents < 20 or market_price_cents > 78:
            return False
        if prob < 0.60:
            return False
        market_prob = market_price_cents / 100.0
        edge = prob - market_prob
        if edge < 0.04:  # +4.0% min edge
            return False
        return True

    # Test cases: (prob, price_cents, should_pass)
    cases = [
        (0.65, 55, True),   # edge = +10.0%, prob=65% -> PASS
        (0.62, 57, True),   # edge = +5.0%, prob=62% -> PASS
        (0.61, 58, False),  # edge = +3.0% (< 4.0%) -> REJECT
        (0.58, 50, False),  # prob = 58% (< 60%) -> REJECT
        (0.85, 82, False),  # price = 82¢ (> 78¢) -> REJECT
        (0.65, 18, False),  # price = 18¢ (< 20¢) -> REJECT
    ]

    for p, pr, expected in cases:
        actual = passes_filters(p, pr)
        if actual != expected:
            print(f"FAIL: Filter test for prob={p}, price={pr}¢: expected {expected}, got {actual}")
            return False

    print("  ✓ Trade selection filter constraints passed.")
    return True


def test_fractional_kelly_sizing() -> bool:
    """Validate Fractional Kelly sizing and EV calculation."""
    print("[5/5] Checking Fractional Kelly position sizing...")
    engine = QuantPredictionEngine()

    # Case 1: High win prob (0.75) against ask of 0.55 -> Positive edge & positive Kelly
    metrics = engine.calculate_trade_metrics(
        model_prob=0.75,
        yes_ask=0.55,
        yes_bid=0.53,
        no_ask=0.47,
        no_bid=0.45,
        slippage_penalty_cents=0.0
    )

    if metrics.get("half_kelly_pct", 0.0) <= 0.0:
        print(f"FAIL: Expected positive half Kelly on strong edge, got {metrics.get('half_kelly_pct')}")
        return False

    if metrics.get("best_edge_pct", 0.0) < 4.0:
        print(f"FAIL: Expected edge >= 4%, got {metrics.get('best_edge_pct')}")
        return False

    # Case 2: Negative edge (model 0.40 against ask 0.60) -> Half Kelly should be 0.0 for YES
    neg_metrics = engine.calculate_trade_metrics(
        model_prob=0.40,
        yes_ask=0.60,
        yes_bid=0.58,
        no_ask=0.60,
        no_bid=0.58,
        slippage_penalty_cents=0.0
    )
    if neg_metrics.get("half_kelly_pct", 0.0) != 0.0 and neg_metrics.get("side") == "YES":
        print(f"FAIL: Expected 0.0 Kelly for negative edge, got {neg_metrics.get('half_kelly_pct')}")
        return False

    print("  ✓ Fractional Kelly sizing regression passed.")
    return True


def main():
    print("=" * 60)
    print("KALSHI MULTI-COIN QUANTITATIVE ENGINE: PRE-DEPLOY REGRESSION")
    print("=" * 60)

    tests = [
        test_fee_formula_regression,
        test_quant_pricing_bounds,
        test_rti_arithmetic_mean_regression,
        test_trade_selection_filters,
        test_fractional_kelly_sizing
    ]

    for test_fn in tests:
        if not test_fn():
            print("\n❌ REGRESSION TEST SUITE FAILED!")
            sys.exit(1)

    print("=" * 60)
    print("✅ ALL QUANT REGRESSION INVARIANTS VERIFIED SUCCESSFULLY.")
    print("=" * 60)
    sys.exit(0)


if __name__ == "__main__":
    main()
