#!/usr/bin/env python3
"""
test_brti_accumulator.py — High-Fidelity Simulation & Verification of
CF Benchmarks BRTI 60-Second Accumulator & Net Post-Fee Half-Kelly Sizing.

Simulates the final 60 seconds of a 15-minute Kalshi Bitcoin candle (KXBTC15M):
1. Verifies continuous accumulation of 1-second spot samples into BRTIAccumulator.
2. Validates cubic variance collapse: Var ~ (1/3) * (sigma_1s^2) * (T_rem^3) / 3600.
3. Tests Net Post-Fee Half-Kelly sizing with 3¢ adverse slippage & Kalshi taker fee.
4. Asserts mathematical consistency and edge detection.
"""

import sys
import math
import random
from typing import List, Dict, Any

from btc_metrics import BRTIAccumulator
from prediction_engine import QuantPredictionEngine, calculate_kalshi_fee


def run_simulation_test():
    print("=" * 80)
    print("  CF BENCHMARKS BRTI 60-SECOND ACCUMULATOR & NET HALF-KELLY TEST HARNESS")
    print("=" * 80)

    # Initialize Engine & Accumulator
    engine = QuantPredictionEngine()
    accumulator = BRTIAccumulator()

    candle_open = 81500.00
    initial_spot = 81515.00 # Candle is currently green (+15 delta)
    dollar_vol_15m = 95.00 # Typical 15m dollar vol ($95)
    sigma_1s = dollar_vol_15m / 30.0 # ~$3.17 / second

    print(f"[*] Candle Open: ${candle_open:,.2f}")
    print(f"[*] Initial Spot (at T-60s): ${initial_spot:,.2f} (+${initial_spot - candle_open:,.2f} delta)")
    print(f"[*] 15m Dollar Vol: ${dollar_vol_15m:.2f} | 1-Second Sigma: ${sigma_1s:.2f}/sec\n")

    # Generate synthetic 60-second stochastic price path (Geometric Brownian Motion)
    random.seed(42) # Deterministic seed for reproducible testing
    spot_series = [initial_spot]
    for _ in range(59):
        # 1-second jump with $3.17 std dev and slight positive momentum
        shock = random.gauss(0.15, sigma_1s)
        next_spot = spot_series[-1] + shock
        spot_series.append(next_spot)

    # Print Header
    header = (
        f"{'Sec Left':>8} | {'Spot ($)':>10} | {'Cumul Avg':>10} | "
        f"{'Sigma ($)':>10} | {'Z-Score':>8} | {'Prob YES':>9} | {'Net Half-Kelly':>14} | {'Signal'}"
    )
    divider = "-" * 8 + "+" + "-" * 12 + "+" + "-" * 12 + "+" + "-" * 12 + "+" + "-" * 10 + "+" + "-" * 11 + "+" + "-" * 16 + "+" + "-" * 18

    print(header)
    print(divider)

    logged_snapshots = []
    checkpoint_seconds = [60, 50, 40, 30, 20, 10, 5, 2, 1]

    for second_idx in range(60):
        seconds_left = 60 - second_idx
        current_spot = spot_series[second_idx]
        
        # Add sample to accumulator
        accumulator.samples.append(current_spot)

        if seconds_left in checkpoint_seconds:
            # Evaluate using BRTI accumulator model
            scalp_res = engine.evaluate_15m_scalp(
                spot=current_spot,
                open_price=candle_open,
                seconds_left=seconds_left,
                dollar_vol_15m=dollar_vol_15m,
                accumulated_samples=accumulator.get_samples()
            )

            # Synthetic market prices (market quotes 65¢ bid / 68¢ ask)
            yes_ask = 0.68
            yes_bid = 0.65
            no_ask = 0.35
            no_bid = 0.32

            metrics = engine.calculate_trade_metrics(
                model_prob=scalp_res["prob_yes"],
                yes_ask=yes_ask,
                yes_bid=yes_bid,
                no_ask=no_ask,
                no_bid=no_bid,
                slippage_penalty_cents=3.0
            )

            mu_settle = scalp_res.get("mu_settle", current_spot)
            sigma_settle = scalp_res.get("sigma_settle", 0.0)
            z_score = scalp_res["z_score"]
            prob_yes = scalp_res["prob_yes_pct"]
            half_kelly = metrics["half_kelly_pct"]
            signal = metrics["signal"]

            print(
                f"{seconds_left:>8} | {current_spot:>10.2f} | {mu_settle:>10.2f} | "
                f"${sigma_settle:>9.2f} | {z_score:>8.2f} | {prob_yes:>8.1f}% | {half_kelly:>13.1f}% | {signal}"
            )

            logged_snapshots.append({
                "seconds_left": seconds_left,
                "sigma_settle": sigma_settle,
                "prob_yes": scalp_res["prob_yes"],
                "half_kelly": half_kelly,
                "z_score": z_score
            })

    print(divider)

    # =========================================================================
    # MATHEMATICAL VERIFICATION & ASSERTIONS
    # =========================================================================
    print("\n[+] RUNNING MATHEMATICAL ASSERTIONS...")

    # 1. Verify Variance Collapse: Sigma at 5s must be < 20% of Sigma at 60s
    sigma_60 = logged_snapshots[0]["sigma_settle"]
    sigma_5 = logged_snapshots[-3]["sigma_settle"]
    collapse_ratio = sigma_5 / sigma_60
    assert collapse_ratio < 0.20, f"Variance collapse failed: Ratio was {collapse_ratio:.3f}"
    print(f"  ✓ Cubic Variance Collapse: Sigma shrunk from ${sigma_60:.2f} at T-60s to ${sigma_5:.2f} at T-5s ({collapse_ratio*100:.1f}% of initial).")

    # 2. Verify Final Settlement Mean
    final_brti_avg = accumulator.get_mean()
    ground_truth_mean = sum(spot_series) / len(spot_series)
    assert abs(final_brti_avg - ground_truth_mean) < 1e-6, "Accumulator mean mismatch!"
    print(f"  ✓ Ground Truth Settlement Mean: ${final_brti_avg:,.2f} strictly verified across 60 samples.")

    # 3. Verify Kalshi Taker Fee Formula
    test_fee_50 = calculate_kalshi_fee(10, 0.50)
    # ceil(0.07 * 10 * 0.50 * 0.50 * 100) / 100 = ceil(17.5) / 100 = 0.18
    assert test_fee_50 == 0.18, f"Fee formula mismatch: Expected $0.18, got ${test_fee_50}"
    test_fee_expiry = calculate_kalshi_fee(10, 1.00)
    assert test_fee_expiry == 0.00, f"Expiry settlement fee must be $0.00, got ${test_fee_expiry}"
    print(f"  ✓ Kalshi Non-linear Taker Fee: Exactly verified (50¢ = $0.18 on 10 contracts; Expiry = $0.00).")

    # 4. Verify 3¢ Adverse Slippage Deductions in Kelly
    # If model prob is 70% and ask is 68¢, after 3¢ slippage fill is 71¢ + 1.4¢ fee = 72.4¢ -> Net EV is negative!
    test_metrics_marginal = engine.calculate_trade_metrics(
        model_prob=0.70,
        yes_ask=0.68,
        yes_bid=0.65,
        no_ask=0.35,
        no_bid=0.32,
        slippage_penalty_cents=3.0
    )
    # 70% model prob with 68¢ ask suffers 3¢ adverse fill -> 71¢ + fee -> EV is <= 0 -> Kelly must be 0!
    assert test_metrics_marginal["half_kelly_pct"] == 0.0, "Net Kelly should be 0 when 3¢ slippage erodes edge!"
    print(f"  ✓ Adverse Slippage Protection: 70% probability against 68¢ ask correctly rejected (0.0% Kelly).")

    # If model prob is 99% (late candle collapse), Kelly sizing must confirm strong edge
    test_metrics_crush = engine.calculate_trade_metrics(
        model_prob=0.99,
        yes_ask=0.75,
        yes_bid=0.72,
        no_ask=0.28,
        no_bid=0.25,
        slippage_penalty_cents=3.0
    )
    assert test_metrics_crush["half_kelly_pct"] > 15.0, "Net Kelly should be substantial when edge is authentic!"
    print(f"  ✓ High-Conviction Kelly: 99% probability against 75¢ ask correctly allocated ({test_metrics_crush['half_kelly_pct']:.1f}% Kelly).")

    print("\n" + "=" * 80)
    print("  ALL MATHEMATICAL ASSERTIONS & BRTI ACCUMULATOR CHECKS PASSED SUCCESSFULLY!")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    run_simulation_test()
