"""
core/tests/test_backtest_suite.py — Unit tests for Backtest Fee, Settlement, and Risk Metrics.
"""

import pytest
import numpy as np
import pandas as pd
from backtests.fee_model import calculate_kalshi_taker_fee, calculate_net_trade_pnl
from backtests.cf_benchmarks import CFBenchmarksSettlementEngine
from backtests.risk_metrics import calculate_risk_metrics, compare_coin_risk_metrics


def test_kalshi_exact_taker_fee():
    """Verify non-linear Kalshi taker fee formula and rounding up to cents."""
    # 1 contract @ 50¢: 0.07 * 1 * 0.5 * 0.5 = 0.0175 -> ceil to 2¢ ($0.02)
    fee_50 = calculate_kalshi_taker_fee(1, 0.50)
    assert fee_50 == 0.02

    # 100 contracts @ 50¢: 0.07 * 100 * 0.5 * 0.5 = 1.75 -> $1.75
    fee_100_50 = calculate_kalshi_taker_fee(100, 0.50)
    assert fee_100_50 == 1.75

    # 10 contracts @ 20¢: 0.07 * 10 * 0.20 * 0.80 = 0.112 -> ceil to $0.12
    fee_10_20 = calculate_kalshi_taker_fee(10, 0.20)
    assert fee_10_20 == 0.12

    # Expiration or edge prices (0.0 or 1.0) carry $0.00 fee
    assert calculate_kalshi_taker_fee(100, 0.0) == 0.0
    assert calculate_kalshi_taker_fee(100, 1.0) == 0.0


def test_cf_benchmarks_arithmetic_mean_settlement():
    """Verify 60-second arithmetic mean settlement calculation."""
    engine = CFBenchmarksSettlementEngine(window_seconds=60)
    
    # 60 seconds with linearly increasing prices from 95,000 to 95,059
    ticks = [95000.0 + i for i in range(60)]
    mean_val = engine.compute_settlement_price(ticks)
    assert mean_val == 95029.5  # Exact midpoint

    # Strike at 95,025 -> YES wins
    res_yes = engine.evaluate_contract_settlement(mean_val, strike_price=95025.0, target_side="YES")
    assert res_yes["yes_won"] is True
    assert res_yes["won"] is True
    assert res_yes["payout_per_contract"] == 1.0

    # Strike at 95,030 -> YES loses, NO wins
    res_no = engine.evaluate_contract_settlement(mean_val, strike_price=95030.0, target_side="NO")
    assert res_no["yes_won"] is False
    assert res_no["won"] is True
    assert res_no["payout_per_contract"] == 1.0


def test_risk_metrics_var_cvar_sharpe_sortino():
    """Verify calculation of VaR, CVaR, Sharpe, and Sortino."""
    # Synthetic equity curve starting at 100 with known returns
    equity = [100.0, 102.0, 101.0, 105.0, 103.0, 108.0, 106.0, 110.0]
    metrics = calculate_risk_metrics(equity, risk_free_rate=0.0)

    assert metrics["total_return_pct"] == 10.0
    assert metrics["max_drawdown_pct"] > 0.0
    assert "sharpe_ratio" in metrics
    assert "sortino_ratio" in metrics
    assert "var_95_pct" in metrics
    assert "cvar_95_pct" in metrics
    # CVaR at 95% must be >= VaR at 95%
    assert metrics["cvar_95_pct"] >= metrics["var_95_pct"]


def test_multi_coin_comparison():
    """Verify multi-coin comparison across BTC, ETH, SOL, XRP."""
    coin_curves = {
        "BTC": [100.0, 103.0, 102.0, 108.0, 112.0],
        "ETH": [100.0, 101.0, 99.0, 104.0, 107.0],
        "SOL": [100.0, 105.0, 101.0, 110.0, 118.0],
        "XRP": [100.0, 98.0, 96.0, 99.0, 101.0]
    }
    df = compare_coin_risk_metrics(coin_curves)
    assert len(df) == 4
    assert "BTC" in df.index
    assert "ETH" in df.index
    assert "SOL" in df.index
    assert "XRP" in df.index
    assert "sharpe_ratio" in df.columns
