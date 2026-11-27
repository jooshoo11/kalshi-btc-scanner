#!/usr/bin/env python3
"""
scripts/tick_walk_forward.py — Rolling walk-forward on ACTUAL recorded tick data.

Loads the tick_backtest_engine candle builder, then runs rolling train/test windows:
  - Train window: calibrate which strategy config (S2 vs S5) performs best in-sample
  - Test window: run that config out-of-sample on the next candle block

Outputs per-window OOS metrics + aggregate to data/backtest_validation/
"""

import csv
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tick_backtest_engine import (
    load_kalshi_ticks, load_spot_ticks, build_candles,
    simulate_candle, calculate_kalshi_taker_fee,
    get_strategy_suite, StrategyConfig,
)


def calculate_risk_metrics(equity_curve):
    if len(equity_curve) < 2:
        return {"total_return_pct": 0.0, "max_drawdown_pct": 0.0, "sharpe_ratio": 0.0,
                "initial_equity": equity_curve[0] if equity_curve else 0.0,
                "final_equity": equity_curve[-1] if equity_curve else 0.0}
    eq = [float(x) for x in equity_curve]
    initial = eq[0]
    final = eq[-1]
    total_return_pct = ((final - initial) / initial) * 100.0 if initial > 0 else 0.0
    returns = [(eq[i] - eq[i-1]) / max(0.01, eq[i-1]) for i in range(1, len(eq))]
    if not returns:
        return {"total_return_pct": total_return_pct, "max_drawdown_pct": 0.0, "sharpe_ratio": 0.0,
                "initial_equity": initial, "final_equity": final}
    mean_r = sum(returns) / len(returns)
    std_r = math.sqrt(sum((r - mean_r) ** 2 for r in returns) / max(1, len(returns) - 1)) if len(returns) > 1 else 0.0
    sharpe = (mean_r / std_r * math.sqrt(35040)) if std_r > 0 else 0.0
    running_max = eq[0]
    max_dd = 0.0
    for v in eq:
        running_max = max(running_max, v)
        dd = (running_max - v) / running_max if running_max > 0 else 0.0
        max_dd = max(max_dd, dd)
    return {
        "initial_equity": round(initial, 2),
        "final_equity": round(final, 2),
        "total_return_pct": round(total_return_pct, 2),
        "max_drawdown_pct": round(max_dd * 100.0, 2),
        "sharpe_ratio": round(sharpe, 2),
    }


def run_strategy_on_candles(candles, cfg, initial_equity, spot_map):
    equity = initial_equity
    equity_curve = [equity]
    trades = []
    for candle in candles:
        equity, tlist = simulate_candle(
            candle, cfg, equity,
            spot_lead_map=spot_map.get(candle.coin, {}),
            coin_weight=0.50, max_trade_alloc_pct=20.0, base_contracts=40,
        )
        equity_curve.append(equity)
        trades.extend(tlist)
    return equity, equity_curve, trades


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Walk-forward on actual tick data")
    parser.add_argument("--data-root", default=str(REPO_ROOT / "data" / "ticks"))
    parser.add_argument("--coins", default="BTC,ETH")
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "data" / "backtest_validation"))
    parser.add_argument("--initial-equity", type=float, default=100.0)
    parser.add_argument("--train-candles", type=int, default=60, help="candles per train window")
    parser.add_argument("--test-candles", type=int, default=20, help="candles per test window")
    args = parser.parse_args()

    coins = tuple(c.strip().upper() for c in args.coins.split(","))
    os.makedirs(args.out_dir, exist_ok=True)

    print(f"[1/4] Loading ticks from {args.data_root} ...")
    kalshi_sec_map = load_kalshi_ticks(args.data_root, coins)
    spot_map = load_spot_ticks(args.data_root, coins)

    print("[2/4] Building candles ...")
    all_candles = []
    for c in coins:
        candles = build_candles(kalshi_sec_map[c], spot_map[c], c)
        print(f"    {c}: {len(candles)} candles")
        all_candles.extend(candles)
    all_candles.sort(key=lambda c: (c.open_ts, c.coin))
    print(f"    Total: {len(all_candles)} candles")

    strategies = get_strategy_suite()
    n = len(all_candles)
    train_w = args.train_candles
    test_w = args.test_candles

    # Split candles by coin since strategies trade per-candle
    candles_by_coin = {}
    for c in coins:
        candles_by_coin[c] = [cand for cand in all_candles if cand.coin == c]

    # Per-coin walk-forward
    oos_results = []
    for coin in coins:
        ccl = candles_by_coin[coin]
        m = len(ccl)
        total_windows = (m - train_w) // test_w
        if total_windows == 0:
            print(f"    {coin}: not enough candles for walk-forward (need > {train_w})")
            continue

        coin_equity = args.initial_equity / len(coins)  # split starting capital
        coin_equity_curve = [coin_equity]
        coin_all_trades = []

        for w in range(total_windows):
            train_start = w * test_w
            train_end = train_start + train_w
            test_start = train_end
            test_end = min(test_start + test_w, m)

            train_markets = ccl[train_start:train_end]
            test_markets = ccl[test_start:test_end]

            # In-sample: pick best strategy by final equity
            best_cfg = strategies[0]
            best_equity = -1.0
            for cfg in strategies:
                eq, _, _ = run_strategy_on_candles(train_markets, cfg, coin_equity, spot_map)
                if eq > best_equity:
                    best_equity = eq
                    best_cfg = cfg

            # Out-of-sample: run best_cfg
            test_eq, test_eq_curve, test_trades = run_strategy_on_candles(
                test_markets, best_cfg, coin_equity, spot_map)

            coin_equity = test_eq
            for eq in test_eq_curve[1:]:
                coin_equity_curve.append(eq)
            coin_all_trades.extend(test_trades)

            oos_results.append({
                "coin": coin,
                "window": w + 1,
                "train_span": f"[{train_start}:{train_end}]",
                "test_span": f"[{test_start}:{test_end}]",
                "chosen_strategy": best_cfg.name,
                "oos_return_pct": round(((test_eq - coin_equity) / coin_equity) * 100.0, 2) if coin_equity > 0 else 0.0,
                "oos_trades": len(test_trades),
            })

        # Aggregate coin metrics
        m = calculate_risk_metrics(coin_equity_curve)
        wins = sum(1 for t in coin_all_trades if t.get("pnl", 0) > 0)
        losses = sum(1 for t in coin_all_trades if t.get("pnl", 0) < 0)
        report = {
            "coin": coin,
            "initial_equity": args.initial_equity / len(coins),
            "final_equity": round(coin_equity, 2),
            "net_pnl": round(coin_equity - (args.initial_equity / len(coins)), 2),
            "total_trades": len(coin_all_trades),
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round((wins / len(coin_all_trades)) * 100.0, 2) if coin_all_trades else 0.0,
            "metrics": m,
            "equity_curve": coin_equity_curve,
            "windows": [r for r in oos_results if r["coin"] == coin],
        }
        path = os.path.join(args.out_dir, f"walkforward_{coin}.json")
        with open(path, "w") as f:
            json.dump(report, f, indent=2)
        print(f"\n=== {coin} Walk-Forward (OOS) ===")
        print(f"  Final equity:  ${report['final_equity']:.2f}")
        print(f"  Net PnL:       ${report['net_pnl']:+.2f}")
        print(f"  Trades:        {report['total_trades']} | Wins: {wins} | Losses: {losses} | Win rate: {report['win_rate_pct']:.1f}%")
        print(f"  Max DD:        {m['max_drawdown_pct']:.1f}% | Sharpe: {m['sharpe_ratio']:.2f} | Return: {m['total_return_pct']:+.2f}%")
        print(f"  Windows:       {len(report['windows'])} OOS test windows")
        print(f"  Saved: {path}")

    # Combined report
    combined = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "coins": list(coins),
        "train_candles": train_w,
        "test_candles": test_w,
        "total_candles": n,
        "results": {r["coin"]: r for r in [json.load(open(os.path.join(args.out_dir, f"walkforward_{c}.json"))) for c in coins]},
    }
    path = os.path.join(args.out_dir, "walkforward_combined.json")
    with open(path, "w") as f:
        json.dump(combined, f, indent=2)
    print(f"\nCombined walk-forward report: {path}")


if __name__ == "__main__":
    main()
