"""
backtests/cf_benchmarks.py — CF Benchmarks 60-Second Settlement Simulation Engine.

Replicates the exact settlement mechanics of Kalshi 15-minute crypto series:
- BTC: CME CF Bitcoin Real Time Index (BRTI)
- ETH: CME CF Ether-Dollar Real Time Index (ETHUSD_RTI)
- SOL: CME CF Solana-Dollar Real Time Index (SOLUSD_RTI)
- XRP: CME CF Ripple-Dollar Real Time Index (XRPUSD_RTI)

Official CME CF Settlement Specification:
Settlement Value = (1 / N) * SUM_{t in [T-60s, T]} Spot(t)
Binary Contract Outcome:
- YES pays $1.00 if Settlement Value >= Strike (or > Strike per market rule), else $0.00
- NO pays $1.00 if YES pays $0.00, else $0.00
"""

import numpy as np
from typing import List, Dict, Any, Optional


class CFBenchmarksSettlementEngine:
    """
    Simulates the exact arithmetic mean calculation over the final 60-second window.
    """

    def __init__(self, window_seconds: int = 60):
        self.window_seconds = window_seconds

    def compute_settlement_price(self, ticks_last_60s: List[float]) -> float:
        """
        Calculate the 60-second arithmetic mean settlement price.
        """
        if not ticks_last_60s:
            raise ValueError("Cannot calculate CF Benchmarks settlement with empty tick list.")
        return float(np.mean(ticks_last_60s))

    def evaluate_contract_settlement(
        self,
        settlement_price: float,
        strike_price: float,
        target_side: str = "YES",
        tie_goes_to: str = "YES"
    ) -> Dict[str, Any]:
        """
        Evaluate binary option payoff at expiration.
        
        Args:
            settlement_price: 60-second arithmetic mean from CF Benchmarks.
            strike_price: Contract target strike.
            target_side: 'YES' or 'NO'.
            tie_goes_to: 'YES' per standard Kalshi "greater than or equal to" rule.
        """
        if settlement_price > strike_price:
            yes_won = True
        elif settlement_price < strike_price:
            yes_won = False
        else:
            yes_won = (tie_goes_to.upper() == "YES")

        won = (yes_won if target_side.upper() == "YES" else not yes_won)
        payout_cents = 100 if won else 0
        payout_dollars = 1.0 if won else 0.0

        return {
            "settlement_price": round(settlement_price, 2),
            "strike_price": round(strike_price, 2),
            "target_side": target_side.upper(),
            "yes_won": yes_won,
            "won": won,
            "payout_per_contract": payout_dollars
        }
