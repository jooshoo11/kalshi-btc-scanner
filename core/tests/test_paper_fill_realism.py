"""
core/tests/test_paper_fill_realism.py — Unit tests for L2 Order Book & Paper Fill Realism.

Covers:
1. Sub-cent tapered_deci_cent price level filtering (preventing 0c/100c book corruption).
2. YES/NO side conversion & VWAP sweeps for BUY YES, BUY NO, SELL YES, SELL NO.
3. Sequence-gap detection on orderbook_delta -> marks book STALE.
4. REST vs WS quote divergence (> 2c) or crossed book -> marks book STALE and aborts entry.
5. Realistic entry fills booked at L2 sweep VWAP (with partial fill & 15% depth cap).
6. Realistic exit fills (TAKE_PROFIT / STOP_LOSS) sweeping bid side of L2 book with partial fills.
7. Official Kalshi REST market settlement reconciliation (SETTLED_ESTIMATED_* -> SETTLED_WIN/LOSS).
"""

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.live_executor import KalshiWebSocketClient, L2OrderBook
from core.multi_coin_trader import MultiCoinPaperEngine


class DummyWSClient:
    def __init__(self):
        self.books = {}

    def get_or_create_book(self, ticker: str) -> L2OrderBook:
        if ticker not in self.books:
            self.books[ticker] = L2OrderBook(ticker)
        return self.books[ticker]


@pytest.mark.asyncio
async def test_subcent_filtering_and_yes_no_snapshot():
    """
    Verify that sub-cent levels ('0.0010', '0.0020') from Kalshi tapered_deci_cent
    orderbooks are filtered out and never create fake 0c bids or 100c asks,
    nor overwrite real cent levels when NO bids are converted to YES asks (100 - P).
    """
    ws = KalshiWebSocketClient()
    raw_msg = """
    {
        "type": "orderbook_snapshot",
        "seq": 10,
        "msg": {
            "market_ticker": "KXBTC15M-TEST",
            "yes_dollars_fp": [["0.0010", "3000000.00"], ["0.0040", "50000.00"], ["0.3100", "100.00"], ["0.3200", "50.00"]],
            "no_dollars_fp": [["0.0010", "3200000.00"], ["0.0030", "80000.00"], ["0.6600", "40.00"], ["0.6700", "60.00"]]
        }
    }
    """
    await ws._handle_message(raw_msg)
    book = ws.get_book("KXBTC15M-TEST")
    assert book is not None
    assert not book.stale
    assert book.last_seq == 10
    assert 0 not in book.bids
    assert 100 not in book.asks
    # Best YES bid = 32c (qty 50)
    assert book.get_best_bid() == (32, 50)
    # NO bid at 67c -> YES ask at 33c (qty 60); NO bid at 66c -> YES ask at 34c (qty 40)
    assert book.get_best_ask() == (33, 60)


@pytest.mark.asyncio
async def test_sequence_gap_marks_book_stale():
    """Verify that a gap in orderbook_delta seq numbers marks the L2 book stale."""
    ws = KalshiWebSocketClient()
    await ws._handle_message(
        '{"type": "orderbook_snapshot", "seq": 1, "msg": {"market_ticker": "KXBTC15M-SEQ", "yes": [[30, 100]], "no": [[68, 100]]}}'
    )
    book = ws.get_book("KXBTC15M-SEQ")
    assert not book.stale
    assert book.last_seq == 1

    # Consecutive seq=2 succeeds
    await ws._handle_message(
        '{"type": "orderbook_delta", "seq": 2, "msg": {"market_ticker": "KXBTC15M-SEQ", "side": "yes", "price_dollars": "0.3000", "delta_fp": "25.00"}}'
    )
    assert not book.stale
    assert book.bids[30] == 125

    # Gap seq=4 marks book stale
    await ws._handle_message(
        '{"type": "orderbook_delta", "seq": 4, "msg": {"market_ticker": "KXBTC15M-SEQ", "side": "yes", "price_dollars": "0.3000", "delta_fp": "10.00"}}'
    )
    assert book.stale is True


def test_buy_and_sell_vwap_sweeps_both_sides():
    """Verify BUY/SELL YES/NO sweeps walk the proper side and convert prices accurately."""
    book = L2OrderBook("KXETH15M-SWEEP")
    # YES bids: 30c (50), 28c (50)  -> implies NO asks at 70c (50), 72c (50)
    # YES asks: 32c (40), 34c (60)  -> implies NO bids at 68c (40), 66c (60)
    book.apply_snapshot(bids=[[30, 50], [28, 50]], asks=[[32, 40], [34, 60]])

    # BUY YES 60x: 40 @ 32c + 20 @ 34c = 1960c / 60 = 32.67c
    buy_yes = book.calculate_sweep_vwap("YES", 60, action="BUY")
    assert buy_yes["can_fill"] is True
    assert buy_yes["vwap_cents"] == 32.67

    # BUY NO 60x: sweeps YES bids descending: 50 @ (100-30=70c) + 10 @ (100-28=72c) = 4220c / 60 = 70.33c
    buy_no = book.calculate_sweep_vwap("NO", 60, action="BUY")
    assert buy_no["can_fill"] is True
    assert buy_no["vwap_cents"] == 70.33

    # SELL YES 60x: sweeps YES bids descending: 50 @ 30c + 10 @ 28c = 1780c / 60 = 29.67c
    sell_yes = book.calculate_sweep_vwap("YES", 60, action="SELL")
    assert sell_yes["can_fill"] is True
    assert sell_yes["vwap_cents"] == 29.67

    # SELL NO 60x: sweeps YES asks ascending (highest NO bids first): 40 @ (100-32=68c) + 20 @ (100-34=66c) = 4040c / 60 = 67.33c
    sell_no = book.calculate_sweep_vwap("NO", 60, action="SELL")
    assert sell_no["can_fill"] is True
    assert sell_no["vwap_cents"] == 67.33


def test_rest_ws_divergence_aborts_entry():
    """Verify that >2c divergence between WS top-of-book and REST ask marks book stale and aborts entry."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_div.db")
        ws = DummyWSClient()
        book = ws.get_or_create_book("KXBTC15M-DIV")
        # WS book has YES ask at 38c, but REST yes_ask is 31c (7c divergence)
        book.apply_snapshot(bids=[[29, 500]], asks=[[38, 500]])

        engine = MultiCoinPaperEngine(initial_balance=100.0, db_path=db_path, ws_client=ws)
        engine.enforce_rest_ws_consistency = True
        engine.max_rest_ws_divergence_cents = 2

        pos = engine.evaluate_entry(
            coin="BTC",
            ticker="KXBTC15M-DIV",
            series="KXBTC15M",
            signal="BUY YES",
            edge_pct=12.0,
            model_prob=70.0,
            yes_ask=0.31,
            no_ask=0.71,
            seconds_left=400,
            delta=50.0,
            candle_id="2026-11-19T12:00:00Z",
        )
        assert pos is None
        assert book.stale is True


def test_entry_books_at_l2_vwap_and_partial_exit():
    """
    Verify that:
    1. Entry is booked at the actual L2 sweep VWAP (not the top-of-book REST quote).
    2. Exit sweeps the bid side of the L2 book and executes a partial exit when bid depth < position size.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_vwap.db")
        ws = DummyWSClient()
        book = ws.get_or_create_book("KXETH15M-VWAP")
        # Ask ladder: 10 @ 30c, 50 @ 32c (total ask depth = 60)
        # Bid ladder: 25 @ 28c
        book.apply_snapshot(bids=[[28, 25]], asks=[[30, 10], [32, 50]])

        engine = MultiCoinPaperEngine(initial_balance=200.0, db_path=db_path, ws_client=ws)
        engine.enforce_rest_ws_consistency = True
        engine.allow_partial_l2_fills = True
        engine.enforce_l2_depth_cap = False

        pos = engine.evaluate_entry(
            coin="ETH",
            ticker="KXETH15M-VWAP",
            series="KXETH15M",
            signal="BUY YES",
            edge_pct=15.0,
            model_prob=72.0,
            yes_ask=0.30,
            no_ask=0.72,
            seconds_left=400,
            delta=10.0,
            candle_id="2026-11-19T12:00:00Z",
        )
        assert pos is not None
        # Position must be booked above 0.30 because only 10 contracts exist at 30c and the rest fill at 32c
        assert pos["entry_price"] > 0.30
        initial_contracts = pos["contracts"]
        assert initial_contracts > 25

        # Now trigger a TAKE_PROFIT exit when only 20 contracts are resting on the YES bid at 45c
        book.apply_snapshot(bids=[[45, 20]], asks=[[47, 100]])
        res1 = engine.update_position(
            coin="ETH",
            current_bid=0.45,
            seconds_left=300,
            settle_spot=1810.0,
            strike=1800.0,
            model_prob=80.0,
        )
        # Partial exit: position stays open with initial_contracts - 20 remaining
        assert res1 is None
        assert "ETH" in engine.active_positions
        assert engine.active_positions["ETH"]["contracts"] == initial_contracts - 20

        # Next tick: replenish bid depth and close remaining contracts
        book.apply_snapshot(bids=[[46, 100]], asks=[[48, 100]])
        res2 = engine.update_position(
            coin="ETH",
            current_bid=0.46,
            seconds_left=295,
            settle_spot=1812.0,
            strike=1800.0,
            model_prob=82.0,
        )
        assert res2 is not None
        assert res2["reason"] == "TAKE_PROFIT"
        assert res2["exit_price"] == 0.46
        assert "ETH" not in engine.active_positions


@pytest.mark.asyncio
async def test_official_settlement_reconciliation():
    """
    Verify that a trade closed at expiration as SETTLED_ESTIMATED_WIN is reconciled
    against Kalshi REST /markets/{ticker} and flipped to SETTLED_LOSS (with cash/PnL adjusted)
    if the official exchange result was actually a loss.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_settle.db")
        engine = MultiCoinPaperEngine(initial_balance=100.0, db_path=db_path, ws_client=None)

        pos = engine.evaluate_entry(
            coin="BTC",
            ticker="KXBTC15M-SETTLE",
            series="KXBTC15M",
            signal="BUY YES",
            edge_pct=15.0,
            model_prob=70.0,
            yes_ask=0.30,
            no_ask=0.72,
            seconds_left=400,
            delta=50.0,
            candle_id="2026-11-19T12:00:00Z",
        )
        assert pos is not None
        contracts = pos["contracts"]

        # At expiry, local spot estimate thinks YES won (settle_spot >= strike)
        closed = engine.update_position(
            coin="BTC",
            current_bid=0.90,
            seconds_left=2.0,
            settle_spot=63010.0,
            strike=63000.0,
            model_prob=95.0,
        )
        assert closed is not None
        assert closed["reason"] == "SETTLED_ESTIMATED_WIN"
        cash_after_est_win = engine.total_cash

        # Mock Kalshi REST /markets/KXBTC15M-SETTLE returning official result="no"
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "market": {
                "ticker": "KXBTC15M-SETTLE",
                "status": "finalized",
                "result": "no",
                "settlement_value_dollars": "0.0000",
            }
        }
        mock_http = AsyncMock()
        mock_http.get.return_value = mock_resp

        resolved = await engine.reconcile_official_settlements(http_client=mock_http)
        assert len(resolved) == 1
        assert resolved[0]["won"] is False
        assert resolved[0]["pnl_delta"] == -round(contracts * 1.0, 2)
        assert round(engine.total_cash, 2) == round(cash_after_est_win - contracts * 1.0, 2)

        trades = engine.db.get_recent_trades(limit=1)
        assert trades[0]["exit_reason"] == "SETTLED_LOSS"
        assert trades[0]["exit_price"] == 0.0
