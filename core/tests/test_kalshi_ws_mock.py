"""
core/tests/test_kalshi_ws_mock.py — Mocked Kalshi WebSocket Tests for Deterministic QA.

Verifies:
1. Deterministic order book snapshot ingestion from mocked WebSocket frames.
2. Real-time L2 order book delta updates without live network I/O.
3. Callback dispatching to execution listeners upon book mutation.
4. Auto-reconnect handling and exponential backoff on simulated disconnects.
"""

import json
import asyncio
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

from core.live_executor import (
    KalshiWebSocketClient,
    KalshiAuth,
    L2OrderBook
)
from core.kalshi_client import AsyncOrderManager


class MockWebSocketConnection:
    """Async context manager that yields a sequence of deterministic JSON frames."""

    def __init__(self, frames: list, wait_after: bool = True):
        self.frames = frames
        self.wait_after = wait_after

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    async def __aiter__(self):
        for frame in self.frames:
            await asyncio.sleep(0.002)
            yield json.dumps(frame)
        if self.wait_after:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass

    async def send(self, msg):
        pass


@pytest.mark.asyncio
async def test_mocked_websocket_snapshot_and_delta():
    """Verify that simulated WebSocket frames correctly populate L2OrderBook."""
    auth_mock = MagicMock(spec=KalshiAuth)
    auth_mock.is_configured = False

    ws_client = KalshiWebSocketClient(
        ws_url="wss://demo-api.kalshi.co/trade-api/ws/v2",
        auth=auth_mock
    )
    ws_client.subscribed_tickers.add("KXBTC15M-TEST")

    # Sequence of deterministic frames:
    # 1. Snapshot: Bid 50¢ (qty 10), Ask 52¢ (qty 15)
    # 2. Delta: Bid 50¢ increases by 5 (qty 15)
    # 3. Delta: Ask 52¢ decreases by 5 (qty 10)
    simulated_frames = [
        {
            "type": "orderbook_snapshot",
            "msg": {
                "market_ticker": "KXBTC15M-TEST",
                "yes": [[50, 10]],
                "no": [[48, 15]]  # Kalshi 'no' at 48¢ implies 'yes' ask at 52¢ (100 - 48)
            }
        },
        {
            "type": "orderbook_delta",
            "msg": {
                "market_ticker": "KXBTC15M-TEST",
                "price": 50,
                "delta": 5,
                "side": "yes"
            }
        },
        {
            "type": "orderbook_delta",
            "msg": {
                "market_ticker": "KXBTC15M-TEST",
                "price": 48,
                "delta": -5,
                "side": "no"
            }
        }
    ]

    mock_conn = MockWebSocketConnection(simulated_frames)
    received_updates = []

    def book_callback(ticker: str, book: L2OrderBook):
        received_updates.append((ticker, book.get_best_bid(), book.get_best_ask()))

    ws_client.on_book_update_callbacks.append(book_callback)

    with patch("websockets.connect", return_value=mock_conn):
        # Run connect_and_listen for a brief moment then cancel
        listen_task = asyncio.create_task(ws_client.connect_and_listen())
        await asyncio.sleep(0.02)
        ws_client.is_running = False
        listen_task.cancel()
        try:
            await listen_task
        except asyncio.CancelledError:
            pass

    # Assertions
    book = ws_client.get_book("KXBTC15M-TEST")
    assert book is not None
    best_bid, bid_qty = book.get_best_bid()
    best_ask, ask_qty = book.get_best_ask()

    # Initial bid was 10, delta was +5 -> qty 15
    assert best_bid == 50
    assert bid_qty == 15

    # Initial no was 15 (yes ask 52), delta was -5 -> qty 10
    assert best_ask == 52
    assert ask_qty == 10

    # Ensure callbacks were fired
    assert len(received_updates) >= 3


@pytest.mark.asyncio
async def test_mocked_websocket_reconnect_backoff():
    """Verify that WebSocket client implements exponential backoff on simulated drop."""
    auth_mock = MagicMock(spec=KalshiAuth)
    auth_mock.is_configured = False

    ws_client = KalshiWebSocketClient(
        ws_url="wss://demo-api.kalshi.co/trade-api/ws/v2",
        auth=auth_mock
    )
    ws_client._reconnect_delay = 0.01  # Fast for test

    attempts = 0

    class FailingConnection:
        async def __aenter__(self):
            nonlocal attempts
            attempts += 1
            raise ConnectionError("Simulated socket drop")

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    with patch("websockets.connect", return_value=FailingConnection()):
        listen_task = asyncio.create_task(ws_client.connect_and_listen())
        await asyncio.sleep(0.05)
        ws_client.is_running = False
        listen_task.cancel()
        try:
            await listen_task
        except asyncio.CancelledError:
            pass

    # Should have attempted reconnection multiple times with increasing delay
    assert attempts >= 2
    assert ws_client._reconnect_delay > 0.01
