import asyncio
import os
import json
import time
import math
import sqlite3
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from fastapi import FastAPI, Depends, WebSocket, WebSocketDisconnect
from starlette.responses import JSONResponse, HTMLResponse, StreamingResponse, Response, RedirectResponse
from starlette.routing import Route
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import uvicorn
import httpx

from btc_metrics import BitcoinMetricsProvider
from core.prediction_engine import (
    QuantPredictionEngine,
    normal_cdf,
    inverse_normal_cdf,
    lookup_fee_40c,
    compute_microstructure_clean_vol,
    compute_clamped_z_shift,
    detect_stale_quote_impulse,
    SignalHysteresisManager,
)
from core.bracket_order_manager import (
    BracketOrderManager,
    BracketTicket,
    calculate_bracket_ticket,
    validate_bracket_ticket,
)
from kalshi_btc_scanner import KalshiBitcoinScanner
from core.bot_database import AuditDatabase
from core.db_pool import get_db_pool
from paper_trader import PaperTradingBot
from core.multi_coin_trader import MultiCoinPaperEngine, calculate_kalshi_taker_fee
from core.portfolio_manager import PortfolioManager
from core.db_async import init_async_models, get_async_db
from core.dependencies import (
    set_portfolio_manager_singleton,
    set_multicoin_engine_singleton,
    get_portfolio_manager,
    get_multicoin_engine,
    PortfolioManagerDep,
    MultiCoinEngineDep
)
from core.ws_manager import WebSocketConnectionManager
from core.live_bridge import LiveMultiCoinBridge
from core.hl_ws_feed import HyperliquidWSFeed
from schemas import (
    RiskDashboardResponse,
    MultiCoinPriceResponse,
    PortfolioStatusResponse
)
from core.portfolio_state import (
    init_baseline as init_portfolio_baseline,
    check_portfolio_daily_loss,
    register_equity_provider,
    combined_equity,
    load_state as load_portfolio_state,
)
from core.kill_switch import kill_switch
from core.blue_green_comparator import blue_green_comparator
from core.live_executor import (
    KALSHI_PROD_REST_URL,
    KALSHI_PROD_WS_URL,
    KALSHI_DEMO_REST_URL,
    KALSHI_DEMO_WS_URL,
    KalshiAuth,
    KalshiWebSocketClient,
    KalshiOrderManagementSystem,
    StaleTickWatchdog,
    KalshiStateReconciler
)
import logging
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s")
logger = logging.getLogger("KalshiApp")
from dotenv import load_dotenv

# Phoenix OpenTelemetry tracing DISABLED: no collector on localhost:4317 caused
# per-second export errors and dashboard lag. Modules use a no-op tracer instead.

# Load credentials from .env
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Dashboard display cap must match the engine's enforced entry-price cap
# (MultiCoinPaperEngine.max_entry_price defaults to 0.52). The 0.20-0.80
# prefilter in the loop is only a coarse allow-band; the UI must never show
# "BUY" for a setup the engine's gate will reject.
MAX_ENTRY_PRICE = 0.52

# Coins that must stay disabled regardless of config/global_risk contents.
HARD_DISABLED_ASSETS = frozenset({"SOL", "XRP"})


def _load_active_assets() -> List[str]:
    """Load global_risk.json's active_assets allow-list at import time."""
    path = os.path.join(os.path.dirname(__file__), "global_risk.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        assets = [str(a).upper() for a in cfg.get("active_assets", []) if str(a).strip()]
        if assets:
            return assets
    except Exception as e:
        logger.warning(f"[Config] Could not load global_risk.json active_assets: {e}")
    # Fail-safe default: keep the historically enabled majors only.
    return ["BTC", "ETH"]


ACTIVE_ASSETS: List[str] = _load_active_assets()


def _parse_iso_close_time(value: Any) -> Optional[float]:
    """Parse a Kalshi ISO8601 close_time into a UTC epoch timestamp, or None."""
    if not value:
        return None
    try:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


def _authoritative_open(
    mkt: Optional[Dict[str, Any]],
    candle_open: float,
    spot: float,
) -> float:
    """
    Return Kalshi's authoritative settlement reference for the 15m market.

    Kalshi publishes `floor_strike` on each market. It is the CF Benchmarks
    60-second arithmetic-mean index (BRTI / ETHUSD_RTI / SOLUSD_RTI / XRPUSD_RTI)
    at the candle open -- exactly the value `rules_primary` settles against.
    The Hyperliquid 15m candle open is only a proxy and can differ from that
    index, which would bias both `delta` and the `strike` passed to
    update_position(). Only fall back to the candle open (then spot) when
    floor_strike is missing or non-positive.
    """
    try:
        floor_strike = float((mkt or {}).get("floor_strike") or 0.0)
        if floor_strike > 0:
            return floor_strike
    except (TypeError, ValueError):
        pass
    candle = float(candle_open or 0.0)
    return candle if candle > 0 else spot

# Environment-aware Kalshi endpoints: prod when KALSHI_ENVIRONMENT=prod, else demo.
# FIX: WS/REST were hardcoded to demo URLs, which 401 when .env holds a prod key.
_kalshi_env = os.environ.get("KALSHI_ENVIRONMENT", "demo").lower()
KALSHI_REST_URL = KALSHI_PROD_REST_URL if _kalshi_env == "prod" else KALSHI_DEMO_REST_URL
KALSHI_WS_URL = KALSHI_PROD_WS_URL if _kalshi_env == "prod" else KALSHI_DEMO_WS_URL

# Global safety and execution instances
watchdog = StaleTickWatchdog(max_allowed_delta_ms=5000.0, recovery_threshold_ms=800.0, recovery_ticks=3)
metrics_provider = BitcoinMetricsProvider(watchdog=watchdog)
prediction_engine = QuantPredictionEngine(risk_free_rate=0.045, taker_fee_cents=0.5)
scanner = KalshiBitcoinScanner(metrics_provider, prediction_engine, demo=(_kalshi_env != "prod"))
db = AuditDatabase("kalshi_audit.db")
live_db = AuditDatabase("kalshi_live_demo.db")
paper_bot = PaperTradingBot(db, initial_balance=50.0, watchdog=watchdog)

# Live Kalshi OMS & WebSocket instances (prod or demo per KALSHI_ENVIRONMENT) - must be before multicoin_engine
live_auth = KalshiAuth.from_env()
live_ws = KalshiWebSocketClient(ws_url=KALSHI_WS_URL, auth=live_auth)
# SAFETY GATE (agreed plan, condition 1): dry_run is now env-gated inside the OMS —
# real order placement requires KALSHI_LIVE_TRADING=1. Do NOT pass dry_run=False here.
live_oms = KalshiOrderManagementSystem(
    auth=live_auth,
    rest_url=KALSHI_REST_URL,
    ws_client=live_ws,
    watchdog=watchdog,
)
kill_switch.register_cancel_callback(live_oms.cancel_all_orders)

# One-Click Manual Trading Execution Layer & Exit Watcher (Auto TP/SL)
bracket_manager = BracketOrderManager(
    dry_run=(os.environ.get("KALSHI_DRY_RUN", "1") == "1"),
    kalshi_client=live_oms.client if hasattr(live_oms, "client") else None,
    live_oms=live_oms,
)

multicoin_engine = MultiCoinPaperEngine(
    initial_balance=100.0,
    db_path="multi_coin_paper.db",
    pinned_weights=None,  # Enable dynamic performance-based rebalancing (BTC/ETH 20%-80% bounds)
    disabled_coins={"SOL", "XRP"},
    ws_client=live_ws  # Pass WebSocket client for L2 book access
)
multicoin_engine.max_entry_price = 0.35
multicoin_engine.max_seconds_left = 660
multicoin_engine.min_seconds_left_by_coin = {"BTC": 360, "ETH": 240}
multicoin_engine.min_prob_by_coin = {"BTC": 65.0, "ETH": 60.0}
multicoin_engine.min_edge_pct = 4.0
multicoin_engine.take_profit_roi_pct = 25.0
multicoin_engine.max_trades_per_candle = 3
multicoin_engine.min_contracts_per_trade = 5
multicoin_engine.max_contracts_per_trade = 120
multicoin_engine.max_hold_seconds = 120.0
# Contract-size ramp: starts at 5 contracts (matches min_contracts_per_trade & avoids 1c fee rounding trap)
multicoin_engine.max_contracts_ramp_cap = 5
_contract_ramp = {
    "trades_entered": 0,
    "stage": 1,  # 1=5c, 2=15c, 3=full
}


def _update_contract_ramp() -> None:
    """Advance contract-size ramp based on clean trades entered AND positive realized PnL."""
    n = _contract_ramp["trades_entered"]
    stage = _contract_ramp["stage"]
    net_pnl = float(multicoin_engine.total_equity - multicoin_engine.initial_balance)
    if net_pnl <= 0.0:
        new_cap = 5
        new_stage = 1
    elif stage == 1 and n >= 10:
        new_cap = 8
        new_stage = 2
    elif stage == 2 and n >= 25 and net_pnl >= 8.0:
        new_cap = 15
        new_stage = 3
    else:
        new_cap = multicoin_engine.max_contracts_ramp_cap
        new_stage = stage
    if new_cap != multicoin_engine.max_contracts_ramp_cap:
        multicoin_engine.max_contracts_ramp_cap = new_cap
        _contract_ramp["stage"] = new_stage
        print(
            f"[ContractRamp] Stage {new_stage}: max_contracts_ramp_cap -> {new_cap} "
            f"({n} clean trades entered, net_pnl=${net_pnl:+.2f})"
        )
multicoin_engine.stop_loss_hard_roi_pct = 45.0
multicoin_engine.maker_take_profit = True
multicoin_engine.enable_dynamic_compounding = True
multicoin_engine.max_hold_seconds = 120.0
multicoin_engine.enforce_rest_ws_consistency = False
multicoin_engine.max_rest_ws_divergence_cents = 3
multicoin_engine.allow_partial_l2_fills = True
multicoin_engine.enforce_l2_depth_cap = True
multicoin_engine.l2_depth_cap_pct = 0.15
multicoin_engine.cf_disloc_mode = True
multicoin_engine.structural_stop_enabled = True
multicoin_engine.structural_stop_min_hold_sec = 45.0
multicoin_engine.structural_stop_roi_pct = 35.0
multicoin_engine.structural_stop_z = -0.65
multicoin_engine.structural_catastrophe_roi_pct = 48.0
multicoin_engine.trailing_profit_lock_enabled = True
multicoin_engine.intraday_peak_equity = max(1.0, float(multicoin_engine.total_equity))

hl_ws = HyperliquidWSFeed(coins=["BTC", "ETH"])

portfolio_manager = PortfolioManager(
    auth=live_auth,
    rest_url=KALSHI_REST_URL,
    initial_balance=100.0,
    max_portfolio_allocation_pct=40.0,
    max_single_trade_pct=20.0,
    max_concurrent_positions=2,
    global_stop_loss_daily_pct=10.0,
    day_start_equity=100.0
)

live_bridge = LiveMultiCoinBridge(
    oms=live_oms,
    portfolio_manager=portfolio_manager,
    normal_max_price_cents=int(round(multicoin_engine.max_entry_price * 100)),
    certainty_max_price_cents=78,
)

# Initialize DI singletons and WebSocket streaming manager
set_portfolio_manager_singleton(portfolio_manager)
set_multicoin_engine_singleton(multicoin_engine)
from collections import deque as _module_deque
ws_manager = WebSocketConnectionManager()
multicoin_live_matrix: Dict[str, Dict[str, Any]] = {}
_chart_tick_history: Dict[str, Any] = {c: _module_deque(maxlen=360) for c in ("BTC", "ETH")}

# =====================================================================
# LOW-LATENCY INFRASTRUCTURE
# Shared httpx connection pool (reuse TCP/TLS across all loops) and
# unified spot feed (Coinbase/Kraken/Hyperliquid WS instead of REST polling).
# =====================================================================
_shared_http_client: Optional[httpx.AsyncClient] = None
spot_feed: Optional[SpotFeedManager] = None


def get_http_client() -> httpx.AsyncClient:
    global _shared_http_client
    if _shared_http_client is None:
        _shared_http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=2.0, read=3.0, write=2.0, pool=5.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=100, keepalive_expiry=30.0),
        )
    return _shared_http_client


async def close_http_client():
    global _shared_http_client
    if _shared_http_client is not None:
        await _shared_http_client.aclose()
        _shared_http_client = None


# Import spot feed manager lazily to avoid circular import at module load
from core.spot_feed_manager import get_spot_feed, reset_spot_feed  # noqa: E402
# FIX (crash): this was initialized as a dict {} but used with .append()/.pop(0),
# raising AttributeError on every heartbeat and every trade log line.
multicoin_brain_logs: List[Dict[str, Any]] = []

# UNIFIED ENGINE FLAG: MultiCoinPaperEngine is now the single trading loop
# (BTC/ETH 50/50 pinned, SOL/XRP disabled, L2 validation + risk gates).
# The legacy single-coin PaperTradingBot is kept read-only for its history;
# set this True to re-enable its trading loop (not recommended: double-trades KXBTC15M).
LEGACY_PAPER_BOT_TRADING = False

# PORTFOLIO-WIDE risk state (agreed plan, phase 3): one shared daily-loss baseline
# across ALL engines, initialized from confirmed-settlement equity only (condition 5).
# Only the multicoin engine feeds into combined_equity() — SOL/XRP are disabled,
# and the legacy paper bot is read-only history (LEGACY_PAPER_BOT_TRADING=False).
register_equity_provider(lambda: float(multicoin_engine.get_status_dict().get("total_equity", 0.0) or 0.0))

def _portfolio_engine_dbs() -> Dict[str, Dict[str, Any]]:
    return {
        "multicoin": {"db_path": "multi_coin_paper.db", "initial_balance": multicoin_engine.initial_balance},
    }

live_execution_mode: str = "PAPER"  # "PAPER" or "LIVE_DEMO"
live_demo_balance: Dict[str, Any] = {"balance": 5254, "balance_dollars": 52.54}
startup_reconciliation_report: Dict[str, Any] = {}

# ---------------------------------------------------------------------------
# SSE / tick-payload change detection cache.
# Prevents redundant JSON serialization + SQLite hits when nothing changed.
# ---------------------------------------------------------------------------
_tick_payload_cache: Dict[str, Any] = {}
_tick_payload_hash: str = ""
_trades_cache: List[Dict[str, Any]] = []
_trades_cache_ts: float = 0.0
_TRADES_CACHE_TTL = 3.0  # seconds — refresh trade list at most every 3s


def _trades_cache_get() -> List[Dict[str, Any]]:
    """Return cached recent trades, refreshing from SQLite at most every _TRADES_CACHE_TTL."""
    global _trades_cache, _trades_cache_ts
    now = time.time()
    if now - _trades_cache_ts < _TRADES_CACHE_TTL and _trades_cache:
        return _trades_cache
    try:
        rows = live_db.get_recent_trades(limit=20)
        _trades_cache = [dict(r) for r in rows]
        _trades_cache_ts = now
    except Exception:
        pass
    return _trades_cache


def _payload_hash(payload: Dict[str, Any]) -> str:
    """Stable hash of the payload for change detection (ignores timestamp)."""
    import hashlib
    # Remove volatile timestamp before hashing
    p = {k: v for k, v in payload.items() if k != "timestamp"}
    return hashlib.sha256(json.dumps(p, sort_keys=True, default=str).encode()).hexdigest()

async def fast_spot_loop():
    """
    High-frequency sub-second loop: updates live spot from WebSocket feeds,
    refreshes KXBTC15M quotes, and evaluates paper trading bot.
    Runs at 0.25s interval with shared HTTP connection pool and WS spot prices.
    """
    print("Starting high-frequency spot & paper bot loop (0.25s interval, WS spot feeds)...")
    while True:
        try:
            # Sub-second spot from WS feeds (Coinbase/Kraken/Hyperliquid) — no REST polling
            spots = spot_feed.get_all_spots() if spot_feed else {}
            btc_spot = spots.get("BTC", 0.0)
            eth_spot = spots.get("ETH", 0.0)

            # Update metrics provider spot from WS feed
            if btc_spot > 0:
                metrics_provider.spot_price = btc_spot
                watchdog.record_tick()  # Keep watchdog heartbeat alive with fresh WS ticks

            # Use shared HTTP client for Kalshi market data (connection pooled)
            client = get_http_client()
            scalp_data = await scanner.scan_kxbtc15m_fast(client)
            if scalp_data:
                # Keep live WebSocket subscribed to current KXBTC15M ticker
                cur_ticker = scalp_data.get("ticker")
                if cur_ticker and cur_ticker not in live_ws.subscribed_tickers:
                    asyncio.create_task(live_ws.subscribe_ticker(cur_ticker))

                if LEGACY_PAPER_BOT_TRADING:
                    # Legacy single-coin bot — disabled under the unified-engine
                    # architecture; MultiCoinPaperEngine handles all entries/exits.
                    paper_bot.update_tick(
                        scalp_data=scalp_data,
                        spot_price=metrics_provider.spot_price,
                        metrics=metrics_provider.get_summary_dict()
                    )
        except Exception as e:
            print(f"[FastSpotLoop] Warning: {e}")
        await asyncio.sleep(0.25)

async def full_scan_loop():
    """Medium-frequency loop (every 15s): scans all 600+ Kalshi BTC contracts, recalculates models, and syncs Demo balance."""
    print("Starting full Kalshi BTC market scan loop...")
    client = get_http_client()
    while True:
        try:
            await scanner.scan_all_btc_markets(client)
            if live_auth.is_configured:
                headers = live_auth.sign_request("GET", "/trade-api/v2/portfolio/balance")
                resp = await client.get(f"{KALSHI_REST_URL}/portfolio/balance", headers=headers, timeout=3.0)
                if resp.status_code == 200:
                    data = resp.json()
                    live_demo_balance["balance"] = data.get("balance", 5254)
                    live_demo_balance["balance_dollars"] = float(data.get("balance", 5254)) / 100.0
        except Exception as e:
            print(f"[Loop] Error: {e}")
        await asyncio.sleep(15.0)

async def technicals_loop():
    """Low-frequency loop (every 60s): refreshes 720+ candles, realized volatilities, records audit logs."""
    print("Starting technicals & audit persistence loop...")
    client = get_http_client()
    # Initial run
    await metrics_provider.update_candles_and_technicals(client)
    await metrics_provider.update_derivatives_context(client)
    while True:
        await asyncio.sleep(60.0)
        try:
            await metrics_provider.update_candles_and_technicals(client)
            await metrics_provider.update_derivatives_context(client)

            # Record metrics audit snapshot
            m = metrics_provider.get_summary_dict()
            db.log_metrics_snapshot(
                spot=m["spot_price"],
                open_p=m["candle_15m_open"],
                delta=m["candle_15m_delta"],
                vol_24h=m["vol_24h_ann_pct"],
                rsi=m["rsi_14"],
                funding=m["funding_rate_ann_pct"],
                prob_15m=scanner.cache.get("kxbtc15m", {}).get("model_prob_yes_pct", 50.0)
            )

            # Record portfolio snapshot
            b_stat = paper_bot.get_status_dict()
            db.log_portfolio_snapshot(
                cash=b_stat["cash"],
                pos_val=round(b_stat["total_equity"] - b_stat["cash"], 2),
                equity=b_stat["total_equity"],
                realized=b_stat["realized_pnl"],
                unrealized=b_stat["unrealized_pnl"],
                win_rate=b_stat["win_rate_pct"],
                trades_count=b_stat["total_trades"]
            )
        except Exception as e:
                print(f"[Loop] Error: {e}")

def normal_cdf(x: float) -> float:
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

async def periodic_reconciliation_loop():
    """
    Distributed Saga Maintenance Loop (every 30s):
    1. Reconciles local portfolio_state.json with live exchange balances and positions.
    2. Sweeps and auto-cancels orphaned ghost orders older than max_hold_seconds (120s).
    3. Reconciles exchange state with local SQLite audit database.
    """
    client = get_http_client()
    while True:
        await asyncio.sleep(30.0)
        try:
            if live_oms.dry_run:
                # In paper mode, do not overwrite shared portfolio_state.json with real exchange balance
                continue
            # 1. 30s Portfolio State Exchange Sync
            sync_rep = await live_oms.sync_portfolio_state_30s(client=client)
            if sync_rep.get("discrepancies"):
                print(f"[Reconcile 30s] Portfolio sync notice: {sync_rep['discrepancies']}")

            # 2. Orphan Ghost Order Auto-Cancellation (> 120s)
            cancelled_orphans = await live_oms.sweep_and_cancel_orphaned_orders(max_hold_seconds=120.0)
            if cancelled_orphans:
                print(f"[Reconcile 30s] Auto-cancelled {len(cancelled_orphans)} ghost order(s) (>120s old): {cancelled_orphans}")

            # 3. Exchange state cross-check with local DB
            if live_auth.is_configured:
                report = await live_oms.reconcile_exchange_state(
                    db_path_or_db=live_db,
                    cancel_orphaned_orders=False
                )
                if report.get("discrepancies_found"):
                    print(f"[Reconcile 30s] {len(report['discrepancies_found'])} discrepancy(ies) resolved:")
                    for d in report["discrepancies_found"]:
                        print(f"  - {d}")
        except Exception as e:
            print(f"[Reconcile 30s] Error: {e}")

async def paper_reconcile_loop():
    """
    Every 60s: reconcile the (single) multi-coin paper engine's in-memory state
    against SQLite (source of truth). Repairs cash drift and re-attaches orphaned
    OPEN trades. Also surfaces watchdog halts as ALERTS ONLY — paper trading must
    never be silently frozen by a stale feed (live OMS blocking stays in live_executor).
    """
    while True:
        await asyncio.sleep(60.0)
        try:
            report = multicoin_engine.reconcile_with_db()
            if report["discrepancies"]:
                print(f"[PaperReconcile] {len(report['discrepancies'])} discrepancy(ies) resolved:")
                for d in report["discrepancies"]:
                    print(f"  - {d}")
            else:
                print(f"[PaperReconcile] In sync | Cash ${report['cash_after']} | Equity ${report['equity']} | Open: {report['open_positions']}")
            # Refresh the shared portfolio-wide baseline (no-op unless the calendar day changed)
            init_portfolio_baseline(_portfolio_engine_dbs())
            pd_reason = check_portfolio_daily_loss(combined_equity())
            if pd_reason:
                print(f"[PaperReconcile] {pd_reason}")
            wd = watchdog.get_status()
            if wd.get("is_halted"):
                print(f"[PaperReconcile] ALERT (non-blocking): market-data watchdog HALTED ({wd.get('halt_reason')}) — live OMS is blocked, paper engine continues")
        except Exception as e:
            print(f"[PaperReconcile] Error: {e}")

async def kill_switch_watcher():
    """
    Continuous Kill-Switch and 10% Daily Drawdown Circuit Breaker Watcher.
    Monitors KILL file and combined equity drawdown against 10% stop loss.
    Halts all trading, cancels resting orders, and triggers automated rollback.
    """
    while True:
        await asyncio.sleep(2.0)
        try:
            tripped, reason = kill_switch.is_tripped()
            if not tripped:
                tripped, reason = kill_switch.check_daily_loss(combined_equity())

            if tripped:
                if paper_bot.enabled:
                    print(f"[KILL SWITCH] Emergency Halt Active ({reason})! Disabling paper bot.")
                    paper_bot.enabled = False
                if not live_oms.dry_run:
                    print(f"[KILL SWITCH] Live trading armed during halt! Reverting to paper and cancelling orders.")
                    await live_oms.trigger_rollback_to_paper(reason=f"Kill switch active: {reason}")
        except Exception as e:
            print(f"[KILL SWITCH] Watcher check error: {e}")

async def multi_coin_production_loop():
    """
    Continuous Multi-Asset (BTC, ETH, SOL, XRP) production paper trading loop.
    Evaluates drift-diffusion edge against real Kalshi Production order books (api.elections.kalshi.com)
    and dynamically rebalances performance-based capital weights ($100 initial baseline).
    """
    print("Starting Multi-Coin Production Paper Trading Loop (BTC, ETH, SOL, XRP)...")
    # Load per-coin configs for series and vol_15m defaults
    import json as _json
    from pathlib import Path as _Path
    _repo_root = _Path(__file__).resolve().parent
    # Per-asset 15m dollar volatility fallback (USD move per 15m, 1 sigma).
    # Must NOT be derived from allocation percentages — that collapses BTC's vol
    # from ~95 to ~10 and silently makes every model probability 0% or 100%.
    _default_vol_15m = {"BTC": 95.0, "ETH": 6.5, "SOL": 2.0, "XRP": 0.05}
    _coin_configs_loaded = {}
    for _asset in ("BTC", "ETH", "SOL", "XRP"):
        _cfg_path = _repo_root / "coins" / _asset.lower() / "config.json"
        try:
            with open(_cfg_path, "r") as _f:
                _cfg = _json.load(_f)
            _coin_configs_loaded[_asset] = {
                **_cfg,
                "series": _cfg.get("kalshi_series", f"KX{_asset}15M"),
                "vol_15m": float(_cfg.get("vol_15m_dollar", _default_vol_15m.get(_asset, 1.0))),
                "enabled": _cfg.get("enabled", False),
            }
        except FileNotFoundError:
            _coin_configs_loaded[_asset] = {
                "series": f"KX{_asset}15M",
                "vol_15m": _default_vol_15m.get(_asset, 1.0),
                "enabled": False,
            }

    # global_risk.json active_assets is an authoritative allow-list. SOL/XRP stay
    # disabled even if a config/global_risk entry accidentally enables them.
    coin_configs = {
        k: v for k, v in _coin_configs_loaded.items()
        if v["enabled"] and k.upper() in ACTIVE_ASSETS and k.upper() not in HARD_DISABLED_ASSETS
    }
    if not coin_configs:
        # Only fall back when config enablement produced nothing, and still respect
        # the active_assets gate (never re-enable SOL/XRP here).
        _fallback_assets = [
            a for a in ("BTC", "ETH")
            if a in ACTIVE_ASSETS and a not in HARD_DISABLED_ASSETS
        ]
        if _fallback_assets:
            coin_configs = {
                a: {"series": f"KX{a}15M", "vol_15m": _default_vol_15m[a], "enabled": True}
                for a in _fallback_assets
            }
            print(f"[MultiCoinProd] No enabled coin configs — falling back to {_fallback_assets}")
        else:
            print("[MultiCoinProd] No active assets after global_risk gating — loop will idle")
    print(
        f"[MultiCoinProd] Effective assets: {sorted(coin_configs)} "
        f"(global_risk.active_assets={ACTIVE_ASSETS}, hard-disabled={sorted(HARD_DISABLED_ASSETS)})"
    )

    # Same 0.68 cap the engine enforces; used by the dashboard matrix so the UI
    # never advertises a BUY that evaluate_entry will reject.
    max_entry_price = float(getattr(multicoin_engine, "max_entry_price", MAX_ENTRY_PRICE))
    _equity_warning_logged = False

    candle_opens: Dict[str, float] = {}
    last_candle_fetch = 0.0
    cached_markets: Dict[str, Dict[str, Any]] = {}
    last_mkt_fetch = 0.0
    last_heartbeat = 0.0
    _ws_subscribed_tickers: set = set()
    from collections import deque as _deque
    from core.rti_accumulator import RtiAccumulator
    from core.strategy_selector import StrategySelector
    from core.regime_detector import RegimeDetector
    _strategy_selectors = {
        c: StrategySelector(detector=RegimeDetector())
        for c in ("BTC", "ETH", "SOL", "XRP")
    }
    _spot_tick_history: Dict[str, _deque] = {c: _deque(maxlen=45) for c in ("BTC", "ETH", "SOL", "XRP")}
    _cf_spot_cache: Dict[str, float] = {}
    _perp_basis_ema: Dict[str, float] = {}
    _last_cf_fetch: float = 0.0
    _rti_accumulators: Dict[str, RtiAccumulator] = {
        "BTC": RtiAccumulator(index_name="BRTI", window_seconds=60),
        "ETH": RtiAccumulator(index_name="ETHUSD_RTI", window_seconds=60),
        "SOL": RtiAccumulator(index_name="SOLUSD_RTI", window_seconds=60),
        "XRP": RtiAccumulator(index_name="XRPUSD_RTI", window_seconds=60),
    }
    _signal_hysteresis_mgr = SignalHysteresisManager()

    async with httpx.AsyncClient(timeout=4.0) as client:
        while True:
            try:
                kill_file_active = os.path.exists("KILL")

                now = time.time()
                now_ms = int(now * 1000)
                candle_15m_start = (now_ms // (15 * 60 * 1000)) * (15 * 60 * 1000)
                candle_15m_end = candle_15m_start + (15 * 60 * 1000)
                # Fallback only: the per-coin evaluation below prefers the market's
                # own close_time, which is the authoritative expiry (the wall-clock
                # epoch grid can drift from Kalshi's actual close).
                grid_seconds_left = max(0.0, float((candle_15m_end - now_ms) / 1000.0))
                seconds_left = grid_seconds_left

                # Equity sanity (one-time): the paper engine keeps its $100
                # experiment baseline. Never silently substitute exchange equity;
                # surface the difference once so ROI comparisons are not misread.
                if not _equity_warning_logged and live_execution_mode == "PAPER" and live_auth.is_configured:
                    _exchange_equity = float(live_demo_balance.get("balance_dollars") or 0.0)
                    _paper_equity = float(getattr(multicoin_engine, "initial_balance", 0.0))
                    if _exchange_equity > 0 and abs(_exchange_equity - _paper_equity) >= 0.01:
                        _equity_warning_logged = True
                        logger.warning(
                            "[MultiCoinProd] EQUITY MISMATCH: paper engine baseline is $%.2f "
                            "but exchange balance is $%.2f. Paper equity is intentionally "
                            "NOT reset or replaced — paper PnL/ROI remains measured on the "
                            "$%.2f experiment baseline.",
                            _paper_equity, _exchange_equity, _paper_equity,
                        )

                # 1A. Refresh Hyperliquid perp lead prices from persistent WebSocket (REST fallback only if stale)
                hl_lead_prices: Dict[str, float] = {}
                stale_coins = []
                for coin in coin_configs:
                    ws_spot = hl_ws.get_spot(coin)
                    if ws_spot is not None and ws_spot > 0 and hl_ws.is_fresh(coin, max_age=5.0):
                        hl_lead_prices[coin] = ws_spot
                    else:
                        stale_coins.append(coin)

                # Query REST allMids only if coins are actually stale AND at least 5s elapsed
                if stale_coins and (now - _last_cf_fetch >= 5.0 or not hl_lead_prices):
                    try:
                        hl_resp = await client.post("https://api.hyperliquid.xyz/info", json={"type": "allMids"}, timeout=1.0)
                        if hl_resp.status_code == 200:
                            mids = hl_resp.json()
                            for coin in stale_coins:
                                val = mids.get(coin)
                                if val is not None:
                                    hl_lead_prices[coin] = float(val)
                    except Exception as e:
                        logger.debug(f"[MultiCoinProd] HL allMids fallback: {e}")

                # 1B. Refresh Coinbase + Kraken CF Benchmarks spot reference directly from real-time WS spot feeds (0ms)
                _active_spot_feed = spot_feed or get_spot_feed()
                for coin in coin_configs:
                    if _active_spot_feed:
                        _ws_p = _active_spot_feed.get_spot(coin)
                        if _ws_p > 0:
                            _cf_spot_cache[coin] = _ws_p

                # Slow REST fallback only if a coin has NO valid spot price at all
                _missing_cf = [c for c in coin_configs if _cf_spot_cache.get(c, 0.0) <= 0]
                if _missing_cf and (now - _last_cf_fetch >= 5.0):
                    _last_cf_fetch = now
                    _cb_prices: Dict[str, float] = {}
                    _kr_prices: Dict[str, float] = {}
                    for coin in _missing_cf:
                        try:
                            cb_r = await client.get(
                                f"https://api.exchange.coinbase.com/products/{coin}-USD/ticker",
                                timeout=1.0,
                            )
                            if cb_r.status_code == 200:
                                _p = float(cb_r.json().get("price") or 0.0)
                                if _p > 0:
                                    _cb_prices[coin] = _p
                        except Exception:
                            pass
                    try:
                        kr_r = await client.get(
                            "https://api.kraken.com/0/public/Ticker?pair=XBTUSD,ETHUSD",
                            timeout=1.0,
                        )
                        if kr_r.status_code == 200:
                            kr_res = kr_r.json().get("result", {})
                            for k_name, k_obj in kr_res.items():
                                c_list = k_obj.get("c") or []
                                if c_list:
                                    _kp = float(c_list[0] or 0.0)
                                    if _kp > 0:
                                        if "XBT" in k_name or "BTC" in k_name:
                                            _kr_prices["BTC"] = _kp
                                        elif "ETH" in k_name:
                                            _kr_prices["ETH"] = _kp
                    except Exception:
                        pass

                    for coin in _missing_cf:
                        samples = [p for p in (_cb_prices.get(coin), _kr_prices.get(coin)) if p and p > 0]
                        if samples:
                            _cf_spot_cache[coin] = sum(samples) / len(samples)

                # Combine CF Spot + De-Biased Hyperliquid Perp Lead-Lag Basis
                spot_prices = {}
                leadlag_impulse_bps: Dict[str, float] = {}
                for coin in coin_configs:
                    hl_p = hl_lead_prices.get(coin, 0.0)
                    cf_p = _cf_spot_cache.get(coin, 0.0)
                    if cf_p > 0 and hl_p > 0:
                        inst_basis = hl_p - cf_p
                        if coin not in _perp_basis_ema:
                            _perp_basis_ema[coin] = inst_basis
                        else:
                            _perp_basis_ema[coin] = 0.95 * _perp_basis_ema[coin] + 0.05 * inst_basis
                        imp_dollar = inst_basis - _perp_basis_ema[coin]
                        leadlag_impulse_bps[coin] = (imp_dollar / cf_p) * 10000.0
                        spot_prices[coin] = cf_p
                    elif cf_p > 0:
                        spot_prices[coin] = cf_p
                        leadlag_impulse_bps[coin] = 0.0
                    elif hl_p > 0:
                        spot_prices[coin] = hl_p - _perp_basis_ema.get(coin, 0.0)
                        leadlag_impulse_bps[coin] = 0.0
                    if coin in spot_prices and spot_prices[coin] > 0:
                        _spot_tick_history.setdefault(coin, _deque(maxlen=45)).append((now, spot_prices[coin]))

                # 2. Refresh candle opens every 20s
                if now - last_candle_fetch > 20.0 or not candle_opens:
                    for coin in coin_configs:
                        try:
                            c_resp = await client.post("https://api.hyperliquid.xyz/info", json={
                                "type": "candleSnapshot",
                                "req": {
                                    "coin": coin,
                                    "interval": "15m",
                                    "startTime": candle_15m_start,
                                    "endTime": now_ms
                                }
                            })
                            if c_resp.status_code == 200:
                                candles = c_resp.json()
                                if candles:
                                    candle_opens[coin] = float(candles[-1].get("o", spot_prices.get(coin, 0.0)))
                            else:
                                print(f"[MultiCoinProd] Candle fetch for {coin} returned {c_resp.status_code}")
                        except Exception as e:
                            print(f"[MultiCoinProd] Candle fetch error for {coin}: {e}")
                    last_candle_fetch = now

                # 3. Refresh active production markets every 10s (or immediately on candle rollover)
                _any_expired = any(
                    c not in cached_markets
                    or ((_parse_iso_close_time(cached_markets[c].get("close_time", "")) or 0.0) <= now)
                    for c in coin_configs
                )
                if (_any_expired and (now - last_mkt_fetch > 2.0)) or (now - last_mkt_fetch > 10.0) or not cached_markets:
                    for coin, cfg in coin_configs.items():
                        try:
                            active_mkts = []
                            for base_url in (KALSHI_REST_URL, "https://api.elections.kalshi.com/trade-api/v2"):
                                m_resp = await client.get(f"{base_url}/markets?series_ticker={cfg['series']}&status=open")
                                if m_resp.status_code == 200:
                                    mkts = m_resp.json().get("markets", [])
                                    active_mkts = [
                                        m for m in mkts
                                        if (_parse_iso_close_time(m.get("close_time", "")) or 0.0) > now
                                    ]
                                    if active_mkts:
                                        active_mkts.sort(key=lambda m: m.get("close_time") or "")
                                        break
                                if base_url == KALSHI_REST_URL and active_mkts:
                                    break
                            if active_mkts:
                                cached_markets[coin] = active_mkts[0]
                                _mkt_ticker = cached_markets[coin].get("ticker", "")
                                if _mkt_ticker and _mkt_ticker not in _ws_subscribed_tickers:
                                    _ws_subscribed_tickers.add(_mkt_ticker)
                                    if _mkt_ticker not in live_ws.subscribed_tickers:
                                        asyncio.create_task(live_ws.subscribe_ticker(_mkt_ticker))
                                        print(f"[MultiCoinProd] Subscribed WebSocket ticker {_mkt_ticker} ({coin})")
                            else:
                                cached_markets.pop(coin, None)
                                print(f"[MultiCoinProd] No active markets yet for {coin} series {cfg['series']}")
                        except Exception as e:
                            print(f"[MultiCoinProd] Market fetch error for {coin}: {e}")
                    last_mkt_fetch = now

                # 4. Evaluate each coin
                for coin, cfg in coin_configs.items():
                    spot = spot_prices.get(coin, 0.0)
                    if not spot:
                        continue
                    mkt = cached_markets.get(coin)
                    ticker = mkt.get("ticker", f"KX{coin}15M-ROLLOVER") if mkt else f"KX{coin}15M-ROLLOVER"
                    close_time = mkt.get("close_time", "") if mkt else ""
                    close_ts = _parse_iso_close_time(close_time) if close_time else None

                    # Expired-market / rollover guard: keep spot/charts/alpha streaming live
                    # while gating trading execution until the new 15m contract is active.
                    market_is_expired = (mkt is None) or (close_ts is not None and close_ts <= now)
                    if market_is_expired:
                        seconds_left = max(0.0, grid_seconds_left)
                        open_p = candle_opens.get(coin, spot)
                        y_ask, y_bid, n_ask, n_bid = 0.0, 0.0, 0.0, 0.0
                        # Publish live spot & chart tick so dashboard never freezes during rollover
                        _delta_roll = spot - open_p
                        _roll_snap = {
                            "coin": coin,
                            "ticker": ticker,
                            "spot": spot,
                            "hl_spot": round(hl_lead_prices.get(coin, 0.0), 2),
                            "kalshi_implied": None,
                            "kalshi_diff": None,
                            "open": open_p,
                            "delta": round(_delta_roll, 4),
                            "seconds_left": int(seconds_left),
                            "z_cf": 0.0,
                            "z_eff": 0.0,
                            "z_score": 0.0,
                            "z_shift": 0.0,
                            "ofi_z": 0.0,
                            "spot_tfi": round(_active_spot_feed.get_spot_tfi(coin, 30.0), 3) if _active_spot_feed else 0.0,
                            "book_imb": round(_active_spot_feed.get_book_imbalance(coin), 3) if _active_spot_feed else 0.0,
                            "momentum_z": 0.0,
                            "eff_vol_15m": round(float(cfg.get("vol_15m_dollar", 200.0)), 2),
                            "ts": round(now, 2),
                            "prob_yes_pct": 50.0,
                            "prob_no_pct": 50.0,
                            "mu_settle": round(spot, 2),
                            "hypothesis": "NEUTRAL_HOLD",
                            "yes_ask": 0.0,
                            "yes_bid": 0.0,
                            "no_ask": 0.0,
                            "no_bid": 0.0,
                            "spread_cents": 0,
                            "fee_y": 0.0,
                            "fee_n": 0.0,
                            "edge_y": 0.0,
                            "edge_n": 0.0,
                            "action": "ROLLOVER",
                            "action_reason": "Waiting for next 15m Kalshi contract...",
                            "active_position": multicoin_engine.active_positions.get(coin),
                            "equity": multicoin_engine.coin_equities.get(coin, 25.0),
                            "weight": multicoin_engine.coin_weights.get(coin, 0.25),
                        }
                        multicoin_live_matrix[coin] = _roll_snap
                        if coin in _chart_tick_history and spot > 0:
                            _chart_tick_history[coin].append([
                                round(now, 2),
                                round(spot, 2),
                                round(hl_lead_prices.get(coin, 0.0), 2),
                                None,
                            ])
                        continue

                    # Expiry comes from the market's own close_time, not the
                    # wall-clock epoch grid. Fall back to the grid only when
                    # close_time is missing/unparseable; clamp at zero.
                    if close_ts is not None:
                        seconds_left = max(0.0, close_ts - now)
                    else:
                        seconds_left = max(0.0, grid_seconds_left)

                    # Settlement reference: Kalshi's floor_strike is the CF
                    # Benchmarks 60-second arithmetic-mean index at the candle open
                    # (confirmed by rules_primary), so it is authoritative for both
                    # delta and the strike passed to update_position(). Hyperliquid's
                    # candle open is only a proxy and would bias settlement PnL.
                    open_p = _authoritative_open(mkt, candle_opens.get(coin, 0.0), spot)

                    rest_y_ask = float(mkt.get("yes_ask_dollars") or 0.0)
                    rest_y_bid = float(mkt.get("yes_bid_dollars") or 0.0)
                    rest_n_ask = float(mkt.get("no_ask_dollars") or 0.0)
                    rest_n_bid = float(mkt.get("no_bid_dollars") or 0.0)
                    y_ask, y_bid, n_ask, n_bid = rest_y_ask, rest_y_bid, rest_n_ask, rest_n_bid

                    # Use real-time WebSocket L2 top-of-book to avoid 10-second stale REST cache lag
                    # NOTE: REST /markets quotes lag the real order book by several seconds (measured
                    # 0.71 REST vs 0.73->0.77 book). The WS book is the source of truth, so it is NOT
                    # invalidated against REST; only seq gaps / crossed books mark it stale.
                    _l2_book = live_ws.get_book(ticker)
                    if _l2_book is not None:
                        if getattr(_l2_book, "stale", False) and (now - getattr(_l2_book, "last_update_ts", 0.0)) > 3.0:
                            asyncio.create_task(live_ws.refresh_orderbook_rest(ticker, overwrite_fresh=True))
                        if not getattr(_l2_book, "stale", False):
                            ws_bid_c, _ = _l2_book.get_best_bid()
                            ws_ask_c, _ = _l2_book.get_best_ask()
                            if ws_bid_c > 0 and ws_ask_c < 100 and ws_bid_c >= ws_ask_c:
                                _l2_book.stale = True
                                asyncio.create_task(live_ws.refresh_orderbook_rest(ticker, overwrite_fresh=True))
                            elif 0 <= ws_bid_c < ws_ask_c <= 100 and (ws_bid_c > 0 or ws_ask_c < 100):
                                y_bid = ws_bid_c / 100.0
                                y_ask = ws_ask_c / 100.0
                                n_bid = (100 - ws_ask_c) / 100.0
                                n_ask = (100 - ws_bid_c) / 100.0

                    # Record 1-second settlement sample in final 60s window
                    acc = _rti_accumulators.get(coin)
                    if acc is not None:
                        acc.add_sample(spot, seconds_left, candle_id=close_time)

                    # Evaluate active one-click bracket orders against real-time book (Auto TP/SL with OCO)
                    if bracket_manager and bracket_manager.active_tickets:
                        bkt_events_y = bracket_manager.on_tick(ticker=ticker, current_bid=y_bid, current_ask=y_ask, current_time=now)
                        bkt_events_n = bracket_manager.on_tick(ticker=ticker, current_bid=n_bid, current_ask=n_ask, current_time=now)
                        for ev in bkt_events_y + bkt_events_n:
                            multicoin_brain_logs.append({
                                "time": time.strftime("%H:%M:%S", time.localtime(now)),
                                "coin": coin,
                                "type": "BRACKET_EXIT",
                                "text": f"🎯 BRACKET {ev['event']} on {ev['ticker']} | Exit: {int(ev['exit_price']*100)}¢ | Net PnL: ${ev['net_pnl']:+.2f} ({ev['contracts']}x)"
                            })

                    # Dynamic volatility scaling from recent spot ticks blended with base 15m vol
                    # with microstructure noise reduction (3s-5s sub-sampled buckets, avoiding 1s bounce)
                    base_vol = float(cfg.get("vol_15m_dollar", cfg.get("vol_15m", 95.0)))
                    subsample_s = float(cfg.get("vol_subsample_seconds", 3.0))
                    clamp_min_m = float(cfg.get("vol_clamp_min_mult", 0.6))
                    clamp_max_m = float(cfg.get("vol_clamp_max_mult", 2.2))
                    tick_hist = _spot_tick_history.get(coin)
                    eff_vol, realized_15m = compute_microstructure_clean_vol(
                        tick_history=list(tick_hist) if tick_hist else [],
                        base_vol=base_vol,
                        bucket_seconds=subsample_s,
                        clamp_min_mult=clamp_min_m,
                        clamp_max_mult=clamp_max_m,
                    )

                    # Feed freshness and gated lead-lag / OFI drift
                    hl_age_ms = hl_ws.get_age_ms(coin)
                    max_feed_age_ms = float(cfg.get("max_feed_age_ms", 3000.0))
                    is_hl_fresh = hl_ws.is_fresh(coin, max_age_ms=max_feed_age_ms) and hl_age_ms <= max_feed_age_ms
                    ofi_z = hl_ws.get_ofi_z(coin) if is_hl_fresh else 0.0
                    lead_imp_bps = leadlag_impulse_bps.get(coin, 0.0) if is_hl_fresh else 0.0

                    # US Spot TFI, L1 Book Imbalance & Multi-Horizon Spot Momentum
                    spot_tfi = _active_spot_feed.get_spot_tfi(coin, window_s=30.0) if _active_spot_feed else 0.0
                    book_imb = _active_spot_feed.get_book_imbalance(coin) if _active_spot_feed else 0.0
                    momentum_z = 0.0
                    _ch_pts = list(_chart_tick_history.get(coin, ()))
                    if len(_ch_pts) >= 15:
                        _p_short = [pt[1] for pt in _ch_pts if pt[0] >= (now - 15.0) and pt[1] > 0]
                        _p_long = [pt[1] for pt in _ch_pts if pt[0] >= (now - 180.0) and pt[1] > 0]
                        if _p_short and _p_long:
                            _ema_s = sum(_p_short) / len(_p_short)
                            _ema_l = sum(_p_long) / len(_p_long)
                            _vol_3m = max(1e-6, eff_vol * math.sqrt(180.0 / 900.0))
                            momentum_z = max(-1.5, min(1.5, (_ema_s - _ema_l) / _vol_3m))

                    # Empirical 5s Stale-Quote Impulse Detector + 60s Anti-Chase Window
                    _kal_mid_now = (y_bid + y_ask) / 2.0 if (y_bid > 0 and y_ask > 0) else 0.0
                    if "_kalshi_mid_hist" not in locals():
                        _kalshi_mid_hist = {}
                    _km_dq = _kalshi_mid_hist.setdefault(coin, _deque(maxlen=260))
                    if _kal_mid_now > 0:
                        _km_dq.append((now, _kal_mid_now))
                    _d_spot_5s = 0.0
                    for _pts_ts, _pts_px, *_ in _ch_pts:
                        if _pts_ts >= (now - 5.5) and _pts_px > 0:
                            _d_spot_5s = spot - _pts_px
                            break
                    _d_kal_5s_c = 0.0
                    _chase_y_60s_c = 0.0
                    _chase_n_60s_c = 0.0
                    if _kal_mid_now > 0:
                        for _kts, _kmid in _km_dq:
                            if _kts >= (now - 5.5) and _kmid > 0:
                                _d_kal_5s_c = (_kal_mid_now - _kmid) * 100.0
                                break
                        _mids_60s = [_kmid for _kts, _kmid in _km_dq if _kts >= (now - 60.0) and _kmid > 0]
                        if _mids_60s:
                            _chase_y_60s_c = (_kal_mid_now - min(_mids_60s)) * 100.0
                            _chase_n_60s_c = (max(_mids_60s) - _kal_mid_now) * 100.0
                    _spot_imp_thresh = 40.0 if coin == "BTC" else 2.50
                    _stale_info = detect_stale_quote_impulse(
                        spot_5s_delta=_d_spot_5s,
                        kalshi_5s_delta_cents=_d_kal_5s_c,
                        spot_tfi=spot_tfi,
                        spot_thresh=_spot_imp_thresh,
                        max_kalshi_move_cents=1.0,
                    )

                    leadlag_z_clamp = float(cfg.get("leadlag_z_clamp", 0.35))
                    z_shift = compute_clamped_z_shift(
                        lead_imp_bps=lead_imp_bps,
                        ofi_z=ofi_z,
                        is_hl_fresh=is_hl_fresh,
                        max_z_shift=leadlag_z_clamp,
                        spot_tfi=spot_tfi,
                        book_imb=book_imb,
                        momentum_z=momentum_z,
                    )
                    if _stale_info["z_boost"] != 0.0:
                        z_shift = max(-leadlag_z_clamp, min(leadlag_z_clamp, z_shift + _stale_info["z_boost"]))

                    # 60s RTI settlement projection:
                    # When seconds_left > 60: project expected settlement over final 60s window [T-60, T]
                    # using current spot, delta, eff_vol, and gated Hyperliquid lead-lag/OFI drift.
                    # When seconds_left <= 60: blend already-elapsed portion of 60s window from
                    # RtiAccumulator (weighted by (60 - seconds_left) / 60) with remaining expected path
                    # (seconds_left / 60) converging directly to CF Benchmarks 60s settlement window as seconds_left -> 0.
                    sigma_base = base_vol * math.sqrt(max(1.0, seconds_left) / 900.0)
                    sigma_eff = eff_vol * math.sqrt(max(1.0, seconds_left) / 900.0)
                    drift_dollar = z_shift * sigma_eff

                    delta = spot - open_p
                    if acc is not None:
                        mu_settle = acc.calculate_projected_settlement(
                            current_spot=spot,
                            seconds_left=seconds_left,
                            delta=delta,
                            eff_vol=eff_vol,
                            drift=drift_dollar,
                        )
                    else:
                        mu_settle = spot + drift_dollar

                    # Retain z_cf as diagnostic field, compute regime-aware z_eff
                    z_cf = delta / max(0.0001, sigma_base)
                    z_eff = (mu_settle - open_p) / max(0.0001, sigma_eff)
                    z = z_eff

                    prob_yes = normal_cdf(z_eff)
                    prob_no = 1.0 - prob_yes
                    prob_yes_pct = prob_yes * 100.0
                    prob_no_pct = prob_no * 100.0

                    # Calculate net post-fee edges using precomputed 40-contract batch LUT
                    fee_y = lookup_fee_40c(y_ask)
                    fee_n = lookup_fee_40c(n_ask)
                    edge_y = (prob_yes - (y_ask + fee_y)) * 100.0 if y_ask > 0 else -100.0
                    edge_n = (prob_no - (n_ask + fee_n)) * 100.0 if n_ask > 0 else -100.0
                    prob_yes_base = normal_cdf(z_cf)
                    prob_no_base = 1.0 - prob_yes_base
                    base_edge_y = (prob_yes_base - (y_ask + fee_y)) * 100.0 if y_ask > 0 else -100.0
                    base_edge_n = (prob_no_base - (n_ask + fee_n)) * 100.0 if n_ask > 0 else -100.0
                    raw_disloc_y = (prob_yes - y_ask) if y_ask > 0 else -1.0
                    raw_disloc_n = (prob_no - n_ask) if n_ask > 0 else -1.0

                    # Evaluate open positions
                    gate_block_status = ""
                    gate_block_reason = ""
                    if coin in multicoin_engine.active_positions:
                        pos = multicoin_engine.active_positions[coin]
                        pos_expired_rollover = bool(pos.get("ticker") and ticker and pos["ticker"] != ticker)
                        cur_bid = y_bid if pos["side"] == "YES" else n_bid
                        if cur_bid <= 0.01 or pos_expired_rollover:
                            cur_bid = pos.get("current_bid", pos["entry_price"])
                        pos["current_bid"] = cur_bid
                        curr_prob = prob_yes_pct if pos["side"] == "YES" else prob_no_pct
                        dir_z = z_eff if pos["side"] == "YES" else -z_eff
                        eff_pos_sec_left = 0.0 if pos_expired_rollover else seconds_left
                        closed_trade = multicoin_engine.update_position(
                            coin=coin,
                            current_bid=cur_bid,
                            seconds_left=eff_pos_sec_left,
                            settle_spot=spot,
                            strike=open_p,
                            model_prob=curr_prob,
                            dir_z=dir_z,
                        )
                        if closed_trade and pos_expired_rollover:
                            try:
                                await multicoin_engine.reconcile_official_settlements(
                                    rest_base_url=KALSHI_REST_URL,
                                    http_client=client,
                                )
                            except Exception as _reconcile_err:
                                logger.debug(f"[MultiCoinProd] Immediate rollover settlement reconcile: {_reconcile_err}")
                        if closed_trade:
                            if live_bridge.is_live_armed:
                                await live_bridge.on_exit(
                                    coin=coin,
                                    exit_reason=closed_trade["reason"],
                                    current_bid=cur_bid,
                                    paper_trade=closed_trade,
                                )
                            icon = "🎯" if closed_trade["pnl"] >= 0 else "🛑"
                            multicoin_brain_logs.append({
                                "time": time.strftime("%H:%M:%S", time.localtime(now)),
                                "coin": coin,
                                "type": "EXIT",
                                "text": f"{icon} EXITED {closed_trade['coin']} ({closed_trade['reason']}): PnL {closed_trade['pnl']:+.2f} ({closed_trade['roi']:+.1f}%) | Equity: ${closed_trade['equity']:.2f}"
                            })

                    # Evaluate new entry (S5_REC_LEADLAG_ENSEMBLE rules with quote staleness, spread & depth guards)
                    elif kill_file_active:
                        gate_block_status = "HALT_KILL_SWITCH"
                        gate_block_reason = "Emergency KILL switch active — new entries paused"
                    else:
                        signal = ""
                        edge_pct = 0.0
                        prob_val = 50.0

                        spread_y = (y_ask - y_bid) if (y_ask > 0 and y_bid > 0) else 1.0
                        spread_n = (n_ask - n_bid) if (n_ask > 0 and n_bid > 0) else 1.0
                        max_allowed_spread = float(cfg.get("max_spread_dollars", 0.05))  # Max 5¢ spread per S5
                        max_rel_spread = 0.35      # Max 35% relative spread

                        max_quote_age_s = float(cfg.get("max_quote_age_seconds", 3.0))
                        ws_live_seq = bool(getattr(live_ws, "is_ws_connected", False)) and not getattr(_l2_book, "stale", False)
                        eff_max_quote_age_s = max(max_quote_age_s, 30.0) if ws_live_seq else max_quote_age_s
                        is_quote_stale = (
                            (_l2_book is None)
                            or getattr(_l2_book, "stale", False)
                            or (getattr(_l2_book, "last_update_ts", 0.0) <= 0.0)
                            or ((now - getattr(_l2_book, "last_update_ts", 0.0)) > eff_max_quote_age_s)
                        )

                        min_book_depth = int(cfg.get("min_book_depth", 10))
                        if _l2_book is not None:
                            _ba_p, _ = _l2_book.get_best_ask()
                            _bb_p, _ = _l2_book.get_best_bid()
                            depth_y = sum(q for p, q in _l2_book.asks.items() if p <= _ba_p + 2) if _l2_book.asks else 0
                            depth_n = sum(q for p, q in _l2_book.bids.items() if p >= _bb_p - 2) if _l2_book.bids else 0
                        else:
                            depth_y = 0
                            depth_n = 0
                        has_depth_y = depth_y >= min_book_depth
                        has_depth_n = depth_n >= min_book_depth

                        spread_ok_y = (y_bid >= 0.05) and (spread_y <= max_allowed_spread) and ((spread_y / max(0.01, y_ask)) <= max_rel_spread)
                        spread_ok_n = (n_bid >= 0.05) and (spread_n <= max_allowed_spread) and ((spread_n / max(0.01, n_ask)) <= max_rel_spread)

                        is_liquid_y = (not is_quote_stale) and spread_ok_y and has_depth_y
                        is_liquid_n = (not is_quote_stale) and spread_ok_n and has_depth_n

                        # ---- Adaptive strategy selection per coin BEFORE evaluating gates ----
                        _coin_selector = _strategy_selectors.get(coin)
                        if _coin_selector is not None:
                            _cfg, _regime = _coin_selector.select_from_ticks(
                                ts=now,
                                spot=spot,
                                strike=open_p,
                                yes_bid=y_bid,
                                yes_ask=y_ask,
                                yes_bid_sz=float(depth_n),
                                yes_ask_sz=float(depth_y),
                                signed_flow_usd=0.0,
                            )
                            multicoin_engine.apply_strategy_config(_cfg)

                        # Calculate 10-second and 20-second CF spot velocity
                        hist = _spot_tick_history.get(coin, _deque())
                        velocity_20s = 0.0
                        vel_10s_bps = 0.0
                        if len(hist) >= 3:
                            cutoff_20 = now - 20.0
                            oldest_20 = next((p for t, p in hist if t >= cutoff_20), hist[0][1])
                            velocity_20s = spot - oldest_20
                            cutoff_10 = now - 10.0
                            oldest_10 = next((p for t, p in hist if t >= cutoff_10), hist[0][1])
                            vel_10s_bps = ((spot - oldest_10) / max(1e-6, spot)) * 10000.0

                        max_entry_cap = float(getattr(multicoin_engine, "max_entry_price", cfg.get("max_entry_price", 0.72)))
                        min_entry_cap = float(getattr(multicoin_engine, "min_entry_price", cfg.get("min_entry_price", 0.20)))
                        min_sec_left = int(getattr(multicoin_engine, "min_seconds_left_by_coin", {}).get(coin, cfg.get("min_seconds_left_for_entry", 90)))
                        max_sec_left = int(getattr(multicoin_engine, "max_seconds_left", cfg.get("max_seconds_left_for_entry", 660)))
                        time_gate_ok = (min_sec_left <= seconds_left <= max_sec_left)
                        min_prob_threshold = float(getattr(multicoin_engine, "min_prob_by_coin", {}).get(coin, cfg.get("min_model_prob", 60.0)))
                        min_edge_threshold = float(getattr(multicoin_engine, "min_edge_pct", cfg.get("min_edge_pct", 3.0)))

                        if time_gate_ok and prob_yes_pct >= min_prob_threshold and edge_y >= min_edge_threshold and min_entry_cap <= y_ask <= max_entry_cap:
                            if is_quote_stale:
                                gate_block_status = "HOLD_STALE_QUOTE"
                                gate_block_reason = f"Stale quote on {coin}: L2 book age > {eff_max_quote_age_s:.1f}s or marked stale"
                            elif not spread_ok_y:
                                gate_block_status = "HOLD_WIDE_SPREAD"
                                gate_block_reason = f"Illiquid book: bid {int(y_bid*100)}¢ / ask {int(y_ask*100)}¢ (spread {int(spread_y*100)}¢ > {int(max_allowed_spread*100)}¢)"
                            elif not has_depth_y:
                                gate_block_status = "HOLD_THIN_BOOK"
                                gate_block_reason = f"Thin book: top-of-book depth {depth_y} < {min_book_depth} contracts"
                            elif vel_10s_bps < 0.0:
                                gate_block_status = "HOLD_ADVERSE_MOMENTUM"
                                gate_block_reason = f"10s CF velocity negative ({vel_10s_bps:+.2f} bps < 0)"
                            elif lead_imp_bps < -0.50:
                                gate_block_status = "HOLD_ADVERSE_LEADLAG"
                                gate_block_reason = f"Adverse de-biased lead-lag impulse ({lead_imp_bps:+.2f} bps < -0.50)"
                            elif ofi_z <= -2.5:
                                gate_block_status = "HOLD_ADVERSE_OFI"
                                gate_block_reason = f"Extreme counter-OFI on {coin}: Z_OFI={ofi_z:+.2f} <= -2.50"
                            elif base_edge_y < 2.5:
                                gate_block_status = "HOLD_LOW_BASE_EDGE"
                                gate_block_reason = f"Un-inflated spot vs strike edge too small ({base_edge_y:+.1f}% < +2.5%)"
                            elif _chase_y_60s_c > 16.0:
                                gate_block_status = "HOLD_CHASE_GUARD"
                                gate_block_reason = f"Anti-chase: YES already jumped +{_chase_y_60s_c:.0f}¢ in last 60s (> 16¢)"
                            else:
                                signal = "BUY YES"
                                edge_pct = edge_y
                                prob_val = prob_yes_pct
                        elif time_gate_ok and prob_no_pct >= min_prob_threshold and edge_n >= min_edge_threshold and min_entry_cap <= n_ask <= max_entry_cap:
                            if is_quote_stale:
                                gate_block_status = "HOLD_STALE_QUOTE"
                                gate_block_reason = f"Stale quote on {coin}: L2 book age > {eff_max_quote_age_s:.1f}s or marked stale"
                            elif not spread_ok_n:
                                gate_block_status = "HOLD_WIDE_SPREAD"
                                gate_block_reason = f"Illiquid book: bid {int(n_bid*100)}¢ / ask {int(n_ask*100)}¢ (spread {int(spread_n*100)}¢ > {int(max_allowed_spread*100)}¢)"
                            elif not has_depth_n:
                                gate_block_status = "HOLD_THIN_BOOK"
                                gate_block_reason = f"Thin book: top-of-book depth {depth_n} < {min_book_depth} contracts"
                            elif (-vel_10s_bps) < 0.0:
                                gate_block_status = "HOLD_ADVERSE_MOMENTUM"
                                gate_block_reason = f"10s CF velocity positive against NO ({vel_10s_bps:+.2f} bps > 0)"
                            elif (-lead_imp_bps) < -0.50:
                                gate_block_status = "HOLD_ADVERSE_LEADLAG"
                                gate_block_reason = f"Adverse de-biased lead-lag impulse ({lead_imp_bps:+.2f} bps > +0.50)"
                            elif ofi_z >= 2.5:
                                gate_block_status = "HOLD_ADVERSE_OFI"
                                gate_block_reason = f"Extreme counter-OFI on {coin}: Z_OFI={ofi_z:+.2f} >= +2.50"
                            elif base_edge_n < 2.5:
                                gate_block_status = "HOLD_LOW_BASE_EDGE"
                                gate_block_reason = f"Un-inflated spot vs strike edge too small ({base_edge_n:+.1f}% < +2.5%)"
                            elif _chase_n_60s_c > 16.0:
                                gate_block_status = "HOLD_CHASE_GUARD"
                                gate_block_reason = f"Anti-chase: NO already jumped +{_chase_n_60s_c:.0f}¢ in last 60s (> 16¢)"
                            else:
                                signal = "BUY NO"
                                edge_pct = edge_n
                                prob_val = prob_no_pct

                        is_certainty_arb = False
                        if not signal and acc is not None and 5 < seconds_left <= 25 and acc.sample_count() >= 35:
                            cert_res = acc.evaluate_certainty_arb(
                                strike=open_p,
                                current_spot=spot,
                                seconds_left=seconds_left,
                                dollar_vol_15m=eff_vol,
                            )
                            if cert_res.get("is_certainty_arb") and cert_res.get("side"):
                                c_side = cert_res["side"]
                                c_ask = y_ask if c_side == "YES" else n_ask
                                c_fee = lookup_fee_40c(c_ask)
                                c_edge = (cert_res["prob"] - (c_ask + c_fee)) * 100.0 if c_ask > 0 else -100.0
                                c_liquid = is_liquid_y if c_side == "YES" else is_liquid_n
                                # Market sanity guard: never buy <55¢ claiming 100% certainty when book strongly disagrees
                                if c_liquid and 0.55 <= c_ask <= 0.78 and c_edge >= 4.0:
                                    signal = f"BUY {c_side}"
                                    edge_pct = c_edge
                                    prob_val = cert_res["prob"] * 100.0
                                    is_certainty_arb = True

                        if signal:
                            trade_pos = multicoin_engine.evaluate_entry(
                                coin=coin,
                                ticker=ticker,
                                series=cfg["series"],
                                signal=signal,
                                edge_pct=edge_pct,
                                model_prob=prob_val,
                                yes_ask=y_ask,
                                no_ask=n_ask,
                                seconds_left=seconds_left,
                                delta=delta,
                                candle_id=close_time,
                                is_certainty_arb=is_certainty_arb
                            )
                            if trade_pos:
                                # Contract-size ramp: count clean entries and scale up
                                _contract_ramp["trades_entered"] += 1
                                _update_contract_ramp()
                                if live_bridge.is_live_armed:
                                    await live_bridge.on_entry(
                                        coin=coin,
                                        ticker=ticker,
                                        side=trade_pos["side"],
                                        entry_price=trade_pos["entry_price"],
                                        model_prob_pct=prob_val,
                                        is_certainty_arb=is_certainty_arb,
                                        http_client=client,
                                    )
                                print(f"[MultiCoinProd] TRADE ENTERED: {coin} {trade_pos.get('side')} {trade_pos.get('contracts')}x @ {trade_pos.get('entry_price')} on {ticker}")
                                multicoin_brain_logs.append({
                                    "time": time.strftime("%H:%M:%S", time.localtime(now)),
                                    "coin": coin,
                                    "type": "ENTRY",
                                    "text": f"🚀 ENTERED {trade_pos.get('side')} ({trade_pos.get('contracts')}x @ {int(trade_pos.get('entry_price',0)*100)}¢) on {ticker} | Edge: +{edge_pct:.1f}% | Prob: {prob_val:.1f}%"
                                })
                            else:
                                gate_block_status = "HOLD_ENGINE_GATE"
                                gate_block_reason = getattr(multicoin_engine, "last_reject_reason", "") or "Rejected by engine risk/L2 gate"

                    # Update live cockpit matrix for frontend
                    pos = multicoin_engine.active_positions.get(coin)
                    action_status = "HOLD"
                    action_reason = ""
                    min_sec_entry = int(getattr(multicoin_engine, "min_seconds_left_by_coin", {}).get(coin, cfg.get("min_seconds_left_for_entry", 90)))
                    max_sec_entry = int(getattr(multicoin_engine, "max_seconds_left", cfg.get("max_seconds_left_for_entry", 660)))
                    max_entry_price = float(getattr(multicoin_engine, "max_entry_price", cfg.get("max_entry_price", 0.72)))
                    eff_min_prob_ui = float(getattr(multicoin_engine, "min_prob_by_coin", {}).get(coin, cfg.get("min_model_prob", 60.0)))
                    eff_min_edge_ui = float(getattr(multicoin_engine, "min_edge_pct", cfg.get("min_edge_pct", 3.0)))

                    if pos:
                        cur_bid = y_bid if pos["side"] == "YES" else n_bid
                        roi_unrealized = ((cur_bid - pos["entry_price"]) / max(0.01, pos["entry_price"])) * 100.0
                        action_status = f"IN_POSITION_{pos['side']}"
                        action_reason = f"Holding {pos['contracts']}x {pos['side']} @ {int(pos['entry_price']*100)}¢ (Now: {int(cur_bid*100)}¢ | ROI: {roi_unrealized:+.1f}%)"
                    elif gate_block_status:
                        action_status = gate_block_status
                        action_reason = gate_block_reason
                    elif seconds_left < min_sec_entry:
                        action_status = "HOLD_EXPIRE"
                        action_reason = f"Expiration gate active ({int(seconds_left)}s < {min_sec_entry}s cutoff)"
                    elif seconds_left > max_sec_entry:
                        action_status = "HOLD_EARLY"
                        action_reason = f"Early candle noise filter ({int(seconds_left)}s > {max_sec_entry}s)"
                    elif multicoin_engine.candle_stopped_out.get(coin) == close_time:
                        action_status = "HOLD_STOPPED_OUT"
                        action_reason = "Candle lockout: stopped out in this 15m candle"
                    else:
                        best_p = max(prob_yes_pct, prob_no_pct)
                        best_e = max(edge_y, edge_n)
                        best_ask = y_ask if prob_yes_pct >= prob_no_pct else n_ask
                        if best_p < eff_min_prob_ui:
                            action_reason = f"Low Conviction ({best_p:.1f}% < {eff_min_prob_ui:.0f}%)"
                        elif best_ask > max_entry_price:
                            action_reason = f"Price above {int(round(max_entry_price*100))}¢ cap ({int(best_ask*100)}¢ > {int(round(max_entry_price*100))}¢)"
                        elif best_ask < 0.20:
                            action_reason = f"Price below 20¢ floor ({int(best_ask*100)}¢ < 20¢)"
                        else:
                            action_reason = f"Insufficient Edge ({best_e:+.1f}% < +{eff_min_edge_ui:.1f}%)"

                    # Signal hysteresis to prevent 1-second boundary flickering
                    hypothesis = _signal_hysteresis_mgr.get_hypothesis(
                        coin=coin,
                        prob_yes_pct=prob_yes_pct,
                        prob_no_pct=prob_no_pct,
                        edge_y=edge_y,
                        edge_n=edge_n,
                    )

                    # Kalshi Orderbook Implied Spot Price (K + sigma_eff * Phi^-1(P_mid))
                    kalshi_mid_y = (y_bid + y_ask) / 2.0 if (y_bid > 0 and y_ask > 0) else (y_ask if y_ask > 0 else 0.0)
                    kalshi_implied = None
                    kalshi_diff = None
                    if 0.02 <= kalshi_mid_y <= 0.98 and sigma_eff > 0 and open_p > 0:
                        kalshi_implied = round(open_p + inverse_normal_cdf(kalshi_mid_y) * sigma_eff, 2)
                        kalshi_diff = round(kalshi_implied - spot, 2)

                    # Atomic, immutable snapshot per coin published each cycle
                    coin_snapshot = {
                        "coin": coin,
                        "ticker": ticker,
                        "spot": spot,
                        "hl_spot": round(hl_lead_prices.get(coin, 0.0), 2),
                        "kalshi_implied": kalshi_implied,
                        "kalshi_diff": kalshi_diff,
                        "open": open_p,
                        "delta": round(delta, 4),
                        "seconds_left": int(seconds_left),
                        "z_cf": round(z_cf, 2),
                        "z_eff": round(z_eff, 2),
                        "z_score": round(z_eff, 2),
                        "z_shift": round(z_shift, 4),
                        "ofi_z": round(ofi_z, 2),
                        "spot_tfi": round(spot_tfi, 3),
                        "book_imb": round(book_imb, 3),
                        "momentum_z": round(momentum_z, 3),
                        "stale_impulse": _stale_info["stale_impulse"],
                        "d_spot_5s": _stale_info["d_spot_5s"],
                        "d_kalshi_5s_cents": _stale_info["d_kalshi_5s_cents"],
                        "stale_tfi_confirmed": _stale_info["tfi_confirmed"],
                        "eff_vol_15m": round(eff_vol, 2),
                        "ts": round(now, 2),
                        "prob_yes_pct": round(prob_yes_pct, 1),
                        "prob_no_pct": round(prob_no_pct, 1),
                        "mu_settle": round(mu_settle, 2),
                        "hypothesis": hypothesis,
                        "yes_ask": y_ask,
                        "yes_bid": y_bid,
                        "no_ask": n_ask,
                        "no_bid": n_bid,
                        "spread_cents": int(round((y_ask - y_bid) * 100)) if (y_ask and y_bid) else 0,
                        "fee_y": round(fee_y, 4),
                        "fee_n": round(fee_n, 4),
                        "edge_y": round(edge_y, 1),
                        "edge_n": round(edge_n, 1),
                        "action": action_status,
                        "action_reason": action_reason,
                        "active_position": pos,
                        "equity": multicoin_engine.coin_equities.get(coin, 25.0),
                        "weight": multicoin_engine.coin_weights.get(coin, 0.25)
                    }
                    multicoin_live_matrix[coin] = dict(coin_snapshot)
                    if coin in _chart_tick_history and spot > 0:
                        _chart_tick_history[coin].append([
                            round(now, 2),
                            round(spot, 2),
                            round(hl_lead_prices.get(coin, 0.0), 2),
                            kalshi_implied,
                        ])

                # Periodic brain scan log
                if now - last_heartbeat > 15.0:
                    last_heartbeat = now
                    try:
                        await multicoin_engine.reconcile_official_settlements(
                            rest_base_url=KALSHI_REST_URL,
                            http_client=client,
                        )
                    except Exception as _settle_err:
                        print(f"[MultiCoinProd] Official settlement reconcile error: {_settle_err}")
                    # Log the same authoritative reference/expiry the evaluator uses.
                    _ref_deltas = {
                        c: spot_prices.get(c, 0.0) - _authoritative_open(
                            cached_markets.get(c), candle_opens.get(c, 0.0), spot_prices.get(c, 0.0)
                        )
                        for c in coin_configs
                    }
                    _hb_seconds_left = grid_seconds_left
                    for _c in coin_configs:
                        _ts = _parse_iso_close_time((cached_markets.get(_c) or {}).get("close_time"))
                        if _ts is not None:
                            _hb_seconds_left = max(0.0, _ts - now)
                            break
                    summary = " | ".join([f"{c}: d={_ref_deltas.get(c, 0.0):+.2f}" for c in coin_configs])
                    mkts_str = ", ".join([f"{c}:{cached_markets.get(c, {}).get('ticker', 'none')}" for c in coin_configs])
                    print(f"[MultiCoinProd] Heartbeat | {summary} | Markets: [{mkts_str}] | Active: {list(multicoin_engine.active_positions.keys())}")
                    deltas_txt = " | ".join([f"{c}: {_ref_deltas.get(c, 0.0):+.2f}" for c in coin_configs])
                    multicoin_brain_logs.append({
                        "time": time.strftime("%H:%M:%S", time.localtime(now)),
                        "type": "SCAN",
                        "text": f"Scanned {len(coin_configs)} coins ({int(_hb_seconds_left)}s left) | {deltas_txt} | Equity: ${multicoin_engine.total_equity:.2f}"
                    })
                    if len(multicoin_brain_logs) > 30:
                        multicoin_brain_logs.pop(0)

            except Exception as e:
                import traceback
                print(f"[MultiCoinProd] Exception: {e}\n{traceback.format_exc()}")

            await asyncio.sleep(0.25)

@asynccontextmanager
async def lifespan(app):
    global startup_reconciliation_report, live_demo_balance, _shared_http_client, spot_feed
    # Phase 3: Cold-Boot State Reconciliation with Kalshi (prod or demo)
    try:
        startup_reconciliation_report = await live_oms.reconcile_state_on_startup(
            db_path_or_db=live_db,
            cancel_resting_orders=False  # SAFE DEFAULT: reconcile reports orphans fail-closed; only kill switches cancel
        )
        print(f"[Lifespan] Cold-boot exchange state reconciliation complete: {startup_reconciliation_report.get('status')}")
    except Exception as e:
        print(f"[Lifespan] Reconciliation notice: {e}")

    # Start low-latency spot feed (Coinbase/Kraken/Hyperliquid WS)
    try:
        spot_feed = get_spot_feed(hl_feed=hl_ws)
        await spot_feed.start()
        print(f"[Lifespan] Spot feed started: {spot_feed.get_all_spots()}")
    except Exception as e:
        print(f"[Lifespan] Spot feed start error: {e}")

    # Initialize shared HTTP connection pool
    _shared_http_client = get_http_client()
    print("[Lifespan] Shared HTTP connection pool initialized")

    # Fetch live balance
    try:
        if live_auth.is_configured:
            headers = live_auth.sign_request("GET", "/trade-api/v2/portfolio/balance")
            resp = await _shared_http_client.get(f"{KALSHI_REST_URL}/portfolio/balance", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                live_demo_balance["balance"] = data.get("balance", 5000)
                live_demo_balance["balance_dollars"] = float(data.get("balance", 5000)) / 100.0
                print(f"[Lifespan] Confirmed Live Balance: ${live_demo_balance['balance_dollars']:.2f}")
    except Exception as e:
        print(f"[Lifespan] Balance fetch error: {e}")

    # Initial bootstrap (reuse shared client)
    try:
        await metrics_provider.update_subsecond_spot(_shared_http_client)
        await metrics_provider.update_candles_and_technicals(_shared_http_client)
        await metrics_provider.update_derivatives_context(_shared_http_client)
        await scanner.scan_all_btc_markets(_shared_http_client)
    except Exception as e:
        print(f"[Lifespan] Bootstrap error: {e}")

    # Initialize Async SQLAlchemy 2.0 models
    try:
        await init_async_models()
        print("[Lifespan] Async SQLAlchemy 2.0 models initialized.")
    except Exception as e:
        print(f"[Lifespan] Async DB notice: {e}")

    # Real-time WebSocket broadcast loop (streams at 4 Hz to active UI clients)
    async def ws_tick_broadcast_loop():
        last_ws_hash = None
        while True:
            try:
                await asyncio.sleep(0.25)
                if ws_manager.connection_count > 0:
                    payload = assemble_tick_payload(force=True)
                    h = _payload_hash(payload)
                    if h != last_ws_hash:
                        last_ws_hash = h
                        await ws_manager.broadcast_json({
                            "type": "tick_stream",
                            "timestamp": time.time(),
                            "data": payload
                        })
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"[WS Broadcast] Broadcast notice: {e}")

    # Automated 24h Data Partitioning & WAL Maintenance loop
    async def periodic_partition_archive_loop():
        from core.partition_manager import PartitionArchiveManager
        archive_mgr = PartitionArchiveManager()
        while True:
            try:
                # Initial wait 300s after boot, then every 6 hours
                await asyncio.sleep(300.0)
                res = archive_mgr.archive_all_databases(
                    db_paths=["kalshi_audit.db", "multi_coin_paper.db", "kalshi_live_demo.db"],
                    cutoff_hours=24,
                    prune_hot=True
                )
                total_archived = sum(
                    tbl_info.get("rows_archived", 0)
                    for db_info in res.values()
                    for tbl_info in db_info.values()
                    if isinstance(tbl_info, dict)
                )
                if total_archived > 0:
                    print(f"[PartitionManager] Automated 24h archival completed: {total_archived} cold rows exported to Parquet.")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[PartitionManager] Automated partition notice: {e}")
            await asyncio.sleep(21300.0)

    if os.environ.get("TESTING") == "1":
        yield
        return

    task_ws = asyncio.create_task(live_ws.connect_and_listen())
    task_hl_ws = asyncio.create_task(hl_ws.connect_and_listen())
    task_spot_feed = asyncio.create_task(spot_feed.start()) if spot_feed else None
    task1 = asyncio.create_task(fast_spot_loop())
    task2 = asyncio.create_task(full_scan_loop())
    task3 = asyncio.create_task(technicals_loop())
    task_multicoin = asyncio.create_task(multi_coin_production_loop())
    task_reconcile = asyncio.create_task(periodic_reconciliation_loop())
    task_paper_reconcile = asyncio.create_task(paper_reconcile_loop())
    task_killwatch = asyncio.create_task(kill_switch_watcher())
    task_ws_broadcast = asyncio.create_task(ws_tick_broadcast_loop())
    task_partition = asyncio.create_task(periodic_partition_archive_loop())
    yield
    # ==================== GRACEFUL SHUTDOWN ====================
    try:
        hl_ws.stop()
    except Exception:
        pass
    try:
        if spot_feed:
            await spot_feed.stop()
    except Exception:
        pass
    try:
        await close_http_client()
    except Exception:
        pass
    print("[Lifespan] Shutdown initiated: cancelling resting orders (positions left open)...")
    # Cancel all resting orders via OMS, trigger order group for emergency safety.
    # SAFETY GATE (agreed plan, condition 1): NEVER issue real exchange cancels while
    # dry_run is on — live order management is armed only via KALSHI_LIVE_TRADING=1.
    try:
        if live_oms.dry_run:
            print("[Lifespan] Shutdown: dry_run is ON — skipping real exchange cancels (no KALSHI_LIVE_TRADING=1). Paper positions simply close out locally.")
        else:
            group_id = getattr(multicoin_engine, "order_group_id", None)
            cancel_headers = live_auth.sign_request("DELETE", "/trade-api/v2/portfolio/events/orders")
            await _shared_http_client.delete(f"{KALSHI_REST_URL}/portfolio/events/orders", headers=cancel_headers, params={"exchange_index": 2})
            print("[Lifespan] Batch cancel-all sent to Kalshi.")
            if group_id:
                trigger_headers = live_auth.sign_request("PUT", f"/trade-api/v2/portfolio/order_groups/{group_id}/trigger")
                await _shared_http_client.put(f"{KALSHI_REST_URL}/portfolio/order_groups/{group_id}/trigger", headers=trigger_headers)
                print(f"[Lifespan] Order group {group_id} TRIGGERED (blocks new orders until manual reset).")
    except Exception as e:
        print(f"[Lifespan] Shutdown cancel error: {e}")
    live_ws.stop()
    for t in (task_ws, task_hl_ws, task_spot_feed, task1, task2, task3, task_multicoin, task_reconcile, task_paper_reconcile, task_killwatch, task_ws_broadcast):
        if t:
            t.cancel()
        try:
            await asyncio.wait_for(t, timeout=2.0)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

def assemble_portfolio_payload() -> Dict[str, Any]:
    """
    Unified portfolio view across all paper engines (single source of truth for reporting).
    Primary engine = MultiCoinPaperEngine; legacy single-coin bot is read-only history.
    Merges trade ledgers from both SQLite DBs, tagged by source.
    """
    mc = multicoin_engine.get_status_dict()
    legacy = paper_bot.get_status_dict()
    trades: List[Dict[str, Any]] = []
    try:
        for t in multicoin_engine.db.get_recent_trades(limit=200):
            t = dict(t)
            t["source"] = "multicoin"
            trades.append(t)
    except Exception as e:
        logger.warning(f"[Portfolio] multicoin ledger error: {e}")
    try:
        for t in db.get_recent_trades(limit=200):
            t = dict(t)
            t["source"] = "legacy_paper"
            trades.append(t)
    except Exception as e:
        logger.warning(f"[Portfolio] legacy ledger error: {e}")
    trades.sort(key=lambda t: t.get("timestamp") or 0, reverse=True)

    legacy_equity = float(legacy.get("total_equity", 0.0) or 0.0)
    legacy_pnl = float(legacy.get("realized_pnl", 0.0) or 0.0)
    # Live OMS safety surface (agreed plan, condition 3): orphan orders fail closed and are visible here
    live_safety = {
        "dry_run": live_oms.dry_run,
        "armed_for_live_trading": (not live_oms.dry_run),
        "blocked_by_orphans": live_oms.blocked_by_orphans,
        "untracked_resting_orders": list(live_oms.untracked_resting_orders),
        "last_reconcile": {
            "status": live_oms.last_reconcile_report.get("status"),
            "untracked_resting_orders": live_oms.last_reconcile_report.get("untracked_resting_orders", []),
            "discrepancies": len(live_oms.last_reconcile_report.get("discrepancies_found", [])),
        },
    }
    # Sync portfolio manager state
    try:
        portfolio_manager.total_equity = float(mc.get("total_equity", 100.0) or 100.0)
        portfolio_manager.total_cash = float(mc.get("total_cash", 100.0) or 100.0)
        for coin, sub_eq in multicoin_engine.coin_equities.items():
            portfolio_manager.update_asset_equity(coin, sub_eq)
    except Exception:
        pass

    return {
        "timestamp": time.time(),
        "primary_engine": {"name": "multicoin", "trading_enabled": True, "status": mc},
        "legacy_engine": {"name": "paper_bot_btc_only", "trading_enabled": LEGACY_PAPER_BOT_TRADING, "status": legacy},
        "live_oms_safety": live_safety,
        "portfolio_risk": load_portfolio_state(),
        "risk_dashboard": portfolio_manager.get_risk_dashboard(),
        "combined": {
            "equity": round(float(mc["total_equity"]) + legacy_equity, 2),
            "pnl": round(float(mc["total_pnl"]) + legacy_pnl, 2),
            "total_trades": len(trades),
        },
        "recent_trades": trades[:50],
    }

def assemble_full_payload() -> Dict[str, Any]:
    """Combine live metrics, scanner results, bot state, and live Kalshi OMS into a master payload."""
    scan_data = scanner.cache
    metrics_data = metrics_provider.get_summary_dict()
    bot_data = paper_bot.get_status_dict()

    active_mkt = scan_data.get("kxbtc15m", {})
    ticker = active_mkt.get("ticker", "")
    book = live_ws.get_or_create_book(ticker) if ticker else None
    sweep_10 = book.calculate_sweep_vwap("YES", 10) if book and book.asks else None
    sweep_25 = book.calculate_sweep_vwap("YES", 25) if book and book.asks else None

    # Fetch live demo trades
    recent_live = live_db.get_recent_trades(limit=20)

    live_kalshi_payload = {
        "mode": "LIVE_DEMO" if live_auth.is_configured else live_execution_mode,
        "configured": live_auth.is_configured,
        "key_id": (live_auth.key_id[:8] + "...") if live_auth.key_id else "NONE",
        "rest_url": KALSHI_REST_URL,
        "ws_url": KALSHI_WS_URL,
        "ws_connected": live_ws.is_ws_connected,
        "balance_dollars": live_demo_balance.get("balance_dollars", 52.54),
        "active_ticker": ticker,
        "l2_depth": {
            "best_bid": book.get_best_bid()[0] if book else 0,
            "best_bid_qty": book.get_best_bid()[1] if book else 0,
            "best_ask": book.get_best_ask()[0] if book else 100,
            "best_ask_qty": book.get_best_ask()[1] if book else 0,
            "bids": [[p, q] for p, q in sorted(book.bids.items(), reverse=True)[:6]] if book else [],
            "asks": [[p, q] for p, q in sorted(book.asks.items())[:6]] if book else [],
            "sweep_vwap_10": sweep_10.get("vwap_cents") if sweep_10 else None,
            "sweep_vwap_25": sweep_25.get("vwap_cents") if sweep_25 else None,
            "can_fill_10": sweep_10.get("can_fill") if sweep_10 else False
        },
        "watchdog": watchdog.get_status(),
        "reconciliation": startup_reconciliation_report,
        "recent_trades": recent_live
    }

    return {
        "timestamp": int(time.time()),
        "metrics": metrics_data,
        "kxbtc15m": scan_data.get("kxbtc15m", {}),
        "top_ev_opportunities": scan_data.get("top_ev_opportunities", [])[:25],
        "strike_ladders_by_expiry": scan_data.get("strike_ladders_by_expiry", {}),
        "range_brackets": scan_data.get("range_brackets", []),
        "barrier_targets": scan_data.get("barrier_targets", []),
        "arbitrage_alerts": scan_data.get("arbitrage_alerts", [])[:15],
        "stats": scan_data.get("stats", {}),
        "bot": bot_data,
        "portfolio": assemble_portfolio_payload(),
        "multicoin": multicoin_engine.get_status_dict(),
        "multicoin_matrix": multicoin_live_matrix,
        "active_brackets": bracket_manager.get_active_tickets() if bracket_manager else [],
        "bracket_history": bracket_manager.get_history_tickets(limit=15) if bracket_manager else [],
        "multicoin_brain_logs": (multicoin_brain_logs[-20:] if multicoin_brain_logs else []),
        "live_kalshi": live_kalshi_payload
    }

def assemble_tick_payload(force: bool = False) -> Optional[Dict[str, Any]]:
    """
    Lightweight 1-second tick payload (~5KB) preventing DOM thrashing and client memory churn.
    Returns None when nothing changed since last call (change detection via hash).
    Set force=True to bypass cache (used on first SSE frame).
    """
    scan_data = scanner.cache
    metrics_data = metrics_provider.get_summary_dict()
    bot_data = paper_bot.get_status_dict()

    active_mkt = scan_data.get("kxbtc15m", {})
    ticker = active_mkt.get("ticker", "")
    book = live_ws.get_or_create_book(ticker) if ticker else None
    sweep_10 = book.calculate_sweep_vwap("YES", 10) if book and book.asks else None
    sweep_25 = book.calculate_sweep_vwap("YES", 25) if book and book.asks else None

    recent_live = _trades_cache_get()

    live_kalshi_payload = {
        "mode": "LIVE_DEMO" if live_auth.is_configured else live_execution_mode,
        "configured": live_auth.is_configured,
        "key_id": (live_auth.key_id[:8] + "...") if live_auth.key_id else "NONE",
        "rest_url": KALSHI_REST_URL,
        "ws_url": KALSHI_WS_URL,
        "ws_connected": live_ws.is_ws_connected,
        "balance_dollars": live_demo_balance.get("balance_dollars", 52.54),
        "active_ticker": ticker,
        "l2_depth": {
            "best_bid": book.get_best_bid()[0] if book else 0,
            "best_bid_qty": book.get_best_bid()[1] if book else 0,
            "best_ask": book.get_best_ask()[0] if book else 100,
            "best_ask_qty": book.get_best_ask()[1] if book else 0,
            "bids": [[p, q] for p, q in sorted(book.bids.items(), reverse=True)[:6]] if book else [],
            "asks": [[p, q] for p, q in sorted(book.asks.items())[:6]] if book else [],
            "sweep_vwap_10": sweep_10.get("vwap_cents") if sweep_10 else None,
            "sweep_vwap_25": sweep_25.get("vwap_cents") if sweep_25 else None,
            "can_fill_10": sweep_10.get("can_fill") if sweep_10 else False
        },
        "watchdog": watchdog.get_status(),
        "reconciliation": startup_reconciliation_report,
        "recent_trades": recent_live
    }

    # Overlay instantaneous Kalshi WS L2 top-of-book on each 4 Hz tick
    live_matrix_view = {}
    for coin_k, snap in multicoin_live_matrix.items():
        snap_copy = dict(snap)
        c_ticker = snap_copy.get("ticker", "")
        c_book = live_ws.get_book(c_ticker) if (live_ws and c_ticker) else None
        if c_book and not getattr(c_book, "stale", False):
            wb, _ = c_book.get_best_bid()
            wa, _ = c_book.get_best_ask()
            if 0 <= wb < wa <= 100 and (wb > 0 or wa < 100):
                snap_copy["yes_bid"] = wb / 100.0
                snap_copy["yes_ask"] = wa / 100.0
                snap_copy["no_bid"] = (100 - wa) / 100.0
                snap_copy["no_ask"] = (100 - wb) / 100.0
                snap_copy["spread_cents"] = int(wa - wb)
        live_matrix_view[coin_k] = snap_copy

    payload = {
        "timestamp": int(time.time()),
        "metrics": metrics_data,
        "kxbtc15m": scan_data.get("kxbtc15m", {}),
        "top_ev_opportunities": scan_data.get("top_ev_opportunities", [])[:5],
        "arbitrage_alerts": scan_data.get("arbitrage_alerts", [])[:5],
        "stats": scan_data.get("stats", {}),
        "bot": bot_data,
        "portfolio": assemble_portfolio_payload(),
        "multicoin": multicoin_engine.get_status_dict(),
        "multicoin_matrix": live_matrix_view,
        "active_brackets": bracket_manager.get_active_tickets() if bracket_manager else [],
        "bracket_history": bracket_manager.get_history_tickets(limit=15) if bracket_manager else [],
        "multicoin_brain_logs": (multicoin_brain_logs[-20:] if multicoin_brain_logs else []),
        "live_kalshi": live_kalshi_payload
    }

    if not force:
        h = _payload_hash(payload)
        if h == _tick_payload_hash:
            return None  # nothing changed
        _tick_payload_hash = h
        _tick_payload_cache.clear()
        _tick_payload_cache.update(payload)

    return payload

async def get_live_scan(request):
    """Return full instantaneous state as JSON."""
    return JSONResponse(assemble_full_payload())

async def get_chart_history(request):
    """Return rolling 5-minute tick history [ts, spot, hl_spot] for BTC and ETH."""
    cutoff = time.time() - 305.0
    out = {}
    for coin, dq in _chart_tick_history.items():
        out[coin] = [pt for pt in list(dq) if pt[0] >= cutoff]
    return JSONResponse(out)

async def live_stream_sse(request):
    """
    Server-Sent Events (SSE) streaming endpoint.
    Streams live ticks when data changes. Sends a lightweight data heartbeat
    every 5s when idle so browsers don't drop the connection.
    The heavy election/market opportunity data is excluded from the SSE stream
    (available via /api/scan) to keep the initial payload small.
    """
    async def event_generator():
        try:
            # First frame: lightweight initial snapshot (no heavy opportunity data)
            full = assemble_full_payload()
            # Strip heavy arrays from initial SSE payload
            full["top_ev_opportunities"] = []
            full["arbitrage_alerts"] = []
            yield f"data: {json.dumps(full)}\n\n"
            last_sse_hash = _payload_hash(full)

            idle_count = 0
            while True:
                if await request.is_disconnected():
                    break
                payload = assemble_tick_payload(force=True)
                h = _payload_hash(payload)
                if h != last_sse_hash:
                    last_sse_hash = h
                    yield f"data: {json.dumps(payload)}\n\n"
                    idle_count = 0
                else:
                    idle_count += 1
                    if idle_count >= 20:  # Send heartbeat every ~2s if idle
                        heartbeat = {
                            "timestamp": int(time.time()),
                            "heartbeat": True,
                            "ws_connected": live_ws.is_ws_connected if live_ws else False,
                        }
                        yield f"data: {json.dumps(heartbeat)}\n\n"
                        idle_count = 0
                await asyncio.sleep(0.1)
        except (asyncio.CancelledError, GeneratorExit, Exception):
            pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )

async def post_simulate_monte_carlo(request):
    """Interactive Monte Carlo simulation endpoint."""
    try:
        body = await request.json()
        spot = float(body.get("spot") or metrics_provider.spot_price)
        strike = float(body.get("strike") or (spot + 500.0))
        hours = float(body.get("hours") or 2.0)
        vol_pct = float(body.get("vol_pct") or (metrics_provider.vol_24h_ann * 100.0))
        vol = max(0.05, vol_pct / 100.0)
        num_paths = min(20000, max(1000, int(body.get("num_paths") or 10000)))
        time_years = hours / (365.25 * 24.0)

        mc_res = prediction_engine.run_monte_carlo_jump_diffusion(
            spot=spot,
            time_to_expiry_years=time_years,
            volatility=vol,
            num_paths=num_paths,
            strikes=[strike]
        )

        bs_prob = prediction_engine.black_scholes_binary(
            spot=spot,
            strike=strike,
            time_to_expiry_years=time_years,
            volatility=vol,
            strike_type="greater"
        )

        mc_res["bs_probability"] = round(bs_prob, 4)
        mc_res["bs_probability_pct"] = round(bs_prob * 100.0, 1)
        mc_res["target_strike"] = strike
        mc_res["target_strike_prob"] = mc_res["strike_probs"].get(strike, round(bs_prob, 4))
        mc_res["target_strike_prob_pct"] = round(mc_res["target_strike_prob"] * 100.0, 1)

        return JSONResponse(mc_res)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

async def trigger_rescan(request):
    """Force an immediate re-scan."""
    async with httpx.AsyncClient() as client:
        await scanner.scan_all_btc_markets(client)
    return JSONResponse({"status": "success", "total_scanned": scanner.cache.get("total_markets_scanned", 0)})

# Bot Management API Endpoints
async def get_bot_status(request):
    """Get complete paper trading bot status."""
    return JSONResponse(paper_bot.get_status_dict())

async def post_bot_settings(request):
    """Update paper trading bot configuration parameters."""
    try:
        body = await request.json()
        if "enabled" in body:
            paper_bot.enabled = bool(body["enabled"])
        if "take_profit_roi_pct" in body:
            paper_bot.take_profit_roi_pct = float(body["take_profit_roi_pct"])
        if "stop_loss_roi_pct" in body:
            paper_bot.stop_loss_roi_pct = float(body["stop_loss_roi_pct"])
        if "max_allocation_pct" in body:
            paper_bot.max_allocation_pct = float(body["max_allocation_pct"])
        if "min_edge_pct" in body:
            paper_bot.min_edge_pct = float(body["min_edge_pct"])
        return JSONResponse({"status": "success", "settings": paper_bot.get_status_dict()["settings"]})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

async def post_bot_close_position(request):
    """Emergency manual close of active open position."""
    res = paper_bot.manual_close_position(reason="MANUAL_EXIT")
    return JSONResponse(res)

async def post_bot_reset(request):
    """Reset paper trading account balance back to $50.00."""
    paper_bot.reset_account(50.0)
    return JSONResponse({"status": "success", "message": "Account reset to $50.00"})

async def export_audit_csv(request):
    """Export full CSV of all logged trades for historical auditing."""
    csv_content = db.export_trades_csv()
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=kalshi_trades_audit.csv"}
    )

async def get_audit_history(request):
    """Get portfolio equity history curve and metrics history."""
    return JSONResponse({
        "portfolio_history": db.get_portfolio_history(limit=100),
        "recent_trades": db.get_recent_trades(limit=50)
    })

async def serve_dashboard(request):
    """Serve main live HTML dashboard."""
    template_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    if os.path.exists(template_path):
        with open(template_path, "r", encoding="utf-8") as f:
            content = f.read()
        return HTMLResponse(content)
    return HTMLResponse("<h1>Loading Bitcoin Kalshi Engine...</h1>")

async def serve_terminal(request):
    """Serve advanced pro trading terminal HTML interface for BTC & ETH."""
    template_path = os.path.join(os.path.dirname(__file__), "templates", "terminal.html")
    if os.path.exists(template_path):
        with open(template_path, "r", encoding="utf-8") as f:
            content = f.read()
        return HTMLResponse(content)
    return HTMLResponse("<h1>Pro Trading Terminal loading...</h1>")

async def get_safety_status(request):
    """Get Stale-Tick Watchdog and Startup Reconciliation telemetry."""
    return JSONResponse({
        "watchdog": watchdog.get_status(),
        "reconciliation": startup_reconciliation_report
    })

# ------------------------------------------------------------------------------
# Live Kalshi Demo Execution & Telemetry Endpoints
# ------------------------------------------------------------------------------

async def get_live_status(request):
    """Return live Kalshi Demo account and execution telemetry."""
    active_mkt = scanner.cache.get("kxbtc15m", {})
    ticker = active_mkt.get("ticker", "")
    book = live_ws.get_or_create_book(ticker) if ticker else None
    sweep_10 = book.calculate_sweep_vwap("YES", 10) if book and book.asks else None
    sweep_25 = book.calculate_sweep_vwap("YES", 25) if book and book.asks else None

    return JSONResponse({
        "mode": live_execution_mode,
        "configured": live_auth.is_configured,
        "key_id": (live_auth.key_id[:8] + "...") if live_auth.key_id else "NONE",
        "rest_url": KALSHI_REST_URL,
        "ws_url": KALSHI_WS_URL,
        "ws_connected": live_ws.is_ws_connected,
        "balance_dollars": live_demo_balance.get("balance_dollars", 52.54),
        "active_ticker": ticker,
        "l2_depth": {
            "best_bid": book.get_best_bid()[0] if book else 0,
            "best_bid_qty": book.get_best_bid()[1] if book else 0,
            "best_ask": book.get_best_ask()[0] if book else 100,
            "best_ask_qty": book.get_best_ask()[1] if book else 0,
            "bids": [[p, q] for p, q in sorted(book.bids.items(), reverse=True)[:8]] if book else [],
            "asks": [[p, q] for p, q in sorted(book.asks.items())[:8]] if book else [],
            "sweep_vwap_10": sweep_10.get("vwap_cents") if sweep_10 else None,
            "sweep_vwap_25": sweep_25.get("vwap_cents") if sweep_25 else None,
            "can_fill_10": sweep_10.get("can_fill") if sweep_10 else False
        },
        "watchdog": watchdog.get_status(),
        "reconciliation": startup_reconciliation_report
    })

async def post_live_mode(request):
    """Toggle between PAPER and LIVE_DEMO mode."""
    global live_execution_mode
    try:
        body = await request.json()
        target_mode = body.get("mode", "PAPER").upper()
        if target_mode in ("PAPER", "LIVE_DEMO"):
            live_execution_mode = target_mode
        return JSONResponse({"status": "success", "mode": live_execution_mode})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)

async def post_live_reconcile(request):
    """Trigger manual on-demand cold-boot reconciliation."""
    global startup_reconciliation_report
    report = await live_oms.reconcile_state_on_startup(db_path_or_db=live_db, cancel_resting_orders=False)
    startup_reconciliation_report = report
    return JSONResponse(report)

async def post_live_refresh_balance(request):
    """Query fresh demo balance from Kalshi REST API."""
    global live_demo_balance
    async with httpx.AsyncClient(timeout=4.0) as client:
        try:
            if live_auth.is_configured:
                headers = live_auth.sign_request("GET", "/trade-api/v2/portfolio/balance")
                resp = await client.get(f"{KALSHI_REST_URL}/portfolio/balance", headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    live_demo_balance["balance"] = data.get("balance", 5000)
                    live_demo_balance["balance_dollars"] = float(data.get("balance", 5000)) / 100.0
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse(live_demo_balance)

async def post_live_cancel_all(request):
    """Cancel all open resting orders on Kalshi exchange."""
    orders = await live_oms.reconciler.fetch_resting_orders()
    canceled_ids = []
    for ord_item in orders:
        oid = ord_item.get("order_id")
        if oid:
            await live_oms.reconciler.cancel_order(oid)
            canceled_ids.append(oid)
    return JSONResponse({"status": "success", "canceled_orders": canceled_ids})

# ------------------------------------------------------------------------------
# One-Click Manual Trading Execution Layer (Auto TP / SL with OCO)
# ------------------------------------------------------------------------------
async def post_trade_bracket(request):
    """
    One-click manual trading execution endpoint with precomputed auto TP and SL.
    Hit Buy -> automatically computes & attaches TP/SL with strict bounds validation,
    partial fill scaling, and OCO exit management.
    """
    try:
        body = await request.json()
        ticker = body.get("ticker")
        if not ticker:
            return JSONResponse({"status": "error", "message": "ticker is required"}, status_code=400)

        side = body.get("side", "YES").upper()
        coin = body.get("coin", "BTC").upper()
        contracts = int(body.get("contracts", 10))
        entry_price = body.get("entry_price")

        # Resolve entry price from active book if not specified
        if entry_price is None or float(entry_price) <= 0:
            book = live_ws.get_book(ticker) if (live_ws and ticker) else None
            if book:
                ask_c, _ = book.get_best_ask()
                if side == "YES":
                    entry_price = ask_c / 100.0 if ask_c > 0 else 0.35
                else:
                    entry_price = (100 - ask_c) / 100.0 if ask_c > 0 else 0.65
            else:
                entry_price = 0.35

        tp_pct = body.get("tp_pct")
        sl_pct = body.get("sl_pct")
        tp_ticks = body.get("tp_ticks")
        sl_ticks = body.get("sl_ticks")
        tp_bps = body.get("tp_bps")
        sl_bps = body.get("sl_bps")
        tp_price = body.get("tp_price")
        sl_price = body.get("sl_price")
        dry_run = body.get("dry_run", bracket_manager.dry_run)

        ticket = calculate_bracket_ticket(
            ticker=ticker,
            side=side,
            entry_price=float(entry_price),
            contracts=contracts,
            coin=coin,
            tp_pct=float(tp_pct) if tp_pct is not None else None,
            sl_pct=float(sl_pct) if sl_pct is not None else None,
            tp_ticks=int(tp_ticks) if tp_ticks is not None else None,
            sl_ticks=int(sl_ticks) if sl_ticks is not None else None,
            tp_bps=float(tp_bps) if tp_bps is not None else None,
            sl_bps=float(sl_bps) if sl_bps is not None else None,
            tp_price=float(tp_price) if tp_price is not None else None,
            sl_price=float(sl_price) if sl_price is not None else None,
            dry_run=bool(dry_run),
        )

        res_ticket = await bracket_manager.execute_one_click_buy(ticket)
        multicoin_brain_logs.append({
            "time": time.strftime("%H:%M:%S", time.localtime()),
            "coin": coin,
            "type": "ONE_CLICK_BUY",
            "text": f"⚡ ONE-CLICK BUY: {ticket.requested_contracts}x {ticket.side} @ {ticket.entry_cents}¢ on {ticket.ticker} | Status: {res_ticket.status} | TP: {res_ticket.tp_cents}¢ | SL: {res_ticket.sl_cents}¢"
        })

        status_code = 200 if res_ticket.status in ("ACTIVE", "SUBMITTED") else (400 if res_ticket.status == "REJECTED" else 200)
        return JSONResponse({
            "status": "success" if res_ticket.status in ("ACTIVE", "SUBMITTED") else res_ticket.status.lower(),
            "ticket": res_ticket.to_dict()
        }, status_code=status_code)
    except Exception as e:
        logger.error(f"[post_trade_bracket] Error: {e}", exc_info=True)
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

async def get_trade_brackets(request):
    """Retrieve active and recent bracket order tickets."""
    return JSONResponse({
        "active": bracket_manager.get_active_tickets() if bracket_manager else [],
        "history": bracket_manager.get_history_tickets(limit=50) if bracket_manager else []
    })

async def post_trade_bracket_cancel(request):
    """Cancel or manually flatten active bracket order(s)."""
    try:
        body = await request.json()
        ticket_id = body.get("ticket_id")
        cancel_all = body.get("all", False)
        if cancel_all:
            res = bracket_manager.cancel_all_brackets()
            return JSONResponse({"status": "success", "cancelled_count": len(res)})
        elif ticket_id:
            res = bracket_manager.cancel_or_flatten_ticket(ticket_id)
            if res:
                return JSONResponse({"status": "success", "ticket": res.to_dict()})
            return JSONResponse({"status": "not_found", "message": f"Ticket {ticket_id} not active"}, status_code=404)
        return JSONResponse({"status": "error", "message": "ticket_id or all=true required"}, status_code=400)
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

async def get_trade_ticket_preview(request):
    """Calculate and preview precomputed bracket ticket without executing."""
    params = request.query_params
    ticker = params.get("ticker", "KXBTC15M-PREVIEW")
    side = params.get("side", "YES").upper()
    coin = params.get("coin", "BTC").upper()
    entry_price = float(params.get("entry_price", 0.35))
    contracts = int(params.get("contracts", 10))
    tp_pct = float(params.get("tp_pct", 25.0))
    sl_pct = float(params.get("sl_pct", 25.0))

    ticket = calculate_bracket_ticket(
        ticker=ticker,
        side=side,
        entry_price=entry_price,
        contracts=contracts,
        coin=coin,
        tp_pct=tp_pct,
        sl_pct=sl_pct,
    )
    is_valid, reason = validate_bracket_ticket(ticket)
    d = ticket.to_dict()
    d["is_valid"] = is_valid
    d["validation_reason"] = reason
    return JSONResponse(d)

def _is_browser_navigation(request) -> bool:
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        if "application/json" not in accept:
            return True
        return accept.find("text/html") < accept.find("application/json")
    return False

async def get_multicoin_status(request):
    """Return multi-coin paper trading engine status."""
    if _is_browser_navigation(request):
        return RedirectResponse(url="/", status_code=303)
    return JSONResponse(multicoin_engine.get_status_dict())

async def get_multicoin_trades(request):
    """Return recent multi-coin trades from SQLite."""
    return JSONResponse(multicoin_engine.db.get_recent_trades(limit=50))

async def get_portfolio(request):
    """Unified portfolio view: primary multi-coin engine + legacy read-only bot, merged ledgers."""
    return JSONResponse(assemble_portfolio_payload())

async def get_risk_dashboard(request):
    """
    Unified Institutional Risk Dashboard:
    - Global Caps (40% portfolio, 20% single-asset, max 2 concurrent, 10% daily stop loss)
    - Real-time Drawdown per Asset (BTC, ETH, SOL, XRP)
    - R-Multiple Trade Tracking, Win Rate, and Expectancy
    - Systematic Hedging Recommendations
    """
    if _is_browser_navigation(request):
        return RedirectResponse(url="/", status_code=303)
    try:
        portfolio_manager.total_equity = float(multicoin_engine.total_equity)
        portfolio_manager.total_cash = float(multicoin_engine.total_cash)
        for coin, sub_eq in multicoin_engine.coin_equities.items():
            portfolio_manager.update_asset_equity(coin, sub_eq)
    except Exception:
        pass
    return JSONResponse(portfolio_manager.get_risk_dashboard())

async def get_audit_block_summary(request):
    """
    Returns exact block-by-block audit synchronization:
    - Current Kalshi Demo balance ($157.72)
    - Single-Coin Paper Bot Total Equity
    - Multi-Coin Paper Portfolio Equity
    - All trades executed within the current 15-minute block (last 900 seconds)
    """
    now = time.time()
    block_window = 900.0

    live_trades_15m = []
    try:
        with get_db_pool("kalshi_live_demo.db").connection() as conn:
            c = conn.cursor()
            c.execute("SELECT id, timestamp, ticker, side, entry_price, exit_price, exit_reason, pnl_dollars, contracts FROM trades WHERE timestamp >= ? ORDER BY id DESC", (now - block_window,))
            live_trades_15m = [dict(r) for r in c.fetchall()]
    except Exception:
        pass

    paper_trades_15m = []
    try:
        with get_db_pool("kalshi_audit.db").connection() as conn:
            c = conn.cursor()
            c.execute("SELECT id, timestamp, ticker, side, entry_price, exit_price, exit_reason, pnl_dollars, contracts FROM trades WHERE timestamp >= ? ORDER BY id DESC", (now - block_window,))
            paper_trades_15m = [dict(r) for r in c.fetchall()]
    except Exception:
        pass

    multicoin_trades_15m = []
    try:
        with get_db_pool("multi_coin_paper.db").connection() as conn:
            c = conn.cursor()
            c.execute("SELECT id, timestamp, coin, ticker, side, entry_price, exit_price, exit_reason, pnl_dollars, contracts FROM trades WHERE timestamp >= ? ORDER BY id DESC", (now - block_window,))
            multicoin_trades_15m = [dict(r) for r in c.fetchall()]
    except Exception:
        pass

    active_mkt = scanner.cache.get("kxbtc15m", {})
    ticker = active_mkt.get("ticker", "")
    close_time = active_mkt.get("close_time", "")

    return JSONResponse({
        "timestamp": now,
        "demo_balance_dollars": live_demo_balance.get("balance_dollars", 157.72),
        "paper_equity_dollars": paper_bot.get_equity(),
        "multicoin_equity_dollars": multicoin_engine.total_equity,
        "active_ticker": ticker,
        "candle_close_time": close_time,
        "trades_in_block": {
            "live_count": len(live_trades_15m),
            "paper_count": len(paper_trades_15m),
            "multicoin_count": len(multicoin_trades_15m),
            "total_count": len(live_trades_15m) + len(paper_trades_15m) + len(multicoin_trades_15m),
            "live_trades": live_trades_15m,
            "paper_trades": paper_trades_15m,
            "multicoin_trades": multicoin_trades_15m
        }
    })

# --- Kill Switch & Deployment Safety Handlers ---
async def get_kill_switch_status(request):
    """Return real-time kill-switch status, daily drawdown, and lock state."""
    return JSONResponse(kill_switch.get_status())

async def post_kill_switch_trip(request):
    """Manually trip emergency kill-switch with optional reason."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = body.get("reason", "Manual trigger via API /api/kill-switch/trip")
    await kill_switch.trip_async(reason)
    return JSONResponse({"status": "tripped", "reason": reason, "timestamp": time.time()})

async def post_kill_switch_reset(request):
    """Reset emergency kill-switch and disarm circuit breaker."""
    ok, msg = kill_switch.reset()
    return JSONResponse({"success": ok, "message": msg, "timestamp": time.time()})

async def get_blue_green_summary(request):
    """Return summary comparison metrics between Blue (Paper) and Green (Live) execution."""
    return JSONResponse(blue_green_comparator.get_summary())

async def get_blue_green_audit(request):
    """Return recent Blue vs Green fill comparison audit logs."""
    limit = int(request.query_params.get("limit", 50))
    return JSONResponse({"records": blue_green_comparator.get_recent_comparisons(limit=limit)})

async def post_rollback_to_paper(request):
    """Manually or programmatically trigger emergency rollback of live bot to paper mode."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    reason = body.get("reason", "Manual rollback requested via /api/rollback")
    res = await live_oms.trigger_rollback_to_paper(reason)
    return JSONResponse(res)

async def post_partition_archive(request):
    """Trigger on-demand 24h cold data export to Parquet and WAL checkpointing."""
    from core.partition_manager import PartitionArchiveManager
    mgr = PartitionArchiveManager()
    res = mgr.archive_all_databases(
        db_paths=["kalshi_audit.db", "multi_coin_paper.db", "kalshi_live_demo.db"],
        cutoff_hours=24,
        prune_hot=True
    )
    return JSONResponse({"status": "success", "archived": res})

async def health_check(request):
    """Docker healthcheck endpoint."""
    return JSONResponse({"status": "healthy", "service": "kalshi-bot"})


routes = [
    Route("/health", health_check, methods=["GET"]),
    Route("/", serve_dashboard, methods=["GET"]),
    Route("/dashboard", serve_dashboard, methods=["GET"]),
    Route("/cockpit", serve_dashboard, methods=["GET"]),
    Route("/terminal", serve_terminal, methods=["GET"]),
    Route("/pro", serve_terminal, methods=["GET"]),
    Route("/api/scan", get_live_scan, methods=["GET"]),
    Route("/api/chart/history", get_chart_history, methods=["GET"]),
    Route("/api/stream", live_stream_sse, methods=["GET"]),
    Route("/api/simulate", post_simulate_monte_carlo, methods=["POST"]),
    Route("/api/trigger", trigger_rescan, methods=["POST"]),
    
    # Bot & Audit routes
    Route("/api/bot/status", get_bot_status, methods=["GET"]),
    Route("/api/bot/settings", post_bot_settings, methods=["POST"]),
    Route("/api/bot/close_position", post_bot_close_position, methods=["POST"]),
    Route("/api/bot/reset", post_bot_reset, methods=["POST"]),
    Route("/api/audit/export", export_audit_csv, methods=["GET"]),
    Route("/api/audit/history", get_audit_history, methods=["GET"]),
    Route("/api/safety/status", get_safety_status, methods=["GET"]),

    # Live Kalshi Demo OMS routes
    Route("/api/live/status", get_live_status, methods=["GET"]),
    Route("/api/live/mode", post_live_mode, methods=["POST"]),
    Route("/api/live/reconcile", post_live_reconcile, methods=["POST"]),
    Route("/api/live/refresh_balance", post_live_refresh_balance, methods=["POST"]),
    Route("/api/live/cancel_all", post_live_cancel_all, methods=["POST"]),

    # One-Click Manual Trading Execution Layer (Auto TP / SL)
    Route("/api/trade/bracket", post_trade_bracket, methods=["POST"]),
    Route("/api/trade/brackets", get_trade_brackets, methods=["GET"]),
    Route("/api/trade/bracket/cancel", post_trade_bracket_cancel, methods=["POST"]),
    Route("/api/trade/ticket/preview", get_trade_ticket_preview, methods=["GET"]),

    # Multi-Coin Portfolio & Block Audit Synchronization routes
    Route("/api/multicoin/status", get_multicoin_status, methods=["GET"]),
    Route("/api/multicoin/trades", get_multicoin_trades, methods=["GET"]),
    Route("/api/portfolio", get_portfolio, methods=["GET"]),
    Route("/api/risk/dashboard", get_risk_dashboard, methods=["GET"]),
    Route("/api/audit/block_summary", get_audit_block_summary, methods=["GET"]),

    # Deployment Safety, Kill-Switch & Blue/Green Routes
    Route("/api/kill-switch/status", get_kill_switch_status, methods=["GET"]),
    Route("/api/kill-switch/trip", post_kill_switch_trip, methods=["POST"]),
    Route("/api/kill-switch/reset", post_kill_switch_reset, methods=["POST"]),
    Route("/api/blue-green/summary", get_blue_green_summary, methods=["GET"]),
    Route("/api/blue-green/audit", get_blue_green_audit, methods=["GET"]),
    Route("/api/rollback", post_rollback_to_paper, methods=["POST"]),
    Route("/api/partition/archive", post_partition_archive, methods=["POST"]),
]

app = FastAPI(
    title="Kalshi Multi-Coin Quantitative Engine",
    description="Production-grade Drift-Diffusion Trading & Risk Platform for Kalshi Crypto Binaries",
    version="2.0.0",
    routes=routes,
    lifespan=lifespan,
    middleware=[
        Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    ]
)

@app.websocket("/ws/ticks")
@app.websocket("/ws/stream")
async def websocket_tick_stream(websocket: WebSocket):
    """
    Real-time WebSocket tick streaming endpoint for subsecond pricing,
    L2 order book changes, position updates, and risk metrics.
    """
    await ws_manager.connect(websocket)
    try:
        # Send immediate initial lightweight tick snapshot (~6KB)
        snapshot = assemble_tick_payload(force=True)
        await websocket.send_json({"type": "snapshot", "timestamp": time.time(), "data": snapshot})
        while True:
            msg = await websocket.receive_text()
            if msg == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket)
    except Exception as e:
        logger.debug(f"[WS Endpoint] Disconnected: {e}")
        await ws_manager.disconnect(websocket)

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8088, log_level="info")
