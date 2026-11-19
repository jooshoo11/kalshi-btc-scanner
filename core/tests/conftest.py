"""
core/tests/conftest.py — Standard Pytest Fixtures for QA & Phase 4 Verification.

Provides reusable fixtures for:
- PortfolioManager with async locks & global_risk.json parameters
- Mock KalshiAuth credentials
- Mock KalshiWebSocketClient with deterministic L2 book frames
- Mock SQLite connections for database resilience testing
"""

import pytest
import pytest_asyncio
import sqlite3
from unittest.mock import MagicMock, AsyncMock

from core.portfolio_manager import PortfolioManager
from core.kalshi_client import KalshiAuth, L2OrderBook


@pytest.fixture(autouse=True)
def clean_test_environment():
    """Ensure clean kill-switch and portfolio state before and after each test."""
    try:
        from core.kill_switch import kill_switch
        kill_switch.reset()
    except Exception:
        pass
    yield
    try:
        from core.kill_switch import kill_switch
        kill_switch.reset()
    except Exception:
        pass


@pytest.fixture
def mock_kalshi_auth():
    """Mock KalshiAuth with valid signature simulation."""
    auth = MagicMock(spec=KalshiAuth)
    auth.is_configured = True
    auth.key_id = "test_key_qa_123"
    auth.sign_request.return_value = {
        "KALSHI-ACCESS-KEY": "test_key_qa_123",
        "KALSHI-ACCESS-SIGNATURE": "mock_signature_bytes_base64",
        "KALSHI-ACCESS-TIMESTAMP": "1731000000000"
    }
    return auth


@pytest_asyncio.fixture
async def portfolio_manager_fixture(mock_kalshi_auth):
    """
    Standard test fixture for PortfolioManager enforcing global_risk.json caps:
    - 40% max portfolio exposure
    - 20% single asset cap
    - 2 max concurrent positions
    - 10% daily stop loss
    """
    pm = PortfolioManager(
        auth=mock_kalshi_auth,
        rest_url="https://demo-api.kalshi.co/trade-api/v2",
        initial_balance=100.0,
        max_portfolio_allocation_pct=40.0,
        max_single_trade_pct=20.0,
        max_concurrent_positions=2,
        global_stop_loss_daily_pct=10.0,
        day_start_equity=100.0
    )
    return pm


@pytest.fixture
def deterministic_l2_book():
    """Returns a deterministic L2OrderBook seeded with known bids and asks."""
    book = L2OrderBook(ticker="KXBTC15M-TEST")
    # Seed bids: 50¢ (qty 10), 49¢ (qty 20), 48¢ (qty 30)
    book.apply_snapshot([[50, 10], [49, 20], [48, 30]], [[52, 10], [53, 20], [54, 30]])
    return book


@pytest_asyncio.fixture
async def mock_ws_client(deterministic_l2_book):
    """Mock KalshiWebSocketClient returning deterministic orderbook snapshots."""
    ws = MagicMock()
    ws.is_ws_connected = True
    ws.books = {"KXBTC15M-TEST": deterministic_l2_book}
    ws.get_book.return_value = deterministic_l2_book
    ws.get_or_create_book.return_value = deterministic_l2_book
    ws.get_best_bid_ask.return_value = (50, 10, 52, 10)
    ws.get_vwap_for_size.return_value = {
        "can_fill": True,
        "vwap_cents": 52.5,
        "filled_count": 15,
        "slippage_cents": 0.5
    }
    return ws
