"""
backtests/risk_metrics.py — Quantitative Risk & Portfolio Performance Metrics.

Calculates institutional risk metrics:
- Value at Risk (VaR): Parametric & Historical at 95% and 99%
- Conditional Value at Risk (CVaR / Expected Shortfall)
- Annualized Sharpe Ratio (24/7 crypto 15-minute compounding)
- Sortino Ratio (downside deviation semi-variance)
- Maximum Drawdown (MDD) and Drawdown Duration
- Calmar Ratio and Win/Loss Expectancy
"""

import math
from typing import Dict, Any, List, Union, Optional
import numpy as np
import pandas as pd
from scipy import stats


def calculate_risk_metrics(
    equity_curve: Union[List[float], np.ndarray, pd.Series],
    risk_free_rate: float = 0.045,
    periods_per_year: int = 35040  # 96 intervals/day * 365 days for 24/7 15m crypto
) -> Dict[str, Any]:
    """
    Calculate comprehensive institutional risk metrics from an equity curve or periodic returns.
    """
    if isinstance(equity_curve, list):
        equity = pd.Series(equity_curve)
    elif isinstance(equity_curve, np.ndarray):
        equity = pd.Series(equity_curve)
    else:
        equity = equity_curve.copy()

    if len(equity) < 2:
        return {
            "error": "Insufficient data points (minimum 2 required)"
        }

    # Clean non-finite values
    equity = equity.replace([np.inf, -np.inf], np.nan).dropna()
    initial_equity = float(equity.iloc[0])
    final_equity = float(equity.iloc[-1])

    # Percentage periodic returns
    returns = equity.pct_change().dropna()
    if len(returns) == 0 or returns.std() == 0:
        return {
            "total_return_pct": 0.0,
            "cagr_pct": 0.0,
            "sharpe_ratio": 0.0,
            "sortino_ratio": 0.0,
            "max_drawdown_pct": 0.0,
            "var_95_pct": 0.0,
            "cvar_95_pct": 0.0,
            "initial_equity": initial_equity,
            "final_equity": final_equity
        }

    n_periods = len(returns)
    years = n_periods / periods_per_year

    # 1. Returns & Compounding
    total_return_pct = ((final_equity - initial_equity) / initial_equity) * 100.0
    if years >= 0.1 and initial_equity > 0 and final_equity > 0:
        try:
            cagr = (final_equity / initial_equity) ** (1.0 / years) - 1.0
            cagr_pct = cagr * 100.0
        except OverflowError:
            cagr_pct = total_return_pct * (1.0 / max(years, 1e-4))
    else:
        # For short intraday / multi-day test slices, use linear annualized scaling to avoid power overflow
        cagr_pct = total_return_pct * (1.0 / max(years, 1e-4))

    mean_return = float(returns.mean())
    std_return = float(returns.std())
    rf_per_period = (1.0 + risk_free_rate) ** (1.0 / periods_per_year) - 1.0

    # 2. Sharpe Ratio (Annualized)
    excess_returns = returns - rf_per_period
    sharpe_ratio = (float(excess_returns.mean()) / std_return * math.sqrt(periods_per_year)) if std_return > 0 else 0.0

    # 3. Sortino Ratio (Downside semi-deviation below rf)
    downside_returns = excess_returns[excess_returns < 0]
    downside_std = float(np.sqrt(np.mean(downside_returns ** 2))) if len(downside_returns) > 0 else 0.0
    sortino_ratio = (float(excess_returns.mean()) / downside_std * math.sqrt(periods_per_year)) if downside_std > 0 else 0.0

    # 4. Maximum Drawdown & Calmar Ratio
    running_max = equity.cummax()
    drawdowns = (equity - running_max) / running_max
    max_drawdown = float(drawdowns.min())  # Negative number
    max_drawdown_pct = abs(max_drawdown) * 100.0

    calmar_ratio = (cagr_pct / max_drawdown_pct) if max_drawdown_pct > 0 else 0.0

    # 5. Value at Risk (VaR) & Conditional VaR (CVaR / Expected Shortfall)
    # Parametric VaR (Normal distribution)
    z_95 = stats.norm.ppf(0.95)
    z_99 = stats.norm.ppf(0.99)
    param_var_95 = float(-(mean_return - z_95 * std_return)) * 100.0
    param_var_99 = float(-(mean_return - z_99 * std_return)) * 100.0

    # Historical VaR & CVaR (Empirical percentiles)
    hist_var_95 = float(-np.percentile(returns, 5)) * 100.0
    hist_var_99 = float(-np.percentile(returns, 1)) * 100.0

    tail_losses_95 = returns[returns <= np.percentile(returns, 5)]
    cvar_95 = float(-tail_losses_95.mean()) * 100.0 if len(tail_losses_95) > 0 else hist_var_95

    tail_losses_99 = returns[returns <= np.percentile(returns, 1)]
    cvar_99 = float(-tail_losses_99.mean()) * 100.0 if len(tail_losses_99) > 0 else hist_var_99

    return {
        "initial_equity": round(initial_equity, 2),
        "final_equity": round(final_equity, 2),
        "total_return_pct": round(total_return_pct, 2),
        "cagr_pct": round(cagr_pct, 2),
        "annualized_volatility_pct": round(std_return * math.sqrt(periods_per_year) * 100.0, 2),
        "sharpe_ratio": round(sharpe_ratio, 2),
        "sortino_ratio": round(sortino_ratio, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 2),
        "calmar_ratio": round(calmar_ratio, 2),
        "var_95_pct": round(hist_var_95, 2),
        "var_99_pct": round(hist_var_99, 2),
        "cvar_95_pct": round(cvar_95, 2),
        "cvar_99_pct": round(cvar_99, 2),
        "param_var_95_pct": round(param_var_95, 2),
        "param_var_99_pct": round(param_var_99, 2),
        "total_intervals": n_periods
    }


def compare_coin_risk_metrics(
    coin_equity_curves: Dict[str, Union[List[float], pd.Series]]
) -> pd.DataFrame:
    """
    Generate a comparative risk & performance DataFrame across multiple coins.
    """
    records = []
    for coin, curve in coin_equity_curves.items():
        metrics = calculate_risk_metrics(curve)
        metrics["coin"] = coin.upper()
        records.append(metrics)

    df = pd.DataFrame(records)
    cols = ["coin", "total_return_pct", "sharpe_ratio", "sortino_ratio", "max_drawdown_pct", "var_95_pct", "cvar_95_pct", "calmar_ratio"]
    existing_cols = [c for c in cols if c in df.columns]
    return df[existing_cols].set_index("coin")
