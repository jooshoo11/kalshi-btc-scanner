import os
import sqlite3
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ["TESTING"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.hl_ws_feed import HyperliquidWSFeed
from core.multi_coin_trader import MultiCoinAuditDatabase, MultiCoinPaperEngine


def test_p0_db_telemetry_migration_and_persistence(tmp_path):
    db_path = str(tmp_path / "test_telemetry.db")
    db = MultiCoinAuditDatabase(db_path=db_path)

    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
    conn.close()

    for expected in (
        "slippage_bps",
        "entry_vwap",
        "exit_vwap",
        "book_depth_at_entry",
        "model_prob_at_stop",
    ):
        assert expected in cols, f"Missing telemetry column: {expected}"

    trade_id = db.record_trade_open(
        timestamp=time.time(),
        coin="BTC",
        ticker="KXBTC15M-TEST",
        series="KXBTC15M",
        side="YES",
        entry_price=0.45,
        contracts=20,
        cost_dollars=9.0,
        fee_dollars=0.35,
        model_prob_entry=68.0,
        delta_at_entry=55.0,
        seconds_left_at_entry=420,
        rationale="test entry",
        slippage_bps=12.5,
        entry_vwap=0.4506,
        book_depth_at_entry=180,
    )
    db.record_trade_close(
        trade_id=trade_id,
        exit_price=0.30,
        exit_timestamp=time.time(),
        exit_reason="STOP_LOSS",
        pnl_dollars=-3.35,
        pnl_pct=-35.8,
        exit_vwap=0.298,
        model_prob_at_stop=39.5,
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone())
    conn.close()

    assert row["slippage_bps"] == pytest.approx(12.5)
    assert row["entry_vwap"] == pytest.approx(0.4506)
    assert row["book_depth_at_entry"] == 180
    assert row["exit_vwap"] == pytest.approx(0.298)
    assert row["model_prob_at_stop"] == pytest.approx(39.5)


def test_p0_max_hold_seconds_aligned():
    import app

    assert app.multicoin_engine.max_hold_seconds == 120.0


def test_p1_rolling_r_multiple_expectancy_rebalancing(tmp_path):
    db_path = str(tmp_path / "test_rebalance.db")
    engine = MultiCoinPaperEngine(
        initial_balance=100.0,
        db_path=db_path,
        pinned_weights=None,
        disabled_coins={"SOL", "XRP"},
    )

    now = time.time()
    # Record 6 winning BTC trades (+0.5R each) and 6 losing ETH trades (-0.5R each)
    for i in range(6):
        tid_btc = engine.db.record_trade_open(
            timestamp=now + i,
            coin="BTC",
            ticker=f"KXBTC15M-{i}",
            series="KXBTC15M",
            side="YES",
            entry_price=0.40,
            contracts=10,
            cost_dollars=4.0,
            fee_dollars=0.17,
            model_prob_entry=65.0,
            delta_at_entry=40.0,
            seconds_left_at_entry=400,
            rationale="btc win",
        )
        engine.db.record_trade_close(
            trade_id=tid_btc,
            exit_price=0.60,
            exit_timestamp=now + i + 10,
            exit_reason="TAKE_PROFIT",
            pnl_dollars=2.0,
            pnl_pct=48.0,
        )

        tid_eth = engine.db.record_trade_open(
            timestamp=now + i,
            coin="ETH",
            ticker=f"KXETH15M-{i}",
            series="KXETH15M",
            side="YES",
            entry_price=0.40,
            contracts=10,
            cost_dollars=4.0,
            fee_dollars=0.17,
            model_prob_entry=65.0,
            delta_at_entry=4.0,
            seconds_left_at_entry=400,
            rationale="eth loss",
        )
        engine.db.record_trade_close(
            trade_id=tid_eth,
            exit_price=0.20,
            exit_timestamp=now + i + 10,
            exit_reason="STOP_LOSS",
            pnl_dollars=-2.0,
            pnl_pct=-48.0,
        )

    btc_exp = engine.db.get_coin_expectancy_stats("BTC", window=30)
    eth_exp = engine.db.get_coin_expectancy_stats("ETH", window=30)
    assert btc_exp["count"] == 6
    assert btc_exp["expectancy_r"] > 0.4
    assert eth_exp["expectancy_r"] < -0.4

    engine.rebalance_weights_on_performance()
    assert engine.coin_weights["BTC"] > engine.coin_weights["ETH"]
    assert engine.coin_weights["SOL"] == 0.0
    assert engine.coin_weights["XRP"] == 0.0


def test_p1_trailing_peak_drawdown_guard(tmp_path):
    db_path = str(tmp_path / "test_trailing.db")
    engine = MultiCoinPaperEngine(
        initial_balance=100.0,
        db_path=db_path,
        disabled_coins={"SOL", "XRP"},
    )
    engine.intraday_peak_equity = 100.0

    # -6.5% drawdown activates 0.5x soft throttle, does not trigger 15m pause
    engine.total_cash = 93.5
    engine._update_total_equity()
    assert engine.trailing_throttle_active is True
    assert engine._check_trailing_peak_guard() is None

    # Recovery to -2.0% clears soft throttle
    engine.total_cash = 98.0
    engine._update_total_equity()
    assert engine.trailing_throttle_active is False

    # -10.5% drawdown triggers 15m cooling pause
    engine.total_cash = 89.5
    engine._update_total_equity()
    reason = engine._check_trailing_peak_guard()
    assert reason is not None
    assert "cooling pause" in reason
    assert engine.trailing_pause_until > time.time()

    # After 15m elapses, pause clears and peak resets to current equity
    with patch("core.multi_coin_trader.time.time", return_value=engine.trailing_pause_until + 1.0):
        assert engine._check_trailing_peak_guard() is None
        assert engine.intraday_peak_equity == pytest.approx(89.5)
        assert engine.trailing_throttle_active is False


def test_p2_hyperliquid_ws_feed_5level_ofi():
    import json
    feed = HyperliquidWSFeed(coins=["BTC", "ETH"])

    # Feed allMids message
    feed.process_message(
        json.dumps(
            {
                "channel": "allMids",
                "data": {"mids": {"BTC": "90000.5", "ETH": "3100.25"}},
            }
        )
    )
    assert feed.get_spot("BTC") == pytest.approx(90000.5)
    assert feed.get_spot("ETH") == pytest.approx(3100.25)
    assert feed.is_fresh("BTC", max_age=5.0) is True

    # Build baseline L2 snapshots with slight alternating imbalance so std > 0
    for i in range(12):
        bid_sz = 10.0 + (i % 2) * 0.5
        ask_sz = 10.0 - (i % 2) * 0.5
        bids = [{"px": str(90000 - lvl), "sz": str(bid_sz)} for lvl in range(5)]
        asks = [{"px": str(90001 + lvl), "sz": str(ask_sz)} for lvl in range(5)]
        feed.process_message(
            json.dumps({"channel": "l2Book", "data": {"coin": "BTC", "levels": [bids, asks]}})
        )

    # Inject strong bid surge across top 5 levels -> positive Z_OFI
    surge_bids = [{"px": str(90000 - lvl), "sz": "80.0"} for lvl in range(5)]
    thin_asks = [{"px": str(90001 + lvl), "sz": "2.0"} for lvl in range(5)]
    feed.process_message(
        json.dumps({"channel": "l2Book", "data": {"coin": "BTC", "levels": [surge_bids, thin_asks]}})
    )

    ofi_z = feed.get_ofi_z("BTC")
    assert ofi_z > 2.0
