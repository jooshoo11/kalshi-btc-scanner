"""
schemas/positions.py — Pydantic V2 Schemas for Position Tracking and Trade History.
"""

from pydantic import BaseModel, Field
from typing import Dict, Any, List, Optional


class ActivePositionSchema(BaseModel):
    """Snapshot of an open binary options position."""
    coin: str = Field(..., description="Asset identifier (e.g. BTC, ETH)")
    ticker: str = Field(..., description="Kalshi market ticker")
    side: str = Field(..., description="Target contract side (YES or NO)")
    contracts: int = Field(..., description="Number of contracts held")
    entry_price: float = Field(..., description="Execution entry price in dollars")
    cost_dollars: float = Field(..., description="Total collateral cost in dollars")
    fee_dollars: float = Field(0.0, description="Kalshi taker fee paid in dollars")
    unrealized_pnl: Optional[float] = Field(0.0, description="Current unrealized PnL in dollars")
    roi_pct: Optional[float] = Field(0.0, description="Current ROI percentage")
    hold_time_seconds: Optional[float] = Field(0.0, description="Seconds position has been open")


class TradeSchema(BaseModel):
    """Completed trade ledger record."""
    id: Optional[int] = None
    timestamp: float
    ticker: str
    series: Optional[str] = "KXBTC15M"
    coin: Optional[str] = "BTC"
    side: str
    contracts: int
    entry_price: float
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    pnl_dollars: Optional[float] = None
    pnl_pct: Optional[float] = None
    source: Optional[str] = "multicoin"


class PortfolioStatusResponse(BaseModel):
    """Complete portfolio balance, allocation, and open positions."""
    total_cash: float
    total_equity: float
    allocated_capital: float
    available_cash: float
    active_positions_count: int
    active_assets: List[str]
    daily_drawdown_pct: float
