#!/usr/bin/env python3
"""
benchmark_hft_loop.py — Microsecond Baseline Benchmark for Multi-Coin Evaluation Loop.

Measures latency, jitter, throughput, and memory consumption across the complete
4-asset evaluation pipeline:
- Assets: BTC (KXBTC15M), ETH (KXETH15M), SOL (KXSOL15M), XRP (KXXRP15M)
- Hot Path:
    1. Tick ingestion & CF Benchmarks RTI accumulator update
    2. Drift-diffusion pricing (evaluate_15m_scalp / accumulator)
    3. Non-linear Kalshi taker fee accounting
    4. Level-2 Order Book VWAP depth sweep (L2OrderBook)
    5. Half-Kelly sizing & asyncio.Lock margin reservation (PortfolioManager)
- Profiles GC impact: Default GC vs GCTuner (critical trading window pauses).

Target:
    - 4-Coin Loop Execution Latency: < 10.0 ms (budget: 1000 ms)
    - p99 Jitter: < 25.0 ms
"""

import asyncio
import gc
import math
import os
import sys
import time
from typing import Dict, Any, List

import numpy as np

# Ensure repository root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.prediction_engine import QuantPredictionEngine, calculate_kalshi_fee
from core.portfolio_manager import PortfolioManager
from core.rti_accumulator import RtiAccumulator
from core.live_executor import L2OrderBook
from core.profiler import GCTuner, HFTProfiler


def create_deterministic_l2_book(ticker: str) -> L2OrderBook:
    """Pre-populates an L2 order book with realistic depth for testing VWAP sweeps."""
    book = L2OrderBook(ticker)
    # Bids: 50¢ down to 45¢ with varying depth
    bids = [[50, 20], [49, 35], [48, 50], [47, 80], [46, 100]]
    # Asks: 52¢ up to 57¢
    asks = [[52, 25], [53, 40], [54, 60], [55, 75], [56, 120]]
    book.apply_snapshot(bids, asks)
    return book


class HFTEvaluationHarness:
    """Simulates one complete 1-second evaluation tick across all 4 assets."""

    def __init__(self, pm: PortfolioManager, engine: QuantPredictionEngine):
        self.pm = pm
        self.engine = engine
        self.assets = ["BTC", "ETH", "SOL", "XRP"]
        self.configs = {
            "BTC": {"vol_15m": 95.0, "series": "KXBTC15M", "spot": 68450.0, "open": 68400.0},
            "ETH": {"vol_15m": 8.5, "series": "KXETH15M", "spot": 2650.0, "open": 2640.0},
            "SOL": {"vol_15m": 1.2, "series": "KXSOL15M", "spot": 185.0, "open": 184.0},
            "XRP": {"vol_15m": 0.015, "series": "KXXRP15M", "spot": 0.585, "open": 0.580},
        }
        self.accumulators = {
            coin: RtiAccumulator(index_name=f"{coin}USD_RTI", window_seconds=60)
            for coin in self.assets
        }
        self.books = {
            coin: create_deterministic_l2_book(f"KX{coin}15M-SAMPLE")
            for coin in self.assets
        }

    async def evaluate_single_coin(self, coin: str, seconds_left: float) -> Dict[str, Any]:
        """Evaluates pricing, fees, edge, VWAP book sweep, and margin reservation for one coin."""
        cfg = self.configs[coin]
        spot = cfg["spot"]
        open_p = cfg["open"]
        accum = self.accumulators[coin]
        book = self.books[coin]

        # 1. Update CF Benchmarks accumulator
        accum.add_sample(spot=spot, seconds_left=seconds_left, candle_id="candle_20261106_2200")
        accum_samples = accum.get_samples()

        # 2. Quantitative Drift-Diffusion evaluation
        pred = self.engine.evaluate_15m_scalp(
            spot=spot,
            open_price=open_p,
            seconds_left=seconds_left,
            dollar_vol_15m=cfg["vol_15m"],
            momentum_bias=0.0,
            accumulated_samples=accum_samples
        )

        prob_yes = pred["prob_yes"]
        prob_no = pred["prob_no"]

        # 3. Order Book L2 best prices and VWAP depth sweep
        best_ask, ask_qty = book.get_best_ask()
        ask_price = best_ask / 100.0

        fee = calculate_kalshi_fee(10, ask_price)
        edge = (prob_yes - (ask_price + (fee / 10.0))) * 100.0

        approved = False
        allocated_dollars = 0.0

        # 4. If edge passes trade selection filter (+4.0% min edge, 60% probability, 20¢-78¢ bounds)
        if edge >= 4.0 and prob_yes >= 0.60 and 0.20 <= ask_price <= 0.78:
            sweep = book.calculate_sweep_vwap("YES", 10)
            if sweep.get("can_fill"):
                vwap_price = sweep["vwap_cents"] / 100.0
                # Atomic margin lock evaluation & reservation
                res = await self.pm.evaluate_and_reserve_capital(
                    asset=coin,
                    entry_price=vwap_price,
                    fee_per_contract=fee / 10.0,
                    max_allocation_pct=20.0
                )
                approved = res.get("approved", False)
                allocated_dollars = res.get("allocated_dollars", 0.0)
                if approved:
                    await self.pm.release_reservation(coin, allocated_dollars)

        return {
            "coin": coin,
            "prob_yes": prob_yes,
            "edge": edge,
            "approved": approved,
            "allocated": allocated_dollars
        }

    async def evaluate_all_4_coins(self, seconds_left: float = 35.0) -> List[Dict[str, Any]]:
        """Evaluates all 4 assets concurrently or sequentially as in production."""
        results = []
        for coin in self.assets:
            res = await self.evaluate_single_coin(coin, seconds_left)
            results.append(res)
        return results


async def run_benchmark(iterations: int = 1000, use_gc_tuner: bool = True) -> Dict[str, Any]:
    """Runs high-frequency evaluation loop benchmark over N iterations."""
    pm = PortfolioManager(
        auth=None,
        rest_url="https://demo-api.kalshi.co",
        initial_balance=1000.0,
        max_portfolio_allocation_pct=40.0,
        max_single_trade_pct=20.0,
        max_concurrent_positions=4
    )
    engine = QuantPredictionEngine()
    harness = HFTEvaluationHarness(pm, engine)
    gc_tuner = GCTuner()

    if use_gc_tuner:
        gc_tuner.configure_for_hft(gen0=50000, gen1=20, gen2=20)
    else:
        gc_tuner.restore_defaults()

    latencies_us: List[float] = []

    # Warmup
    for _ in range(50):
        await harness.evaluate_all_4_coins(seconds_left=45.0)

    # Force initial GC clean before timed test
    gc.collect()

    t_start = time.perf_counter()

    for i in range(iterations):
        sec_left = 60.0 - (i % 60)

        t0 = time.perf_counter()
        if use_gc_tuner:
            with gc_tuner.critical_trading_window():
                await harness.evaluate_all_4_coins(seconds_left=sec_left)
            # Simulate idle period collection every 100 iterations
            if i % 100 == 0:
                gc_tuner.collect_if_idle(force=True)
        else:
            await harness.evaluate_all_4_coins(seconds_left=sec_left)

        dt_us = (time.perf_counter() - t0) * 1e6
        latencies_us.append(dt_us)

    total_time_sec = time.perf_counter() - t_start
    arr = np.array(latencies_us)

    stats = {
        "iterations": iterations,
        "mode": "GCTuner Enabled" if use_gc_tuner else "Default GC",
        "total_time_sec": round(total_time_sec, 4),
        "throughput_loops_per_sec": round(iterations / total_time_sec, 1),
        "mean_us": round(float(np.mean(arr)), 2),
        "std_us": round(float(np.std(arr)), 2),
        "min_us": round(float(np.min(arr)), 2),
        "p50_us": round(float(np.percentile(arr, 50)), 2),
        "p90_us": round(float(np.percentile(arr, 90)), 2),
        "p95_us": round(float(np.percentile(arr, 95)), 2),
        "p99_us": round(float(np.percentile(arr, 99)), 2),
        "max_us": round(float(np.max(arr)), 2),
        "mean_ms": round(float(np.mean(arr)) / 1000.0, 3),
        "p99_ms": round(float(np.percentile(arr, 99)) / 1000.0, 3),
        "max_ms": round(float(np.max(arr)) / 1000.0, 3),
        "gc_stats": gc_tuner.get_stats()
    }
    return stats


def print_benchmark_report(stats_default: Dict[str, Any], stats_tuned: Dict[str, Any]):
    print("=" * 80)
    print(" HFT MULTI-COIN EVALUATION LOOP PERFORMANCE BENCHMARK (4 ASSETS)")
    print("=" * 80)
    print(f"Iterations: {stats_default['iterations']:,} cycles across [BTC, ETH, SOL, XRP]")
    print("-" * 80)
    print(f"{'Metric':<28} | {'Default GC':<22} | {'GCTuner (HFT Tuned)':<22}")
    print("-" * 80)
    print(f"{'Mean Latency (4 coins)':<28} | {stats_default['mean_us']:>10.2f} µs ({stats_default['mean_ms']:.3f} ms) | {stats_tuned['mean_us']:>10.2f} µs ({stats_tuned['mean_ms']:.3f} ms)")
    print(f"{'Median Latency (p50)':<28} | {stats_default['p50_us']:>10.2f} µs ({stats_default['p50_us']/1000:.3f} ms) | {stats_tuned['p50_us']:>10.2f} µs ({stats_tuned['p50_us']/1000:.3f} ms)")
    print(f"{'p95 Latency':<28} | {stats_default['p95_us']:>10.2f} µs ({stats_default['p95_us']/1000:.3f} ms) | {stats_tuned['p95_us']:>10.2f} µs ({stats_tuned['p95_us']/1000:.3f} ms)")
    print(f"{'p99 Latency (Tail Jitter)':<28} | {stats_default['p99_us']:>10.2f} µs ({stats_default['p99_ms']:.3f} ms) | {stats_tuned['p99_us']:>10.2f} µs ({stats_tuned['p99_ms']:.3f} ms)")
    print(f"{'Max Latency':<28} | {stats_default['max_us']:>10.2f} µs ({stats_default['max_ms']:.3f} ms) | {stats_tuned['max_us']:>10.2f} µs ({stats_tuned['max_ms']:.3f} ms)")
    print(f"{'Throughput':<28} | {stats_default['throughput_loops_per_sec']:>12.1f} loops/s   | {stats_tuned['throughput_loops_per_sec']:>12.1f} loops/s")
    print("-" * 80)
    sub_second_budget_pct = (stats_tuned['p99_ms'] / 1000.0) * 100.0
    print(f"Sub-Second Loop Budget Utilization: {sub_second_budget_pct:.3f}% of 1000 ms budget!")
    print(f"Jitter Reduction (Max): {((stats_default['max_us'] - stats_tuned['max_us']) / max(1, stats_default['max_us'])) * 100.0:+.1f}%")
    print("=" * 80)


if __name__ == "__main__":
    print("[Benchmark] Running 1,000 cycles with standard GC...")
    res_def = asyncio.run(run_benchmark(iterations=1000, use_gc_tuner=False))

    print("[Benchmark] Running 1,000 cycles with GCTuner...")
    res_tuned = asyncio.run(run_benchmark(iterations=1000, use_gc_tuner=True))

    print_benchmark_report(res_def, res_tuned)
