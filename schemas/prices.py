"""
schemas/prices.py — Pydantic V2 Schemas for Multi-Coin Spot Feeds & Market Depth.
"""

from pydantic import BaseModel, Field
from typing import Dict, Any, List, Optional


class CoinPriceTick(BaseModel):
    """Real-time price tick and model probability for an asset."""
    coin: str = Field(..., description="Asset symbol (BTC, ETH, SOL, XRP)")
    spot_price: float = Field(..., description="Current sub-second spot price in USD")
    source: str = Field("hyperliquid", description="Exchange feed source (hyperliquid, kraken, coinbase)")
    timestamp: float = Field(..., description="Epoch timestamp in seconds")
    candle_open: Optional[float] = Field(None, description="15-minute candle opening price")
    delta: Optional[float] = Field(None, description="Price change from candle open")
    model_prob: Optional[float] = Field(None, description="Quantitative model probability (0-100)")

    model_config = {
        "json_schema_extra": {
            "example": {
                "coin": "BTC",
                "spot_price": 95420.50,
                "source": "hyperliquid",
                "timestamp": 1731000000.0,
                "candle_open": 95300.00,
                "delta": 120.50,
                "model_prob": 64.5
            }
        }
    }


class MarketDepthSchema(BaseModel):
    """Kalshi L2 order book top-of-book and sweep depth."""
    best_bid: int = Field(0, description="Best bid in cents")
    best_bid_qty: int = Field(0, description="Contracts at best bid")
    best_ask: int = Field(100, description="Best ask in cents")
    best_ask_qty: int = Field(0, description="Contracts at best ask")
    sweep_vwap_10: Optional[float] = Field(None, description="VWAP in cents to sweep 10 contracts")
    sweep_vwap_25: Optional[float] = Field(None, description="VWAP in cents to sweep 25 contracts")
    can_fill_10: bool = Field(False, description="Whether 10 contracts can be filled within limits")


class MultiCoinPriceResponse(BaseModel):
    """Aggregated multi-asset market state snapshot."""
    timestamp: float
    prices: Dict[str, CoinPriceTick]
