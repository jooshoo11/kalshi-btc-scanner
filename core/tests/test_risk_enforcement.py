"""
core/tests/test_risk_enforcement.py — Verification of Global Risk Enforcement & R-Multiple Tracking.
"""

import pytest
from unittest.mock import MagicMock
from core.portfolio_manager import PortfolioManager


@pytest.mark.asyncio
async def test_r_multiple_tracking_and_expectancy():
    """Verify 1R definition, R-multiple calculation, win rate, and expectancy."""
    auth_mock = MagicMock()
    auth_mock.is_configured = False

    pm = PortfolioManager(
        auth=auth_mock,
        rest_url="https://demo-api.kalshi.co/trade-api/v2",
        initial_balance=100.0
    )

    # Trade 1: $10 risk (1R = $10), win +$25 (+2.5R)
    t1 = pm.record_trade_result("BTC", net_pnl=25.0, initial_risk_dollars=10.0, contracts=20, exit_reason="TAKE_PROFIT")
    assert t1["r_multiple"] == 2.5
    assert t1["is_win"] is True

    # Trade 2: $10 risk (1R = $10), loss -$10 (-1.0R)
    t2 = pm.record_trade_result("BTC", net_pnl=-10.0, initial_risk_dollars=10.0, contracts=20, exit_reason="STOP_LOSS")
    assert t2["r_multiple"] == -1.0
    assert t2["is_win"] is False

    # Trade 3: $10 risk (1R = $10), win +$15 (+1.5R)
    t3 = pm.record_trade_result("ETH", net_pnl=15.0, initial_risk_dollars=10.0, contracts=15, exit_reason="TAKE_PROFIT")
    assert t3["r_multiple"] == 1.5

    stats = pm.get_r_multiple_stats()
    assert stats["total_trades"] == 3
    # 2 wins out of 3 = 66.7%
    assert stats["win_rate_pct"] == 66.7
    # Total R = 2.5 - 1.0 + 1.5 = +3.0R
    assert stats["total_r"] == 3.0
    assert stats["avg_win_r"] == 2.0  # (2.5 + 1.5)/2
    assert stats["avg_loss_r"] == 1.0
    # Expectancy = (0.667 * 2.0) - (0.333 * 1.0) = 1.334 - 0.333 = ~1.0R
    assert stats["expectancy_r"] > 0.95
    # Profit Factor = (2.5 + 1.5) / 1.0 = 4.0
    assert stats["profit_factor"] == 4.0


@pytest.mark.asyncio
async def test_per_asset_drawdown_monitoring():
    """Verify real-time peak equity, current drawdown, and max drawdown per asset."""
    auth_mock = MagicMock()
    auth_mock.is_configured = False

    pm = PortfolioManager(
        auth=auth_mock,
        rest_url="https://demo-api.kalshi.co/trade-api/v2",
        initial_balance=100.0,
        max_single_trade_pct=20.0  # $20 base per asset
    )

    # Base equity for BTC starts at $20.00
    pm.update_asset_equity("BTC", 25.0)  # Peak rises to $25
    pm.update_asset_equity("BTC", 20.0)  # Equity drops to $20 -> 20% drawdown

    drawdowns = pm.get_asset_drawdowns()
    assert "BTC" in drawdowns
    assert drawdowns["BTC"]["peak_equity"] == 25.0
    assert drawdowns["BTC"]["current_equity"] == 20.0
    assert drawdowns["BTC"]["current_drawdown_pct"] == 20.0
    assert drawdowns["BTC"]["max_drawdown_pct"] == 20.0

    # New peak
    pm.update_asset_equity("BTC", 30.0)
    drawdowns2 = pm.get_asset_drawdowns()
    assert drawdowns2["BTC"]["current_drawdown_pct"] == 0.0
    assert drawdowns2["BTC"]["max_drawdown_pct"] == 20.0  # Historical max preserved


@pytest.mark.asyncio
async def test_margin_and_position_limits_enforcement():
    """Verify rejection when daily stop loss, position count, or margin is exhausted."""
    auth_mock = MagicMock()
    auth_mock.is_configured = False

    pm = PortfolioManager(
        auth=auth_mock,
        rest_url="https://demo-api.kalshi.co/trade-api/v2",
        initial_balance=100.0,
        max_portfolio_allocation_pct=40.0,
        max_single_trade_pct=20.0,
        max_concurrent_positions=2,
        global_stop_loss_daily_pct=10.0,
        day_start_equity=100.0
    )

    # 1. Simulate 11% daily drawdown ($100 down to $88.00)
    pm.total_equity = 88.00
    res_halt = await pm.evaluate_and_reserve_capital("BTC", entry_price=0.50, fee_per_contract=0.01)
    assert not res_halt["approved"]
    assert "daily stop loss tripped" in res_halt["reason"].lower()

    # Reset equity to healthy $100
    pm.total_equity = 100.0
    try:
        from core.kill_switch import kill_switch
        kill_switch.reset()
    except Exception:
        pass

    # 2. Concurrency limit: Fill 2 positions (BTC and ETH)
    res_btc = await pm.evaluate_and_reserve_capital("BTC", entry_price=0.50, fee_per_contract=0.01)
    assert res_btc["approved"]
    await pm.commit_fill("BTC", {"contracts": res_btc["target_contracts"], "cost_dollars": 15.0, "fee_dollars": 0.20})

    res_eth = await pm.evaluate_and_reserve_capital("ETH", entry_price=0.50, fee_per_contract=0.01)
    assert res_eth["approved"]
    await pm.commit_fill("ETH", {"contracts": res_eth["target_contracts"], "cost_dollars": 15.0, "fee_dollars": 0.20})

    # 3. Third asset (SOL) must be rejected due to max concurrent positions
    res_sol = await pm.evaluate_and_reserve_capital("SOL", entry_price=0.50, fee_per_contract=0.01)
    assert not res_sol["approved"]
    assert "Max concurrent positions reached" in res_sol["reason"]


@pytest.mark.asyncio
async def test_hedging_recommendations_and_dashboard():
    """Verify systematic hedging triggers when approaching the 10% daily stop loss."""
    auth_mock = MagicMock()
    auth_mock.is_configured = False

    pm = PortfolioManager(
        auth=auth_mock,
        rest_url="https://demo-api.kalshi.co/trade-api/v2",
        initial_balance=100.0,
        global_stop_loss_daily_pct=10.0,
        day_start_equity=100.0
    )

    # Normal state: 0% drawdown -> empty recommendations
    assert len(pm.get_hedging_recommendations()) == 0

    # Approaching threshold: Drawdown drops to 7.5% (75% of 10% limit)
    pm.total_equity = 92.50
    # Add an active BTC position
    pm.active_positions["BTC"] = {"side": "YES", "contracts": 25, "cost_dollars": 12.50}

    recs = pm.get_hedging_recommendations()
    assert len(recs) >= 2
    actions = [r["action"] for r in recs]
    assert "THROTTLE_POSITION_SIZING" in actions
    assert "TIGHTEN_EXIT_THRESHOLDS" in actions
    assert any("DELTA_HEDGE_BTC" in a for a in actions)

    # Dashboard generation
    dashboard = pm.get_risk_dashboard()
    assert dashboard["status"] == "WARNING_HIGH_RISK"
    assert "global_caps" in dashboard
    assert "per_asset_drawdowns" in dashboard
    assert "r_multiple_metrics" in dashboard
