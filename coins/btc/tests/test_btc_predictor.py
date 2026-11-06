"""
coins/btc/tests/test_btc_predictor.py — BTCPredictor unit tests.

Verifies:
  1. Predictor initialization from config
  2. Spot feed priority (Hyperliquid lead, Kraken fallback, Coinbase tertiary)
  3. 15m candle open / delta computation
  4. Drift-diffusion evaluation output shape
  5. BRTI accumulator integration
"""

import sys
import time
from unittest.mock import MagicMock

import pytest


class FakeResponse:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data

    def json(self):
        return self._json


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
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
def predictor():
    from coins.btc.predictors import BTCPredictor
    return BTCPredictor()


@pytest.mark.asyncio
async def test_initialization_from_config(predictor):
    """Predictor should load config values correctly."""
    assert predictor.asset == "BTC"
    assert predictor.symbol == "BTC/USD"
    assert predictor.kalshi_series == "KXBTC15M"
    assert predictor.cf_index == "BRTI"
    assert predictor.spot_price == 0.0
    assert predictor.vol_15m_dollar == 95.0
    assert predictor.accumulator.index_name == "BRTI"
    assert predictor.accumulator.window_seconds == 60


@pytest.mark.asyncio
async def test_spot_feed_hyperliquid_lead(predictor):
    """Hyperliquid allMids should be used as lead spot feed."""
    responses = {
        ("POST", "https://api.hyperliquid.xyz/info"): FakeResponse(
            200, {"BTC": "50000.00"}
        ),
    }
    client = FakeClient(responses)
    price = await predictor.update_spot_feeds(client)
    assert price == 50000.0
    assert predictor.spot_price == 50000.0
    assert predictor.spot_source == "hyperliquid"


@pytest.mark.asyncio
async def test_spot_feed_fallback_to_kraken(predictor):
    """If Hyperliquid fails, Kraken should be used."""
    responses = {
        ("POST", "https://api.hyperliquid.xyz/info"): FakeResponse(500, {}),
        ("GET", "https://api.kraken.com/0/public/Ticker"): FakeResponse(
            200, {"result": {"XXBTZUSD": {"c": ["49950.00"]}}}
        ),
    }
    client = FakeClient(responses)
    price = await predictor.update_spot_feeds(client)
    assert price == 49950.0
    assert predictor.spot_source == "kraken"


@pytest.mark.asyncio
async def test_spot_feed_fallback_to_coinbase(predictor):
    """If Hyperliquid and Kraken fail, Coinbase should be used."""
    responses = {
        ("POST", "https://api.hyperliquid.xyz/info"): FakeResponse(500, {}),
        ("GET", "https://api.kraken.com/0/public/Ticker"): FakeResponse(500, {}),
        ("GET", "https://api.exchange.coinbase.com/products/BTC-USD/ticker"): FakeResponse(
            200, {"price": "49900.00"}
        ),
    }
    client = FakeClient(responses)
    price = await predictor.update_spot_feeds(client)
    assert price == 49900.0
    assert predictor.spot_source == "coinbase"


@pytest.mark.asyncio
async def test_update_technicals_sets_candle_open(predictor):
    """Candle open should come from Hyperliquid candle snapshot."""
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
    await predictor.update_spot_feeds(spot_client)
    await predictor.update_technicals(client)
    assert predictor.current_15m_candle_open == 49000.0


def test_evaluate_15m_scalp_output_shape(predictor):
    """evaluate_15m_scalp should return all expected keys."""
    predictor.spot_price = 76000.0
    predictor.current_15m_candle_open = 75900.0
    result = predictor.evaluate_15m_scalp(
        seconds_left=300.0,
        yes_ask=0.55,
        yes_bid=0.52,
        no_ask=0.48,
        no_bid=0.45,
    )
    required_keys = [
        "asset", "cf_index", "spot_price", "candle_open", "delta",
        "delta_pct", "prob_yes", "prob_no", "prob_yes_pct", "prob_no_pct",
        "signal", "side", "best_edge_pct", "best_ev_cents", "best_roi_pct",
        "half_kelly_pct", "full_kelly_pct", "price_cents", "confidence",
    ]
    for key in required_keys:
        assert key in result, f"Missing key: {key}"
    assert result["asset"] == "BTC"
    assert result["cf_index"] == "BRTI"


def test_brti_accumulator_integration(predictor):
    """BRTI accumulator should record samples during final 60 seconds."""
    predictor.accumulator.add_sample(spot=76000.0, seconds_left=30.0, candle_id="candle_1")
    predictor.accumulator.add_sample(spot=76100.0, seconds_left=29.0, candle_id="candle_1")
    assert predictor.accumulator.has_samples()
    assert predictor.accumulator.sample_count() == 2
    assert predictor.accumulator.get_mean() == 76050.0
