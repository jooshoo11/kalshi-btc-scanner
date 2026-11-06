"""
coins/sol/tests/test_sol_predictor.py — SOLPredictor unit tests.

Verifies:
  1. Predictor initialization from config
  2. Spot feed priority (Hyperliquid lead, Kraken fallback, Coinbase tertiary)
  3. Drift-diffusion evaluation output shape
"""

import sys
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
    from coins.sol.predictors import SOLPredictor
    return SOLPredictor()


@pytest.mark.asyncio
async def test_initialization_from_config(predictor):
    """Predictor should load config values correctly."""
    assert predictor.asset == "SOL"
    assert predictor.symbol == "SOL/USD"
    assert predictor.kalshi_series == "KXSOL15M"
    assert predictor.cf_index == "SOLUSD_RTI"
    assert predictor.spot_price == 0.0
    assert predictor.vol_15m_dollar == 2.0
    assert predictor.accumulator.index_name == "SOLUSD_RTI"
    assert predictor.accumulator.window_seconds == 60


@pytest.mark.asyncio
async def test_spot_feed_hyperliquid_lead(predictor):
    """Hyperliquid allMids should be used as lead spot feed."""
    responses = {
        ("POST", "https://api.hyperliquid.xyz/info"): FakeResponse(
            200, {"SOL": "140.00"}
        ),
    }
    client = FakeClient(responses)
    price = await predictor.update_spot_feeds(client)
    assert price == 140.0
    assert predictor.spot_price == 140.0
    assert predictor.spot_source == "hyperliquid"


@pytest.mark.asyncio
async def test_spot_feed_fallback_to_kraken(predictor):
    """If Hyperliquid fails, Kraken should be used."""
    responses = {
        ("POST", "https://api.hyperliquid.xyz/info"): FakeResponse(500, {}),
        ("GET", "https://api.kraken.com/0/public/Ticker"): FakeResponse(
            200, {"result": {"SOLUSD": {"c": ["139.50"]}}}
        ),
    }
    client = FakeClient(responses)
    price = await predictor.update_spot_feeds(client)
    assert price == 139.50
    assert predictor.spot_source == "kraken"


@pytest.mark.asyncio
async def test_spot_feed_fallback_to_coinbase(predictor):
    """If Hyperliquid and Kraken fail, Coinbase should be used."""
    responses = {
        ("POST", "https://api.hyperliquid.xyz/info"): FakeResponse(500, {}),
        ("GET", "https://api.kraken.com/0/public/Ticker"): FakeResponse(500, {}),
        ("GET", "https://api.exchange.coinbase.com/products/SOL-USD/ticker"): FakeResponse(
            200, {"price": "139.00"}
        ),
    }
    client = FakeClient(responses)
    price = await predictor.update_spot_feeds(client)
    assert price == 139.00
    assert predictor.spot_source == "coinbase"


def test_evaluate_15m_scalp_output_shape(predictor):
    """evaluate_15m_scalp should return all expected keys."""
    predictor.spot_price = 140.0
    predictor.current_15m_candle_open = 139.50
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
    assert result["asset"] == "SOL"
    assert result["cf_index"] == "SOLUSD_RTI"
