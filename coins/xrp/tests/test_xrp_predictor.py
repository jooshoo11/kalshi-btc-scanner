"""
coins/xrp/tests/test_xrp_predictor.py — XRPPredictor unit tests.

Verifies:
  1. Predictor initialization from config
  2. Spot feed priority (Coinbase lead, Kraken secondary — no Hyperliquid)
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
    from coins.xrp.predictors import XRPPredictor
    return XRPPredictor()


@pytest.mark.asyncio
async def test_initialization_from_config(predictor):
    """Predictor should load config values correctly."""
    assert predictor.asset == "XRP"
    assert predictor.symbol == "XRP/USD"
    assert predictor.kalshi_series == "KXXRP15M"
    assert predictor.cf_index == "XRPUSD_RTI"
    assert predictor.spot_price == 0.0
    assert predictor.vol_15m_dollar == 0.05
    assert predictor.accumulator.index_name == "XRPUSD_RTI"
    assert predictor.accumulator.window_seconds == 60


@pytest.mark.asyncio
async def test_spot_feed_coinbase_lead(predictor):
    """Coinbase should be used as lead spot feed for XRP (per config)."""
    responses = {
        ("GET", "https://api.exchange.coinbase.com/products/XRP-USD/ticker"): FakeResponse(
            200, {"price": "2.50"}
        ),
    }
    client = FakeClient(responses)
    price = await predictor.update_spot_feeds(client)
    assert price == 2.50
    assert predictor.spot_price == 2.50
    assert predictor.spot_source == "coinbase"


@pytest.mark.asyncio
async def test_spot_feed_fallback_to_kraken(predictor):
    """If Coinbase fails, Kraken should be used."""
    responses = {
        ("GET", "https://api.exchange.coinbase.com/products/XRP-USD/ticker"): FakeResponse(500, {}),
        ("GET", "https://api.kraken.com/0/public/Ticker"): FakeResponse(
            200, {"result": {"XXRPZUSD": {"c": ["2.49"]}}}
        ),
    }
    client = FakeClient(responses)
    price = await predictor.update_spot_feeds(client)
    assert price == 2.49
    assert predictor.spot_source == "kraken"


def test_evaluate_15m_scalp_output_shape(predictor):
    """evaluate_15m_scalp should return all expected keys."""
    predictor.spot_price = 2.50
    predictor.current_15m_candle_open = 2.48
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
    assert result["asset"] == "XRP"
    assert result["cf_index"] == "XRPUSD_RTI"
