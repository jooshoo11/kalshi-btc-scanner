"""
core/tests/test_multicoin_risk_gate.py — Regression tests for bid-ask spread protection,
dual-confirmation stop loss, and hold duration logic.
"""

import pytest
import time
from core.multi_coin_trader import MultiCoinPaperEngine


def test_dual_confirmation_stop_loss_grace_period():
    """Verify that during the first 30s, resting bid dips do not stop out an in-the-money trade."""
    engine = MultiCoinPaperEngine(initial_balance=100.0, db_path=":memory:")
    
    # Open YES position on BTC at 40¢
    pos = engine.active_positions["BTC"] = {
        "trade_id": 1,
        "coin": "BTC",
        "ticker": "KXBTC15M-TEST",
        "series": "KXBTC15M",
        "side": "YES",
        "contracts": 20,
        "entry_price": 0.40,
        "cost_dollars": 8.00,
        "fee_dollars": 0.34,
        "entry_timestamp": time.time(),
        "current_bid": 0.40,
        "candle_id": "test_candle"
    }

    # 5 seconds later, bid drops to 15¢ (-62.5% ROI), but spot is 95,050 vs strike 95,000 (delta +50, model prob 85%)
    res = engine.update_position(
        coin="BTC",
        current_bid=0.15,
        seconds_left=500,
        settle_spot=95050.0,
        strike=95000.0,
        current_time=pos["entry_timestamp"] + 5.0,
        model_prob=85.0
    )
    # Grace period + favorable spot: Must NOT stop out!
    assert res is None, "Expected position to remain open due to favorable spot price and grace period"
    assert "BTC" in engine.active_positions


def test_dual_confirmation_stop_loss_triggers_on_adverse_break():
    """Verify that if spot moves adverse AND ROI is <= -25%, stop loss executes."""
    engine = MultiCoinPaperEngine(initial_balance=100.0, db_path=":memory:")
    
    pos = engine.active_positions["BTC"] = {
        "trade_id": 2,
        "coin": "BTC",
        "ticker": "KXBTC15M-TEST",
        "series": "KXBTC15M",
        "side": "YES",
        "contracts": 20,
        "entry_price": 0.40,
        "cost_dollars": 8.00,
        "fee_dollars": 0.34,
        "entry_timestamp": time.time(),
        "current_bid": 0.40,
        "candle_id": "test_candle"
    }

    # 40 seconds later, bid drops to 25¢ (-37.5% ROI), spot drops below strike (94,950 vs 95,000) and model prob decays to 30%
    res = engine.update_position(
        coin="BTC",
        current_bid=0.25,
        seconds_left=400,
        settle_spot=94950.0,
        strike=95000.0,
        current_time=pos["entry_timestamp"] + 40.0,
        model_prob=30.0
    )
    assert res is not None, "Expected position to close on confirmed adverse break"
    assert res["reason"] == "STOP_LOSS"
    assert "BTC" not in engine.active_positions


def test_time_exit_preserves_winning_in_the_money_trades():
    """Verify that trades exceeding max_hold_seconds do NOT dump if strongly in the money."""
    engine = MultiCoinPaperEngine(initial_balance=100.0, db_path=":memory:")
    engine.max_hold_seconds = 120.0
    
    pos = engine.active_positions["BTC"] = {
        "trade_id": 3,
        "coin": "BTC",
        "ticker": "KXBTC15M-TEST",
        "series": "KXBTC15M",
        "side": "YES",
        "contracts": 20,
        "entry_price": 0.40,
        "cost_dollars": 8.00,
        "fee_dollars": 0.34,
        "entry_timestamp": time.time(),
        "current_bid": 0.42,
        "candle_id": "test_candle"
    }

    # 150 seconds later (past 120s), spot is way in the money (95,100 vs 95,000) and prob is 85%
    res = engine.update_position(
        coin="BTC",
        current_bid=0.42,
        seconds_left=300,
        settle_spot=95100.0,
        strike=95000.0,
        current_time=pos["entry_timestamp"] + 150.0,
        model_prob=85.0
    )
    # Must NOT exit on time: let winning trade capture $1.00 at expiry
    assert res is None, "Expected winning trade to stay open despite exceeding hold time"
    assert "BTC" in engine.active_positions


def test_position_sizing_collateral_cap():
    """Verify that high-priced contracts scale down to adhere to max collateral cap ($25)."""
    engine = MultiCoinPaperEngine(initial_balance=1000.0, db_path=":memory:")
    engine._check_portfolio_daily_loss = lambda: None
    engine._check_daily_loss_limit = lambda: None
    engine.max_trade_collateral_dollars = 25.0
    engine.max_contracts_per_trade = 60

    # 1. High-priced contract: 65¢
    # Budget would normally be 20% of sub-equity ($100), but capped at $25
    # 25 / (0.65 + fee) -> ~37 contracts (total cost ~ $24.05)
    trade_high = engine.evaluate_entry(
        coin="BTC",
        ticker="KXBTC15M-HIGH",
        series="KXBTC15M",
        signal="BUY YES",
        edge_pct=15.0,
        model_prob=80.0,
        yes_ask=0.65,
        no_ask=0.35,
        seconds_left=400,
        delta=20.0,
        candle_id="candle_1"
    )
    assert trade_high is not None
    assert trade_high["contracts"] <= 38, f"Expected contracts <= 38, got {trade_high['contracts']}"
    assert trade_high["cost_dollars"] <= 25.0, f"Expected cost <= $25, got {trade_high['cost_dollars']}"

    # Reset position and cash for comparison
    engine.active_positions.clear()
    engine.total_cash = 1000.0

    # 2. Lower-priced contract: 25¢
    # 25 / (0.25 + fee) -> 95 contracts, but capped by max_contracts_per_trade = 60
    trade_low = engine.evaluate_entry(
        coin="BTC",
        ticker="KXBTC15M-LOW",
        series="KXBTC15M",
        signal="BUY YES",
        edge_pct=15.0,
        model_prob=80.0,
        yes_ask=0.25,
        no_ask=0.75,
        seconds_left=400,
        delta=20.0,
        candle_id="candle_2"
    )
    assert trade_low is not None
    assert trade_low["contracts"] == 60, f"Expected 60 contracts, got {trade_low['contracts']}"
    assert trade_low["cost_dollars"] == 15.0


def test_early_spike_entry_price_ceiling():
    """Verify that buying the top of an early overextended spike (>500s left, delta > 70, price > 58¢) is rejected."""
    engine = MultiCoinPaperEngine(initial_balance=1000.0, db_path=":memory:")
    engine._check_portfolio_daily_loss = lambda: None
    engine._check_daily_loss_limit = lambda: None
    engine.max_spike_entry_price = 0.58
    engine.early_spike_delta_threshold = 70.0

    # Early in candle (600s left), overextended spike (delta +95), high price (63¢ > 58¢) -> Must REJECT
    trade_spike = engine.evaluate_entry(
        coin="BTC",
        ticker="KXBTC15M-SPIKE",
        series="KXBTC15M",
        signal="BUY YES",
        edge_pct=25.0,
        model_prob=88.0,
        yes_ask=0.63,
        no_ask=0.37,
        seconds_left=600,
        delta=95.0,
        candle_id="candle_spike"
    )
    assert trade_spike is None, "Expected early overextended spike to be rejected"

    # Later in candle (400s left), same price and delta -> allowed (trend established)
    trade_later = engine.evaluate_entry(
        coin="BTC",
        ticker="KXBTC15M-LATER",
        series="KXBTC15M",
        signal="BUY YES",
        edge_pct=25.0,
        model_prob=88.0,
        yes_ask=0.63,
        no_ask=0.37,
        seconds_left=400,
        delta=95.0,
        candle_id="candle_later"
    )
    assert trade_later is not None, "Expected established trend later in candle to be permitted"


def test_optimizations_suite():
    """Verify the 4 core empirical refinements: sweet-spot entry cap, timing filter, dynamic compounding, unconditional hard stop."""
    engine = MultiCoinPaperEngine(
        initial_balance=300.0,
        db_path=":memory:",
        pinned_weights={"BTC": 0.5, "ETH": 0.5, "SOL": 0.0, "XRP": 0.0},
        disabled_coins={"SOL", "XRP"}
    )
    engine._check_portfolio_daily_loss = lambda: None
    engine._check_daily_loss_limit = lambda: None
    engine.max_entry_price = 0.52
    engine.max_seconds_left = 500
    engine.enable_dynamic_compounding = True

    # 1. Timing: reject if seconds_left > 500
    t_early = engine.evaluate_entry("BTC", "TEST", "KXBTC15M", "BUY YES", 10.0, 75.0, 0.45, 0.55, seconds_left=550, delta=20.0, candle_id="c1")
    assert t_early is None, "Should reject when seconds_left > 500"

    # 2. Price: reject if price > 0.52
    t_high = engine.evaluate_entry("BTC", "TEST", "KXBTC15M", "BUY YES", 10.0, 75.0, 0.55, 0.45, seconds_left=400, delta=20.0, candle_id="c1")
    assert t_high is None, "Should reject when target_price > 0.52"

    # 3. Dynamic compounding: at $300 equity, sized at $27.00 (> $25 baseline cap)
    t_valid = engine.evaluate_entry("BTC", "TEST", "KXBTC15M", "BUY YES", 10.0, 75.0, 0.45, 0.55, seconds_left=400, delta=20.0, candle_id="c1")
    assert t_valid is not None
    assert t_valid["cost_dollars"] == 27.0, f"Expected 27.0, got {t_valid['cost_dollars']}"

    # 4. Unconditional hard stop:
    pos = engine.active_positions["BTC"] = {
        "trade_id": 999, "coin": "BTC", "ticker": "TEST", "series": "KXBTC15M",
        "side": "YES", "contracts": 20, "entry_price": 0.45, "cost_dollars": 9.0,
        "fee_dollars": 0.35, "entry_timestamp": 1000.0, "current_bid": 0.45, "candle_id": "c1"
    }
    # At hold_secs = 40 (> 30s grace), bid drops to 0.18 (-60% ROI), but spot is favorable (delta +50)
    res = engine.update_position("BTC", current_bid=0.18, seconds_left=300, settle_spot=95050.0, strike=95000.0, current_time=1040.0, model_prob=60.0)
    assert res is not None, "Hard stop must trigger unconditionally at <= -50% ROI"
    assert res["reason"] == "STOP_LOSS"


