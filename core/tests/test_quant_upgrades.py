"""
Unit tests for Quantitative Upgrades (Phases 1-4):
1. Cubic Brownian settlement variance & ETH sigma_1s floor fix
2. Certainty Arbitrage Engine (deterministic, velocity, cubic Z-score bounds)
3. Maker Limit Take-Profit (zero exit fee)
4. Certainty Arb Bypass & Dynamic Performance Weight Rebalancing (SOL/XRP locked at 0.0)
"""

import math
import os
import tempfile
import pytest

from core.prediction_engine import QuantPredictionEngine
from core.rti_accumulator import RtiAccumulator
from core.multi_coin_trader import MultiCoinPaperEngine, calculate_kalshi_taker_fee


def test_cubic_settlement_variance_and_eth_floor():
    engine = QuantPredictionEngine()

    # ETH has vol_15m = 5.5 -> sigma_1s = 5.5 / 30 = 0.1833 (not clamped to 1.0)
    res_eth_60 = engine.evaluate_15m_brti_accumulator(
        spot=2500.0, open_price=2499.0, seconds_left=60,
        accumulated_samples=[2500.0] * 0, dollar_vol_15m=5.5
    )
    sigma_1s_eth = 5.5 / 30.0
    expected_var_60 = (sigma_1s_eth ** 2) * 60.0 * 61.0 * 121.0 / 21600.0
    assert abs(res_eth_60["sigma_settle"] - math.sqrt(expected_var_60)) < 0.02

    # BTC variance collapse from 30s -> 10s -> 1s follows cubic O(k^3)
    res_30 = engine.evaluate_15m_brti_accumulator(
        spot=90000.0, open_price=89990.0, seconds_left=30,
        accumulated_samples=[90000.0] * 30, dollar_vol_15m=95.0
    )
    res_10 = engine.evaluate_15m_brti_accumulator(
        spot=90000.0, open_price=89990.0, seconds_left=10,
        accumulated_samples=[90000.0] * 50, dollar_vol_15m=95.0
    )
    # Ratio of sigma_settle at 30s vs 10s should be roughly (30/10)^1.5 = 5.196
    ratio = res_30["sigma_settle"] / res_10["sigma_settle"]
    assert 4.5 < ratio < 5.8


def test_rti_certainty_arbitrage_bounds():
    acc = RtiAccumulator(index_name="BRTI", window_seconds=60)
    strike = 90000.0

    # 1. Deterministic Non-Negativity Bound: 50 samples at 109,000 -> sum = 5,450,000 >= 60 * 90,000 (5,400,000)
    for _ in range(50):
        acc.add_sample(109000.0, seconds_left=20, candle_id="c1")
    res_det = acc.evaluate_certainty_arb(strike=strike, current_spot=90100.0, seconds_left=10, dollar_vol_15m=95.0)
    assert res_det["is_certainty_arb"] is True
    assert res_det["side"] == "YES"
    assert res_det["prob"] == 1.0
    assert res_det["mode"] == "DETERMINISTIC"

    # 2. Cubic Z-Score Bound: 45 samples slightly above strike -> Z >= 3.72
    acc2 = RtiAccumulator(index_name="BRTI", window_seconds=60)
    for _ in range(45):
        acc2.add_sample(90050.0, seconds_left=15, candle_id="c2")
    res_z = acc2.evaluate_certainty_arb(strike=strike, current_spot=90050.0, seconds_left=15, dollar_vol_15m=95.0)
    assert res_z["is_certainty_arb"] is True
    assert res_z["side"] == "YES"
    assert res_z["prob"] >= 0.9999


def test_maker_tp_certainty_bypass_and_rebalancing():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_paper.db")
        state_file = os.path.join(tmpdir, "portfolio_state.json")
        os.environ["PORTFOLIO_STATE_FILE"] = state_file

        engine = MultiCoinPaperEngine(
            initial_balance=200.0,
            db_path=db_path,
            pinned_weights=None,
            disabled_coins={"SOL", "XRP"},
        )
        engine.max_entry_price = 0.52
        engine.min_seconds_left = 240
        engine.max_seconds_left = 500
        engine.maker_take_profit = True

        # 1. Normal entry at 20s left should be blocked by 240s cutoff
        blocked = engine.evaluate_entry(
            coin="BTC", ticker="KXBTC15M-TEST", series="KXBTC15M",
            signal="BUY YES", edge_pct=8.0, model_prob=99.9,
            yes_ask=0.88, no_ask=0.12, seconds_left=20, delta=50.0, candle_id="c1",
            is_certainty_arb=False
        )
        assert blocked is None

        # 2. Certainty arb entry at 20s left and 88c should succeed
        pos = engine.evaluate_entry(
            coin="BTC", ticker="KXBTC15M-TEST", series="KXBTC15M",
            signal="BUY YES", edge_pct=10.0, model_prob=100.0,
            yes_ask=0.88, no_ask=0.12, seconds_left=20, delta=50.0, candle_id="c1",
            is_certainty_arb=True
        )
        assert pos is not None
        assert pos["entry_price"] == 0.88

        # 3. Maker TP exit incurs 0 exit fee
        # Entry @ 0.40 -> +25% ROI at 0.50 bid
        engine2 = MultiCoinPaperEngine(
            initial_balance=200.0,
            db_path=os.path.join(tmpdir, "test_paper2.db"),
            pinned_weights=None,
            disabled_coins={"SOL", "XRP"},
        )
        engine2.maker_take_profit = True
        pos2 = engine2.evaluate_entry(
            coin="BTC", ticker="KXBTC15M-TEST2", series="KXBTC15M",
            signal="BUY YES", edge_pct=10.0, model_prob=70.0,
            yes_ask=0.40, no_ask=0.60, seconds_left=350, delta=40.0, candle_id="c2"
        )
        assert pos2 is not None
        c_count = pos2["contracts"]
        entry_fee = pos2["fee_dollars"]
        closed = engine2.update_position(
            coin="BTC", current_bid=0.52, seconds_left=300,
            settle_spot=90050.0, strike=90000.0, model_prob=75.0
        )
        assert closed is not None
        assert closed["reason"] == "TAKE_PROFIT"
        expected_pnl = round(c_count * 0.52 - (pos2["cost_dollars"] + entry_fee), 2)
        assert abs(closed["pnl"] - expected_pnl) < 0.02

        # 4. Rebalancing keeps SOL and XRP at 0.0 and BTC+ETH = 1.0
        engine2.rebalance_weights_on_performance()
        assert engine2.coin_weights["SOL"] == 0.0
        assert engine2.coin_weights["XRP"] == 0.0
        assert abs(engine2.coin_weights["BTC"] + engine2.coin_weights["ETH"] - 1.0) < 0.01
