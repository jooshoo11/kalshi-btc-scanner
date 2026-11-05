"""
core/tests/test_portfolio_concurrency.py — Integration & Concurrency Test Harness.

Tests the PortfolioManager's asyncio.Lock() serialization under simultaneous
signals from BTC, ETH, and SOL, confirming that:
1. Capital reservation is atomic and serialized.
2. Concurrent orders never exceed max portfolio allocation.
3. Insufficient balance errors are cleanly avoided.
"""

import asyncio
from unittest.mock import MagicMock
from core.portfolio_manager import PortfolioManager

try:
    import pytest
    pytest_asyncio_marker = pytest.mark.asyncio
except ImportError:
    def pytest_asyncio_marker(func):
        return func


@pytest_asyncio_marker
async def test_simultaneous_trade_reservation_lock():
    """Verify that simultaneous signals from BTC and SOL are cleanly serialized."""
    auth_mock = MagicMock()
    auth_mock.is_configured = False
    
    # Portfolio of $100 with 30% max exposure cap and 15% single trade cap
    pm = PortfolioManager(
        auth=auth_mock,
        rest_url="https://demo-api.kalshi.co/trade-api/v2",
        initial_balance=100.0,
        max_portfolio_allocation_pct=30.0,
        max_single_trade_pct=15.0,
        max_concurrent_positions=2
    )

    # Simulate 3 simultaneous triggers: BTC, SOL, and ETH
    async def place_simulated_trade(asset: str, price: float):
        # 15% of $100 = $15.00 budget. Entry price 50¢ = 30 contracts ($15.00)
        res = await pm.evaluate_and_reserve_capital(
            asset=asset,
            entry_price=price,
            fee_per_contract=0.005
        )
        return res

    # Trigger BTC and SOL at the exact same instant
    results = await asyncio.gather(
        place_simulated_trade("BTC", 0.50),
        place_simulated_trade("SOL", 0.50),
        place_simulated_trade("ETH", 0.50)
    )

    approved = [r for r in results if r["approved"]]
    rejected = [r for r in results if not r["approved"]]

    # Exactly 2 should be approved (reaching max_concurrent_positions = 2)
    assert len(approved) == 2, f"Expected 2 approved, got {len(approved)}"
    assert len(rejected) == 1, f"Expected 1 rejected, got {len(rejected)}"
    assert "Max concurrent positions reached" in rejected[0]["reason"] or "capacity" in rejected[0]["reason"]

    # Total allocated capital should not exceed 30% ($30.00)
    assert pm.allocated_capital <= 30.0, f"Allocated capital {pm.allocated_capital} exceeded $30.00!"
    print(f"  ✓ Concurrency Lock Verified: 2 concurrent trades approved (${pm.allocated_capital:.2f}), 3rd rejected cleanly.")


@pytest_asyncio_marker
async def test_release_and_reallocation():
    """Verify that releasing capital correctly restores buying power."""
    auth_mock = MagicMock()
    auth_mock.is_configured = False

    pm = PortfolioManager(
        auth=auth_mock,
        rest_url="https://demo-api.kalshi.co/trade-api/v2",
        initial_balance=100.0,
        max_portfolio_allocation_pct=30.0,
        max_single_trade_pct=15.0,
        max_concurrent_positions=2
    )

    # 1. Reserve for BTC
    btc_res = await pm.evaluate_and_reserve_capital("BTC", 0.50, 0.005)
    assert btc_res["approved"]
    await pm.commit_fill("BTC", {"contracts": btc_res["target_contracts"], "cost_dollars": 14.50, "fee_dollars": 0.15})

    # 2. Reserve for SOL
    sol_res = await pm.evaluate_and_reserve_capital("SOL", 0.50, 0.005)
    assert sol_res["approved"]
    await pm.commit_fill("SOL", {"contracts": sol_res["target_contracts"], "cost_dollars": 14.50, "fee_dollars": 0.15})

    # 3. Third trade (ETH) must be rejected
    eth_res = await pm.evaluate_and_reserve_capital("ETH", 0.50, 0.005)
    assert not eth_res["approved"]

    # 4. BTC takes profit and releases with $20 payout ($5.35 profit)
    await pm.release_position("BTC", payout_dollars=20.00)
    assert "BTC" not in pm.active_positions
    assert pm.total_cash > 100.0

    # 5. ETH can now be approved
    eth_res2 = await pm.evaluate_and_reserve_capital("ETH", 0.50, 0.005)
    assert eth_res2["approved"], f"ETH should be approved after BTC release, reason: {eth_res2.get('reason')}"
    print(f"  ✓ Capital Recycling Verified: Released BTC, approved ETH successfully.")


if __name__ == "__main__":
    asyncio.run(test_simultaneous_trade_reservation_lock())
    asyncio.run(test_release_and_reallocation())
    print("\n✓ ALL PORTFOLIO CONCURRENCY TESTS PASSED!")
