"""
schemas/risk.py — Pydantic V2 Schemas for Global Risk Enforcement & R-Multiples.
"""

from pydantic import BaseModel, Field
from typing import Dict, Any, List, Optional


class GlobalCapsSchema(BaseModel):
    """Enforced limits from global_risk.json."""
    max_portfolio_allocation_pct: float = Field(40.0, description="Max total portfolio capital in active trades")
    max_single_asset_allocation_pct: float = Field(20.0, description="Max capital allocated to a single coin")
    max_concurrent_positions: int = Field(2, description="Max concurrent open contracts")
    global_stop_loss_daily_pct: float = Field(10.0, description="Circuit breaker halt threshold")


class AssetDrawdownSchema(BaseModel):
    """Real-time drawdown metrics per individual crypto asset."""
    peak_equity: float
    current_equity: float
    current_drawdown_pct: float
    max_drawdown_pct: float
    realized_pnl: float
    trade_count: int
    is_active: bool


class RMultipleMetricsSchema(BaseModel):
    """Institutional R-multiple expectancy and performance stats."""
    total_trades: int
    win_rate_pct: float
    total_r: float
    avg_win_r: float
    avg_loss_r: float
    expectancy_r: float
    profit_factor: float
    recent_r_multiples: List[float]


class HedgingRecommendationSchema(BaseModel):
    """Active hedging directive generated when approaching risk limits."""
    severity: str = Field(..., description="LOW, MEDIUM, HIGH, or CRITICAL")
    action: str = Field(..., description="Directive code (e.g. THROTTLE_POSITION_SIZING, DELTA_HEDGE)")
    details: str = Field(..., description="Actionable execution description")


class RiskDashboardResponse(BaseModel):
    """Unified Risk Dashboard Response for FastAPI."""
    status: str = Field(..., description="HEALTHY, WARNING_HIGH_RISK, or HALTED_CIRCUIT_BREAKER")
    timestamp: float
    global_caps: GlobalCapsSchema
    portfolio_state: Dict[str, Any]
    per_asset_drawdowns: Dict[str, AssetDrawdownSchema]
    r_multiple_metrics: RMultipleMetricsSchema
    hedging_recommendations: List[HedgingRecommendationSchema]
