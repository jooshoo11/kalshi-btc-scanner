"""
core/tests/test_live_bridge.py — Unit tests for LiveMultiCoinBridge.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.live_bridge import LiveMultiCoinBridge
from core.portfolio_manager import PortfolioManager


def _make_pm(balance: float = 200.0) -> PortfolioManager:
    return PortfolioManager(
        auth=None,
        rest_url="https://api.elections.kalshi.com/trade-api/v2",
        initial_balance=balance,
        max_portfolio_allocation_pct=40.0,
        max_single_trade_pct=20.0,
        max_concurrent_positions=2,
        global_stop_loss_daily_pct=10.0,
        day_start_equity=balance,
    )


def _make_oms(dry_run: bool = False, blocked_by_orphans: bool = False):
    return SimpleNamespace(
        dry_run=dry_run,
        blocked_by_orphans=blocked_by_orphans,
        execute_entry_with_vwap_protection=AsyncMock(
            return_value={
                "status": "FILLED",
                "order_id": "ord_live_1",
                "filled_count": 30,
                "fill_price_cents": 30,
                "ticker": "KXETH15M-TEST",
            }
        ),
        execute_complete_exit=AsyncMock(
            return_value={
                "status": "EXITED",
                "filled_count": 30,
                "avg_exit_price_cents": 45,
            }
        ),
    )


@pytest.mark.asyncio
async def test_live_bridge_noop_when_dry_run():
    oms = _make_oms(dry_run=True)
    pm = _make_pm(200.0)
    bridge = LiveMultiCoinBridge(oms=oms, portfolio_manager=pm)

    assert bridge.is_live_armed is False
    res_in = await bridge.on_entry("ETH", "KXETH15M-TEST", "YES", 0.30, 72.0)
    assert res_in["status"] == "PAPER_ONLY"
    oms.execute_entry_with_vwap_protection.assert_not_awaited()
    assert pm.allocated_capital == 0.0

    res_out = await bridge.on_exit("ETH", "TAKE_PROFIT", 0.45)
    assert res_out["status"] == "PAPER_ONLY"
    oms.execute_complete_exit.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_bridge_entry_and_exit_lifecycle():
    oms = _make_oms(dry_run=False)
    pm = _make_pm(200.0)
    bridge = LiveMultiCoinBridge(
        oms=oms,
        portfolio_manager=pm,
        live_max_allocation_pct=15.0,
        max_live_trade_dollars=25.0,
        max_live_contracts=50,
    )

    assert bridge.is_live_armed is True
    res_in = await bridge.on_entry("ETH", "KXETH15M-TEST", "YES", 0.30, 72.0)
    assert res_in["status"] == "FILLED"
    assert "ETH" in bridge.live_positions
    assert "ETH" in pm.active_positions

    # Verify VWAP cap respected (30c + 2c = 32c <= 35c)
    call_kwargs = oms.execute_entry_with_vwap_protection.await_args.kwargs
    assert call_kwargs["max_acceptable_vwap_cents"] == 32
    assert call_kwargs["contracts"] <= 50

    # Exit via TAKE_PROFIT
    res_out = await bridge.on_exit("ETH", "TAKE_PROFIT", 0.45)
    assert res_out["status"] == "EXITED"
    oms.execute_complete_exit.assert_awaited_once()
    assert "ETH" not in bridge.live_positions
    assert "ETH" not in pm.active_positions
    assert pm.allocated_capital == 0.0


@pytest.mark.asyncio
async def test_live_bridge_releases_margin_on_unfilled_or_price_cap():
    oms = _make_oms(dry_run=False)
    oms.execute_entry_with_vwap_protection = AsyncMock(
        return_value={"status": "UNFILLED", "reason": "0 fills", "filled_count": 0}
    )
    pm = _make_pm(200.0)
    bridge = LiveMultiCoinBridge(oms=oms, portfolio_manager=pm)

    # 1. Price above 35c cap on normal entry -> blocked before OMS, margin released
    res_cap = await bridge.on_entry("BTC", "KXBTC15M-TEST", "YES", 0.40, 72.0, is_certainty_arb=False)
    assert res_cap["status"] == "SKIPPED_PRICE_CAP"
    oms.execute_entry_with_vwap_protection.assert_not_awaited()
    assert pm.allocated_capital == 0.0

    # 2. Price above 78c cap on certainty arb -> blocked before OMS, margin released
    res_cert_cap = await bridge.on_entry("BTC", "KXBTC15M-TEST", "YES", 0.85, 100.0, is_certainty_arb=True)
    assert res_cert_cap["status"] == "SKIPPED_PRICE_CAP"
    oms.execute_entry_with_vwap_protection.assert_not_awaited()
    assert pm.allocated_capital == 0.0

    # 3. Valid entry that returns UNFILLED -> reservation cleanly released
    res_unfilled = await bridge.on_entry("BTC", "KXBTC15M-TEST", "YES", 0.30, 72.0)
    assert res_unfilled["status"] == "UNFILLED"
    assert "BTC" not in bridge.live_positions
    assert pm.allocated_capital == 0.0


@pytest.mark.asyncio
async def test_live_bridge_settlement_skips_sell_order():
    oms = _make_oms(dry_run=False)
    pm = _make_pm(200.0)
    bridge = LiveMultiCoinBridge(oms=oms, portfolio_manager=pm)

    await bridge.on_entry("ETH", "KXETH15M-TEST", "YES", 0.30, 72.0)
    res_settle = await bridge.on_exit("ETH", "SETTLED_ESTIMATED_WIN", 0.99)
    assert res_settle["status"] == "SETTLED"
    assert res_settle["payout_dollars"] == 30.0
    oms.execute_complete_exit.assert_not_awaited()
    assert "ETH" not in bridge.live_positions
    assert pm.allocated_capital == 0.0
