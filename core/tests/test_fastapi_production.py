"""
core/tests/test_fastapi_production.py — Tests for FastAPI, Pydantic V2, Async SQLAlchemy 2.0 & WebSockets.
"""

import os
os.environ["TESTING"] = "1"
import pytest
import pytest_asyncio
import time
from starlette.testclient import TestClient

from schemas import (
    CoinPriceTick,
    ActivePositionSchema,
    GlobalCapsSchema,
    AssetDrawdownSchema,
    RMultipleMetricsSchema,
    RiskDashboardResponse
)
from core.db_async import (
    Base,
    TradeModel,
    get_async_engine,
    get_async_sessionmaker,
    AsyncAuditRepository
)
from core.dependencies import (
    get_portfolio_manager,
    get_multicoin_engine
)
from app import app


def test_pydantic_v2_models_validation():
    """Verify Pydantic V2 schema serialization and validation."""
    tick = CoinPriceTick(
        coin="BTC",
        spot_price=95000.5,
        source="hyperliquid",
        timestamp=time.time(),
        candle_open=94800.0,
        delta=200.5,
        model_prob=65.2
    )
    assert tick.coin == "BTC"
    assert tick.spot_price == 95000.5
    data = tick.model_dump()
    assert "spot_price" in data

    pos = ActivePositionSchema(
        coin="ETH",
        ticker="KXETH15M-SAMPLE",
        side="YES",
        contracts=20,
        entry_price=0.52,
        cost_dollars=10.40,
        fee_dollars=0.18
    )
    assert pos.contracts == 20

    risk = RiskDashboardResponse(
        status="HEALTHY",
        timestamp=time.time(),
        global_caps=GlobalCapsSchema(
            max_portfolio_allocation_pct=40.0,
            max_single_asset_allocation_pct=20.0,
            max_concurrent_positions=2,
            global_stop_loss_daily_pct=10.0
        ),
        portfolio_state={"total_cash": 100.0, "total_equity": 100.0},
        per_asset_drawdowns={
            "BTC": AssetDrawdownSchema(
                peak_equity=20.0,
                current_equity=20.0,
                current_drawdown_pct=0.0,
                max_drawdown_pct=0.0,
                realized_pnl=0.0,
                trade_count=0,
                is_active=False
            )
        },
        r_multiple_metrics=RMultipleMetricsSchema(
            total_trades=0,
            win_rate_pct=0.0,
            total_r=0.0,
            avg_win_r=0.0,
            avg_loss_r=0.0,
            expectancy_r=0.0,
            profit_factor=0.0,
            recent_r_multiples=[]
        ),
        hedging_recommendations=[]
    )
    assert risk.status == "HEALTHY"


@pytest.mark.asyncio
async def test_async_sqlalchemy_2_repository(tmp_path):
    """Verify Async SQLAlchemy 2.0 repository operations with aiosqlite."""
    test_db = f"sqlite+aiosqlite:///{tmp_path}/test_audit.db"
    engine = get_async_engine(test_db)
    session_factory = get_async_sessionmaker(test_db)

    # Initialize tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        repo = AsyncAuditRepository(session)
        # Record open trade
        trade = await repo.record_trade_open(
            ticker="KXBTC15M-TEST",
            series="KXBTC15M",
            side="YES",
            entry_price=0.50,
            contracts=10,
            cost_dollars=5.0,
            fee_dollars=0.18,
            order_group_id="grp_123"
        )
        await session.commit()
        assert trade.id is not None

        # Query recent trades
        trades = await repo.get_recent_trades(limit=10)
        assert len(trades) == 1
        assert trades[0]["ticker"] == "KXBTC15M-TEST"
        assert trades[0]["order_group_id"] == "grp_123"

        # Record trade close
        await repo.record_trade_close(
            trade_id=trade.id,
            exit_price=0.65,
            exit_reason="TAKE_PROFIT",
            pnl_dollars=1.50,
            pnl_pct=30.0
        )
        await session.commit()

        closed_trades = await repo.get_recent_trades(limit=10)
        assert closed_trades[0]["status"] == "CLOSED"
        assert closed_trades[0]["exit_reason"] == "TAKE_PROFIT"


def test_fastapi_dependency_injection_singletons():
    """Verify singleton DI access for PortfolioManager and MultiCoinEngine."""
    pm = get_portfolio_manager()
    assert pm is not None
    assert pm.max_portfolio_allocation_pct == 40.0
    assert pm.global_stop_loss_daily_pct == 10.0

    mc = get_multicoin_engine()
    assert mc is not None
    assert mc.initial_balance == 100.0


def test_fastapi_rest_and_websocket_endpoints():
    """Verify FastAPI testclient handles REST routes and WebSocket streaming."""
    from unittest.mock import patch, AsyncMock
    with patch("app.metrics_provider.update_subsecond_spot", new=AsyncMock()), \
         patch("app.metrics_provider.update_candles_and_technicals", new=AsyncMock()), \
         patch("app.metrics_provider.update_derivatives_context", new=AsyncMock()), \
         patch("app.scanner.scan_all_btc_markets", new=AsyncMock()), \
         patch("app.live_oms.reconcile_state_on_startup", new=AsyncMock(return_value={"status": "OK"})):
        client = TestClient(app)

        # 1. REST GET /api/risk/dashboard
        resp = client.get("/api/risk/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] in ("HEALTHY", "WARNING_HIGH_RISK", "HALTED_CIRCUIT_BREAKER")
        assert "global_caps" in data
        assert "r_multiple_metrics" in data

        # 2. WebSocket /ws/ticks connection and initial snapshot
        with client.websocket_connect("/ws/ticks") as ws:
            snapshot = ws.receive_json()
            assert snapshot["type"] == "snapshot"
            assert "data" in snapshot

            # Ping pong test
            ws.send_text("ping")
            pong = ws.receive_text()
            assert pong == "pong"
