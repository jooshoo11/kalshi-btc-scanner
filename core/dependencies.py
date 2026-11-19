"""
core/dependencies.py — Modern FastAPI Dependency Injection for Singletons & Services.

Implements Annotated[T, Depends(...)] dependencies for:
- PortfolioManager singleton
- MultiCoinPaperEngine singleton
- Async SQLAlchemy database sessions and repositories
"""

from typing import Annotated, Optional
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from core.portfolio_manager import PortfolioManager
from core.multi_coin_trader import MultiCoinPaperEngine
from core.db_async import get_async_db, AsyncAuditRepository

# Global singleton references initialized on app lifespan
_portfolio_manager_instance: Optional[PortfolioManager] = None
_multicoin_engine_instance: Optional[MultiCoinPaperEngine] = None


def set_portfolio_manager_singleton(pm: PortfolioManager) -> None:
    """Register the initialized PortfolioManager singleton."""
    global _portfolio_manager_instance
    _portfolio_manager_instance = pm


def set_multicoin_engine_singleton(engine: MultiCoinPaperEngine) -> None:
    """Register the initialized MultiCoinPaperEngine singleton."""
    global _multicoin_engine_instance
    _multicoin_engine_instance = engine


def get_portfolio_manager() -> PortfolioManager:
    """FastAPI Dependency for accessing the centralized PortfolioManager singleton."""
    if _portfolio_manager_instance is None:
        raise RuntimeError("PortfolioManager singleton has not been initialized.")
    return _portfolio_manager_instance


def get_multicoin_engine() -> MultiCoinPaperEngine:
    """FastAPI Dependency for accessing the MultiCoinPaperEngine singleton."""
    if _multicoin_engine_instance is None:
        raise RuntimeError("MultiCoinPaperEngine singleton has not been initialized.")
    return _multicoin_engine_instance


async def get_audit_repository(
    session: Annotated[AsyncSession, Depends(get_async_db)]
) -> AsyncAuditRepository:
    """FastAPI Dependency for obtaining an AsyncAuditRepository instance."""
    return AsyncAuditRepository(session)


# Modern FastAPI Type Aliases for Clean Endpoint Signatures
PortfolioManagerDep = Annotated[PortfolioManager, Depends(get_portfolio_manager)]
MultiCoinEngineDep = Annotated[MultiCoinPaperEngine, Depends(get_multicoin_engine)]
AsyncSessionDep = Annotated[AsyncSession, Depends(get_async_db)]
AuditRepoDep = Annotated[AsyncAuditRepository, Depends(get_audit_repository)]
