"""
coins/btc/backtests/btc_walk_forward.py — BTC (KXBTC15M / BRTI) Walk-Forward Backtester.

Asset-specific walk-forward validation harness for Bitcoin 15-minute binary options
settling against CME CF Bitcoin Real Time Index (BRTI) 60-second arithmetic mean.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backtests.walk_forward import MarketGenerator, WalkForwardEngine, StrategyParams
from backtests.risk_metrics import calculate_risk_metrics


def run_btc_walk_forward(
    n_candles: int = 500,
    initial_spot: float = 95000.0,
    volatility: float = 0.55
):
    print("=" * 75)
    print("  WALK-FORWARD ANALYSIS: BITCOIN (KXBTC15M / BRTI 60s SETTLEMENT)")
    print("=" * 75)

    print(f"\n[1/3] Generating {n_candles} Point-in-Time 15-Minute BTC Candles...")
    markets = MarketGenerator.generate_synthetic_series(
        n_candles=n_candles,
        initial_spot=initial_spot,
        volatility_annual=volatility,
        drift_annual=0.08,
        random_seed=101
    )
    print(f"  Generated {len(markets)} markets ({len(markets) * 900:,} 1-second BRTI ticks).")

    print("\n[2/3] Running Walk-Forward Optimization across Rolling Windows (Train 200, Test 50)...")
    engine = WalkForwardEngine(
        initial_bankroll=1000.0,
        slippage_entry_cents=1.0,
        slippage_exit_cents=1.0
    )

    candidates = [
        StrategyParams(min_edge_pct=3.5, min_model_prob=58.0, take_profit_roi_pct=20.0, stop_loss_roi_pct=25.0),
        StrategyParams(min_edge_pct=4.5, min_model_prob=60.0, take_profit_roi_pct=25.0, stop_loss_roi_pct=25.0),
        StrategyParams(min_edge_pct=5.5, min_model_prob=62.0, take_profit_roi_pct=30.0, stop_loss_roi_pct=25.0),
        StrategyParams(min_edge_pct=6.5, min_model_prob=65.0, take_profit_roi_pct=35.0, stop_loss_roi_pct=30.0),
    ]

    results = engine.run_walk_forward_analysis(
        all_markets=markets,
        train_window_size=200,
        test_window_size=50,
        parameter_candidates=candidates
    )

    print("\n[3/3] Out-of-Sample Results by Rolling Segment:")
    print("-" * 75)
    for w in results["window_reports"]:
        print(f"  Window {w['window']:02d} | Train {w['train_span']} IS Sharpe: {w['is_sharpe']:+6.2f} | "
              f"Test {w['test_span']} OOS Ret: {w['oos_return_pct']:+6.1f}% | DD: {w['oos_drawdown_pct']:4.1f}% | "
              f"Trades: {w['oos_trades_count']:2d} (Edge {w['best_edge_pct']}%, TP {w['best_tp_roi']}%)")

    m = results["overall_oos_metrics"]
    print("-" * 75)
    print("  BTC Aggregate Out-of-Sample Performance & Risk Profile:")
    print(f"    Initial Equity:       ${m['initial_equity']:.2f}")
    print(f"    Final Equity:         ${m['final_equity']:.2f} ({m['total_return_pct']:+.2f}%)")
    print(f"    Sharpe Ratio:         {m['sharpe_ratio']:.2f}")
    print(f"    Sortino Ratio:        {m['sortino_ratio']:.2f}")
    print(f"    Max Drawdown:         {m['max_drawdown_pct']:.2f}%")
    print(f"    Calmar Ratio:         {m['calmar_ratio']:.2f}")
    print(f"    Historical VaR (95%): {m['var_95_pct']:.2f}%")
    print(f"    Historical CVaR (95%):{m['cvar_95_pct']:.2f}%")
    print(f"    Historical VaR (99%): {m['var_99_pct']:.2f}%")
    print(f"    Historical CVaR (99%):{m['cvar_99_pct']:.2f}%")
    print("=" * 75)
    return results


if __name__ == "__main__":
    run_btc_walk_forward()
