"""
backtests/fee_model.py — Exact Kalshi Non-Linear Taker Fee & Slippage Model.

Implements the official Kalshi exchange taker fee formula:
    fee = ceil(0.07 * contracts * price * (1 - price) * 100) / 100

Key Properties:
- Symmetric around 50¢: Maximum fee drag of 1.75¢ per contract at p=0.50.
- Decays to ~0.63¢ at extremes (10¢ / 90¢), rounded UP to 1¢.
- Settlement at expiration carries $0.00 fee.
- Early exit before expiry incurs taker fee on the exit price.
"""

import math
from typing import Dict, Any


def calculate_kalshi_taker_fee(contracts: int, price: float) -> float:
    """
    Calculate Kalshi official taker fee in dollars, rounded UP to nearest cent.
    
    Formula: ceil(0.07 * count * price * (1.0 - price) * 100) / 100
    Args:
        contracts: Number of contracts (positive integer).
        price: Contract price in dollars (e.g. 0.54 for 54¢).
    Returns:
        Fee in dollars.
    """
    if contracts <= 0 or price <= 0.0 or price >= 1.0:
        return 0.0
    raw_cents = 0.07 * float(contracts) * float(price) * (1.0 - float(price)) * 100.0
    fee_cents = math.ceil(round(raw_cents, 6))
    return fee_cents / 100.0


def calculate_net_trade_pnl(
    side: str,
    contracts: int,
    entry_price: float,
    exit_price: float,
    is_settlement: bool = False,
    entry_slippage_cents: float = 0.0,
    exit_slippage_cents: float = 0.0
) -> Dict[str, Any]:
    """
    Compute point-in-time net PnL accounting for:
    - Adverse entry slippage
    - Kalshi non-linear entry taker fee
    - Adverse exit slippage
    - Kalshi non-linear exit taker fee (or $0.00 at expiration settlement)
    """
    # 1. Entry execution with slippage
    eff_entry_price = min(0.99, max(0.01, entry_price + (entry_slippage_cents / 100.0)))
    entry_notional = contracts * eff_entry_price
    entry_fee = calculate_kalshi_taker_fee(contracts, eff_entry_price)
    total_entry_cost = entry_notional + entry_fee

    # 2. Exit execution with slippage
    if is_settlement:
        eff_exit_price = 1.0 if exit_price >= 0.999 else 0.0
        exit_fee = 0.0  # Settlement at expiration carries no Kalshi fee
    else:
        eff_exit_price = min(0.99, max(0.01, exit_price - (exit_slippage_cents / 100.0)))
        exit_fee = calculate_kalshi_taker_fee(contracts, eff_exit_price)

    gross_payout = contracts * eff_exit_price
    net_payout = gross_payout - exit_fee
    net_pnl = net_payout - total_entry_cost
    roi_pct = (net_pnl / total_entry_cost * 100.0) if total_entry_cost > 0 else 0.0

    return {
        "contracts": contracts,
        "entry_price": round(entry_price, 4),
        "eff_entry_price": round(eff_entry_price, 4),
        "entry_fee": round(entry_fee, 4),
        "total_entry_cost": round(total_entry_cost, 4),
        "exit_price": round(exit_price, 4),
        "eff_exit_price": round(eff_exit_price, 4),
        "exit_fee": round(exit_fee, 4),
        "gross_payout": round(gross_payout, 4),
        "net_payout": round(net_payout, 4),
        "net_pnl": round(net_pnl, 4),
        "roi_pct": round(roi_pct, 2),
        "is_settlement": is_settlement
    }
