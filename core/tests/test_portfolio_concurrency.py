"""
core/tests/test_portfolio_concurrency.py — Integration & Concurrency Test Harness.

Tests the PortfolioManager's asyncio.Lock() serialization under simultaneous
signals from BTC, ETH, SOL, and XRP, confirming that:
1. Capital reservation is atomic and serialized under simultaneous signals.
2. Concurrent orders never exceed max portfolio allocation (40%) or max positions (2).
3. Simultaneous BTC and SOL triggers cannot double-spend margin.
4. Margin exhaustion cleanly rejects excess trades with structured error reasons.
5. Capital recycling properly frees buying power for subsequent coin evaluations.
"""

import asyncio
import pytest
from unittest.mock import MagicMock
from core.portfolio_manager import PortfolioManager


@pytest.mark.asyncio
async def test_simultaneous_trade_reservation_lock(portfolio_manager_fixture):
    """Verify that simultaneous signals from BTC, SOL, and ETH are cleanly serialized."""
    pm = portfolio_manager_fixture

    # Simulate 3 simultaneous triggers: BTC, SOL, and ETH
    async def place_simulated_trade(asset: str, price: float):
        return await pm.evaluate_and_reserve_capital(
            asset=asset,
            entry_price=price,
            fee_per_contract=0.005
        )

    # Trigger BTC, SOL, and ETH at the exact same instant
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
    assert (
        "Max concurrent positions reached" in rejected[0]["reason"]
        or "capacity" in rejected[0]["reason"]
    )

    # Total allocated capital must not exceed 40% ($40.00)
    assert pm.allocated_capital <= 40.0, f"Allocated capital {pm.allocated_capital} exceeded $40.00!"


@pytest.mark.asyncio
async def test_simultaneous_btc_and_sol_margin_exhaustion(portfolio_manager_fixture):
    """
    Simulate BTC and SOL contending for a limited margin pool.
    Verify that if BTC takes 20% and SOL takes 20%, a subsequent XRP or ETH
    trade is rejected with margin/portfolio capacity exhaustion.
    """
    pm = portfolio_manager_fixture

    # Trigger BTC and SOL simultaneously with larger size requests
    btc_task = asyncio.create_task(pm.evaluate_and_reserve_capital("BTC", 0.50, 0.01))
    sol_task = asyncio.create_task(pm.evaluate_and_reserve_capital("SOL", 0.50, 0.01))

    btc_res, sol_res = await asyncio.gather(btc_task, sol_task)

    assert btc_res["approved"] is True
    assert sol_res["approved"] is True

    # Commit both fills
    await pm.commit_fill("BTC", {"contracts": btc_res["target_contracts"], "cost_dollars": btc_res["total_reserve"], "fee_dollars": 0.20})
    await pm.commit_fill("SOL", {"contracts": sol_res["target_contracts"], "cost_dollars": sol_res["total_reserve"], "fee_dollars": 0.20})

    # Portfolio is at 40% capacity ($40.00)
    assert pm.allocated_capital >= 38.0

    # Simultaneous third-wave attempts (ETH and XRP) must both be rejected
    eth_task = asyncio.create_task(pm.evaluate_and_reserve_capital("ETH", 0.50, 0.01))
    xrp_task = asyncio.create_task(pm.evaluate_and_reserve_capital("XRP", 0.50, 0.01))

    eth_res, xrp_res = await asyncio.gather(eth_task, xrp_task)

    assert eth_res["approved"] is False
    assert xrp_res["approved"] is False
    assert "capacity" in eth_res["reason"].lower() or "positions" in eth_res["reason"].lower()
    assert "capacity" in xrp_res["reason"].lower() or "positions" in xrp_res["reason"].lower()


@pytest.mark.asyncio
async def test_release_and_reallocation(portfolio_manager_fixture):
    """Verify that releasing capital correctly restores buying power."""
    pm = portfolio_manager_fixture

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
    await pm.release_position("BTC", payout_dollars=20.00, exit_reason="TAKE_PROFIT")
    assert "BTC" not in pm.active_positions
    assert pm.total_cash > 100.0

    # 5. ETH can now be approved
    eth_res2 = await pm.evaluate_and_reserve_capital("ETH", 0.50, 0.005)
    assert eth_res2["approved"], f"ETH should be approved after BTC release, reason: {eth_res2.get('reason')}"
