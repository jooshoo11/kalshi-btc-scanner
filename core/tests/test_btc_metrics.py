"""
core/tests/test_btc_metrics.py — Test Harness for BitcoinMetricsProvider.

Verifies:
  1. Spot feed priority: Hyperliquid (lead) -> Kraken (secondary) -> Coinbase (tertiary)
  2. Candle open/delta computation
  3. RSI-14 calculation
  4. Momentum bias computation
  5. BRTI accumulator integration (60-second settlement window)
  6. Liquidation proximity, OI, volume, funding rate fields
  7. get_summary_dict() returns all expected keys
"""

import sys
import time
import math
from unittest.mock import MagicMock, AsyncMock, patch

import pytest


class FakeResponse:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data

    def json(self):
        return self._json


class FakeClient:
    """Mock httpx.AsyncClient that returns canned responses."""
    def __init__(self, responses):
        self.responses = responses  # dict: (method, url) -> FakeResponse
        self.is_closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.aclose()

    async def post(self, url, json=None, timeout=None):
        key = ("POST", url)
        if key in self.responses:
            return self.responses[key]
        return FakeResponse(200, {})

    async def get(self, url, params=None, timeout=None):
        key = ("GET", url)
        if key in self.responses:
            return self.responses[key]
        return FakeResponse(200, {})

    async def aclose(self):
        self.is_closed = True


@pytest.fixture
def metrics_provider():
    from btc_metrics import BitcoinMetricsProvider
    from core.live_executor import StaleTickWatchdog
    watchdog = StaleTickWatchdog()
    return BitcoinMetricsProvider(watchdog=watchdog)


@pytest.mark.asyncio
async def test_spot_feed_hyperliquid_lead(metrics_provider):
    """Hyperliquid should be used as lead spot feed."""
    responses = {
        ("POST", "https://api.hyperliquid.xyz/info"): FakeResponse(
            200, {"BTC": "50000.00"}
        ),
    }
    client = FakeClient(responses)
    await metrics_provider.update_subsecond_spot(client)
    assert metrics_provider.spot_price == 50000.0
    assert metrics_provider.spot_source == "hyperliquid"
    assert metrics_provider.spot_prices["hyperliquid"] == 50000.0


@pytest.mark.asyncio
async def test_spot_feed_fallback_to_kraken(metrics_provider):
    """If Hyperliquid fails, Kraken should be used."""
    responses = {
        ("POST", "https://api.hyperliquid.xyz/info"): FakeResponse(500, {}),
        ("GET", "https://api.kraken.com/0/public/Ticker"): FakeResponse(
            200,
            {"result": {"XXBTZUSD": {"c": ["49950.00"]}}},
        ),
    }
    client = FakeClient(responses)
    await metrics_provider.update_subsecond_spot(client)
    assert metrics_provider.spot_price == 49950.0
    assert metrics_provider.spot_source == "kraken"


@pytest.mark.asyncio
async def test_spot_feed_fallback_to_coinbase(metrics_provider):
    """If Hyperliquid and Kraken fail, Coinbase should be used."""
    responses = {
        ("POST", "https://api.hyperliquid.xyz/info"): FakeResponse(500, {}),
        ("GET", "https://api.kraken.com/0/public/Ticker"): FakeResponse(500, {}),
        ("GET", "https://api.exchange.coinbase.com/products/BTC-USD/ticker"): FakeResponse(
            200, {"price": "49900.00"}
        ),
    }
    client = FakeClient(responses)
    await metrics_provider.update_subsecond_spot(client)
    assert metrics_provider.spot_price == 49900.0
    assert metrics_provider.spot_source == "coinbase"


@pytest.mark.asyncio
async def test_candle_open_and_delta(metrics_provider):
    """Candle open should come from Hyperliquid candle snapshot; delta = spot - open."""
    responses = {
        ("POST", "https://api.hyperliquid.xyz/info"): FakeResponse(
            200, [{"o": "49000.00", "h": "49500.00", "l": "48800.00", "c": "49200.00"}]
        ),
    }
    client = FakeClient(responses)

    # Set spot first
    spot_responses = {
        ("POST", "https://api.hyperliquid.xyz/info"): FakeResponse(
            200, {"BTC": "49500.00"}
        ),
    }
    spot_client = FakeClient(spot_responses)
    await metrics_provider.update_subsecond_spot(spot_client)

    # Now update candles
    await metrics_provider.update_candles_and_technicals(client)

    assert metrics_provider.candle_15m_open == 49000.0
    assert metrics_provider.candle_15m_delta == 500.0  # 49500 - 49000
    assert metrics_provider.candle_15m_close == 49500.0


def test_rsi_calculation(metrics_provider):
    """RSI-14 should return 50.0 with insufficient data, and a valid value with enough."""
    # Not enough price history -> 50.0
    assert metrics_provider.rsi_14 == 50.0

    # Feed a trending price series
    import time as _time
    base = _time.time()
    for i in range(20):
        metrics_provider._price_history.append((base + i, 100.0 + i))

    rsi = metrics_provider._compute_rsi(14)
    assert 0.0 <= rsi <= 100.0


def test_momentum_bias(metrics_provider):
    """Momentum bias should be 0.0 with no history, positive with upward trend."""
    # No history -> 0.0
    assert metrics_provider.momentum_bias == 0.0

    # Upward trend: first 30 entries at t-60s..t-31s (price 100), last 30 at t-30s..t-1s (price 101+)
    import time as _time
    now = _time.time()
    for i in range(30):
        metrics_provider._price_history.append((now - 60 + i, 100.0))
    for i in range(30):
        metrics_provider._price_history.append((now - 30 + i, 101.0 + i * 0.1))

    bias = metrics_provider._compute_momentum_bias()
    assert bias > 0.0


def test_brti_accumulator_integration(metrics_provider):
    """BRTI accumulator should record samples during final 60 seconds."""
    metrics_provider.record_brti_sample(spot=50000.0, seconds_left=30.0, candle_id="candle_1")
    metrics_provider.record_brti_sample(spot=50100.0, seconds_left=29.0, candle_id="candle_1")

    assert metrics_provider.brti_accumulator.has_samples()
    assert metrics_provider.brti_accumulator.sample_count() == 2
    mean = metrics_provider.get_brti_mean()
    assert mean == 50050.0


def test_summary_dict_keys(metrics_provider):
    """get_summary_dict() should return all expected keys."""
    summary = metrics_provider.get_summary_dict()
    required_keys = [
        "spot_price", "spot_source", "candle_15m_open", "candle_15m_delta",
        "vol_24h_ann_pct", "rsi_14", "momentum_bias", "funding_rate_ann_pct",
        "open_interest", "oi_change_pct", "volume_24h", "liq_proximity_pct",
        "brti_accumulator", "last_update_ts",
    ]
    for key in required_keys:
        assert key in summary, f"Missing key: {key}"

    # brti_accumulator sub-keys
    brti = summary["brti_accumulator"]
    assert "index_name" in brti
    assert "sample_count" in brti
    assert "has_samples" in brti
    assert "mean" in brti


def test_liq_proximity_and_oi_fields(metrics_provider):
    """Liquidation proximity, OI, and volume fields should be present and sane."""
    assert 0.0 <= metrics_provider.liq_proximity_pct <= 100.0
    assert metrics_provider.funding_rate_ann_pct >= 0.0
    assert metrics_provider.open_interest >= 0.0
    assert metrics_provider.volume_24h >= 0.0
