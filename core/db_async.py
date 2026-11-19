"""
core/db_async.py — Async SQLAlchemy 2.0 Integration for SQLite Audit Ledger.

Provides non-blocking database persistence for trade ledgers, portfolio snapshots,
and market metrics using SQLAlchemy 2.0 DeclarativeBase, AsyncSession, and aiosqlite.
"""

import time
from typing import AsyncGenerator, List, Dict, Any, Optional
from sqlalchemy import (
    Column, Integer, Float, String, Text, select, desc, func, event
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    async_sessionmaker,
    AsyncSession,
    AsyncEngine
)


class Base(DeclarativeBase):
    pass


class TradeModel(Base):
    """SQLAlchemy 2.0 model matching the trades table."""
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)
    ticker: Mapped[str] = mapped_column(String(64), nullable=False)
    series: Mapped[str] = mapped_column(String(32), nullable=False, default="KXBTC15M")
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    contracts: Mapped[int] = mapped_column(Integer, nullable=False)
    cost_dollars: Mapped[float] = mapped_column(Float, nullable=False)
    fee_dollars: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    exit_price: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_timestamp: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    exit_reason: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    pnl_dollars: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pnl_pct: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")
    model_prob_entry: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    delta_at_entry: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    seconds_left_at_entry: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    rationale: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    order_group_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    order_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)


class PortfolioSnapshotModel(Base):
    """SQLAlchemy 2.0 model matching portfolio_snapshots."""
    __tablename__ = "portfolio_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[float] = mapped_column(Float, nullable=False, default=time.time)
    cash: Mapped[float] = mapped_column(Float, nullable=False)
    pos_val: Mapped[float] = mapped_column(Float, nullable=False)
    equity: Mapped[float] = mapped_column(Float, nullable=False)
    realized: Mapped[float] = mapped_column(Float, nullable=False)
    unrealized: Mapped[float] = mapped_column(Float, nullable=False)
    win_rate: Mapped[float] = mapped_column(Float, nullable=False)
    trades_count: Mapped[int] = mapped_column(Integer, nullable=False)


# Default Async Engine & Sessionmaker
DEFAULT_DB_URL = "sqlite+aiosqlite:///multi_coin_paper.db"
_async_engine: Optional[AsyncEngine] = None
_async_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


def get_async_engine(db_url: str = DEFAULT_DB_URL) -> AsyncEngine:
    """Returns or creates the singleton async engine with HFT SQLite PRAGMAs."""
    global _async_engine
    if _async_engine is None:
        _async_engine = create_async_engine(
            db_url,
            echo=False,
            future=True,
            connect_args={"check_same_thread": False, "timeout": 30.0}
        )

        @event.listens_for(_async_engine.sync_engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode = WAL;")
            cursor.execute("PRAGMA busy_timeout = 5000;")
            cursor.execute("PRAGMA synchronous = NORMAL;")
            cursor.execute("PRAGMA cache_size = -64000;")
            cursor.execute("PRAGMA temp_store = MEMORY;")
            try:
                cursor.execute("PRAGMA mmap_size = 268435456;")
            except Exception:
                pass
            cursor.close()

    return _async_engine


def get_async_sessionmaker(db_url: str = DEFAULT_DB_URL) -> async_sessionmaker[AsyncSession]:
    """Returns or creates the async sessionmaker."""
    global _async_session_factory
    if _async_session_factory is None:
        engine = get_async_engine(db_url)
        _async_session_factory = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False
        )
    return _async_session_factory


async def init_async_models(engine: Optional[AsyncEngine] = None):
    """Initializes tables if they do not exist."""
    eng = engine or get_async_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_async_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI Dependency for obtaining an async database session."""
    session_factory = get_async_sessionmaker()
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


class AsyncAuditRepository:
    """
    High-performance async repository for querying and recording trades with SQLAlchemy 2.0.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_recent_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Fetch recent completed or open trades ordered by descending timestamp."""
        stmt = select(TradeModel).order_by(desc(TradeModel.id)).limit(limit)
        result = await self.session.execute(stmt)
        trades = result.scalars().all()
        return [
            {
                "id": t.id,
                "timestamp": t.timestamp,
                "ticker": t.ticker,
                "series": t.series,
                "side": t.side,
                "entry_price": t.entry_price,
                "contracts": t.contracts,
                "cost_dollars": t.cost_dollars,
                "fee_dollars": t.fee_dollars,
                "exit_price": t.exit_price,
                "exit_reason": t.exit_reason,
                "pnl_dollars": t.pnl_dollars,
                "pnl_pct": t.pnl_pct,
                "status": t.status,
                "order_group_id": t.order_group_id
            }
            for t in trades
        ]

    async def record_trade_open(
        self,
        ticker: str,
        series: str,
        side: str,
        entry_price: float,
        contracts: int,
        cost_dollars: float,
        fee_dollars: float,
        order_group_id: Optional[str] = None
    ) -> TradeModel:
        """Record trade entry asynchronously."""
        trade = TradeModel(
            timestamp=time.time(),
            ticker=ticker,
            series=series,
            side=side,
            entry_price=entry_price,
            contracts=contracts,
            cost_dollars=cost_dollars,
            fee_dollars=fee_dollars,
            status="OPEN",
            order_group_id=order_group_id
        )
        self.session.add(trade)
        await self.session.flush()
        return trade

    async def record_trade_close(
        self,
        trade_id: int,
        exit_price: float,
        exit_reason: str,
        pnl_dollars: float,
        pnl_pct: float
    ) -> Optional[TradeModel]:
        """Record trade exit asynchronously."""
        stmt = select(TradeModel).where(TradeModel.id == trade_id)
        result = await self.session.execute(stmt)
        trade = result.scalar_one_or_none()
        if trade:
            trade.exit_price = exit_price
            trade.exit_timestamp = time.time()
            trade.exit_reason = exit_reason
            trade.pnl_dollars = pnl_dollars
            trade.pnl_pct = pnl_pct
            trade.status = "CLOSED"
            await self.session.flush()
        return trade
