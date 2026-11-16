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
from core.prediction_engine import QuantPredictionEngine
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
scanner = KalshiBitcoinScanner(metrics_provider, prediction_engine)
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

multicoin_engine = MultiCoinPaperEngine(
    initial_balance=100.0,
    db_path="multi_coin_paper.db",
    pinned_weights=None,  # Enable dynamic performance-based rebalancing (BTC/ETH 20%-80% bounds)
    disabled_coins={"SOL", "XRP"},
    ws_client=live_ws  # Pass WebSocket client for L2 book access
)
multicoin_engine.max_entry_price = 0.52
multicoin_engine.max_seconds_left = 500
multicoin_engine.min_contracts_per_trade = 15
multicoin_engine.max_contracts_per_trade = 120
multicoin_engine.stop_loss_hard_roi_pct = 35.0
multicoin_engine.maker_take_profit = True
multicoin_engine.enable_dynamic_compounding = True
multicoin_engine.max_hold_seconds = 120.0

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

# Initialize DI singletons and WebSocket streaming manager
set_portfolio_manager_singleton(portfolio_manager)
set_multicoin_engine_singleton(multicoin_engine)
ws_manager = WebSocketConnectionManager()
multicoin_live_matrix: Dict[str, Dict[str, Any]] = {}
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

async def fast_spot_loop():
    """High-frequency sub-second loop: updates live spot, refreshes KXBTC15M quotes, and evaluates paper trading bot."""
    print("Starting high-frequency spot & paper bot loop...")
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await metrics_provider.update_subsecond_spot(client)
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
            await asyncio.sleep(1.0)

async def full_scan_loop():
    """Medium-frequency loop (every 15s): scans all 600+ Kalshi BTC contracts, recalculates models, and syncs Demo balance."""
    print("Starting full Kalshi BTC market scan loop...")
    async with httpx.AsyncClient() as client:
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
    async with httpx.AsyncClient() as client:
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
    async with httpx.AsyncClient(timeout=5.0) as client:
        while True:
            await asyncio.sleep(30.0)
            try:
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
    _spot_tick_history: Dict[str, _deque] = {c: _deque(maxlen=45) for c in ("BTC", "ETH", "SOL", "XRP")}
    _rti_accumulators: Dict[str, RtiAccumulator] = {
        "BTC": RtiAccumulator(index_name="BRTI", window_seconds=60),
        "ETH": RtiAccumulator(index_name="ETHUSD_RTI", window_seconds=60),
        "SOL": RtiAccumulator(index_name="SOLUSD_RTI", window_seconds=60),
        "XRP": RtiAccumulator(index_name="XRPUSD_RTI", window_seconds=60),
    }

    async with httpx.AsyncClient(timeout=4.0) as client:
        while True:
            try:
                # KILL file check - halt all new trades if file exists
                if os.path.exists("KILL"):
                    print("[MultiCoinProd] KILL file detected, sleeping...")
                    await asyncio.sleep(5.0)
                    continue

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

                # 1. Refresh spot prices from persistent Hyperliquid WebSocket (REST fallback only if stale)
                spot_prices = {}
                stale_coins = []
                for coin in coin_configs:
                    ws_spot = hl_ws.get_spot(coin)
                    if ws_spot is not None and hl_ws.is_fresh(coin, max_age=5.0):
                        spot_prices[coin] = ws_spot
                        _spot_tick_history.setdefault(coin, _deque(maxlen=45)).append((now, ws_spot))
                    else:
                        stale_coins.append(coin)

                if stale_coins:
                    hl_resp = await client.post("https://api.hyperliquid.xyz/info", json={"type": "allMids"})
                    if hl_resp.status_code == 200:
                        mids = hl_resp.json()
                        for coin in stale_coins:
                            val = mids.get(coin)
                            if val is not None:
                                val_f = float(val)
                                spot_prices[coin] = val_f
                                _spot_tick_history.setdefault(coin, _deque(maxlen=45)).append((now, val_f))
                    else:
                        print(f"[MultiCoinProd] Hyperliquid allMids fallback returned {hl_resp.status_code}")

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

                # 3. Refresh active production markets every 10s
                if now - last_mkt_fetch > 10.0 or not cached_markets:
                    for coin, cfg in coin_configs.items():
                        try:
                            m_resp = await client.get(f"{KALSHI_REST_URL}/markets?series_ticker={cfg['series']}&status=open")
                            if m_resp.status_code == 200:
                                mkts = m_resp.json().get("markets", [])
                                if mkts:
                                    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                                    active = [m for m in mkts if (m.get("close_time") or "") >= now_iso]
                                    cached_markets[coin] = active[0] if active else mkts[0]
                                    _mkt_ticker = cached_markets[coin].get("ticker", "")
                                    print(f"[MultiCoinProd] Found market for {coin}: {_mkt_ticker or '?'}")
                                    # Subscribe every enabled coin's ticker (L2/quote
                                    # streaming), not just BTC. Track locally so a ticker
                                    # is subscribed once even if the cache refreshes.
                                    if _mkt_ticker and _mkt_ticker not in _ws_subscribed_tickers:
                                        _ws_subscribed_tickers.add(_mkt_ticker)
                                        if _mkt_ticker not in live_ws.subscribed_tickers:
                                            asyncio.create_task(live_ws.subscribe_ticker(_mkt_ticker))
                                            print(f"[MultiCoinProd] Subscribed WebSocket ticker {_mkt_ticker} ({coin})")
                                else:
                                    print(f"[MultiCoinProd] No markets for {coin} series {cfg['series']}")
                            else:
                                print(f"[MultiCoinProd] Market fetch for {coin} returned {m_resp.status_code}")
                        except Exception as e:
                            print(f"[MultiCoinProd] Market fetch error for {coin}: {e}")
                    last_mkt_fetch = now

                # 4. Evaluate each coin
                for coin, cfg in coin_configs.items():
                    spot = spot_prices.get(coin, 0.0)
                    mkt = cached_markets.get(coin)
                    if not spot or not mkt:
                        continue

                    ticker = mkt.get("ticker", "")
                    close_time = mkt.get("close_time", "")
                    close_ts = _parse_iso_close_time(close_time)

                    # Expired-market guard: markets are cached for 10s and can go
                    # stale at the candle boundary. Never evaluate entries or exits
                    # against an already-expired market.
                    if close_ts is not None and close_ts <= now:
                        print(
                            f"[MultiCoinProd] Skipping {coin}: market expired "
                            f"({ticker} close_time {close_time} <= now)"
                        )
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

                    y_ask = float(mkt.get("yes_ask_dollars") or 0.0)
                    y_bid = float(mkt.get("yes_bid_dollars") or 0.0)
                    n_ask = float(mkt.get("no_ask_dollars") or 0.0)
                    n_bid = float(mkt.get("no_bid_dollars") or 0.0)

                    # Record 1-second settlement sample in final 60s window
                    acc = _rti_accumulators.get(coin)
                    if acc is not None:
                        acc.add_sample(spot, seconds_left, candle_id=close_time)

                    # Dynamic volatility scaling from recent spot ticks blended with base 15m vol
                    base_vol = float(cfg["vol_15m"])
                    eff_vol = base_vol
                    tick_hist = _spot_tick_history.get(coin)
                    if tick_hist and len(tick_hist) >= 12:
                        diffs = [
                            abs(tick_hist[i][1] - tick_hist[i - 1][1]) / math.sqrt(max(0.2, tick_hist[i][0] - tick_hist[i - 1][0]))
                            for i in range(1, len(tick_hist))
                        ]
                        if diffs:
                            realized_1s = sum(diffs) / len(diffs)
                            realized_15m = realized_1s * 30.0
                            eff_vol = max(base_vol * 0.6, min(base_vol * 2.2, 0.5 * base_vol + 0.5 * realized_15m))

                    # Model probability (with bounded 5-level OFI drift bias from Hyperliquid WS)
                    delta = spot - open_p
                    sigma = eff_vol * math.sqrt(seconds_left / 900.0)
                    z = delta / max(0.0001, sigma)
                    ofi_z = hl_ws.get_ofi_z(coin) if hl_ws.is_fresh(coin, max_age=5.0) else 0.0
                    ofi_bias = max(-0.35, min(0.35, 0.12 * ofi_z))
                    prob_yes = normal_cdf(z + ofi_bias)
                    prob_no = 1.0 - prob_yes
                    prob_yes_pct = prob_yes * 100.0
                    prob_no_pct = prob_no * 100.0

                    # Calculate net post-fee edges using realistic 40-contract batch (avoids 1-contract ceil() distortion)
                    fee_y = (calculate_kalshi_taker_fee(40, y_ask) / 40.0) if 0.0 < y_ask < 1.0 else 0.0
                    fee_n = (calculate_kalshi_taker_fee(40, n_ask) / 40.0) if 0.0 < n_ask < 1.0 else 0.0
                    edge_y = (prob_yes - (y_ask + fee_y)) * 100.0 if y_ask > 0 else -100.0
                    edge_n = (prob_no - (n_ask + fee_n)) * 100.0 if n_ask > 0 else -100.0

                    # Evaluate open positions
                    if coin in multicoin_engine.active_positions:
                        pos = multicoin_engine.active_positions[coin]
                        cur_bid = y_bid if pos["side"] == "YES" else n_bid
                        if cur_bid <= 0.01:
                            cur_bid = pos.get("current_bid", pos["entry_price"])
                        # Persist the freshly computed bid for the dashboard's live
                        # ROI display. Engine PnL/exit math below is unchanged.
                        pos["current_bid"] = cur_bid
                        curr_prob = prob_yes_pct if pos["side"] == "YES" else prob_no_pct
                        closed_trade = multicoin_engine.update_position(
                            coin=coin,
                            current_bid=cur_bid,
                            seconds_left=seconds_left,
                            settle_spot=spot,
                            strike=open_p,
                            model_prob=curr_prob
                        )
                        if closed_trade:
                            icon = "🎯" if closed_trade["pnl"] >= 0 else "🛑"
                            multicoin_brain_logs.append({
                                "time": time.strftime("%H:%M:%S", time.localtime(now)),
                                "coin": coin,
                                "type": "EXIT",
                                "text": f"{icon} EXITED {closed_trade['coin']} ({closed_trade['reason']}): PnL {closed_trade['pnl']:+.2f} ({closed_trade['roi']:+.1f}%) | Equity: ${closed_trade['equity']:.2f}"
                            })

                    # Evaluate new entry
                    else:
                        signal = ""
                        edge_pct = 0.0
                        prob_val = 50.0

                        spread_y = (y_ask - y_bid) if (y_ask > 0 and y_bid > 0) else 1.0
                        spread_n = (n_ask - n_bid) if (n_ask > 0 and n_bid > 0) else 1.0
                        max_allowed_spread = 0.10  # Max 10¢ spread
                        max_rel_spread = 0.35      # Max 35% relative spread

                        is_liquid_y = (y_bid >= 0.05) and (spread_y <= max_allowed_spread) and (spread_y / max(0.01, y_ask) <= max_rel_spread)
                        is_liquid_n = (n_bid >= 0.05) and (spread_n <= max_allowed_spread) and (spread_n / max(0.01, n_ask) <= max_rel_spread)

                        # Calculate 20-second spot momentum/velocity to prevent buying into exhaustion pullbacks
                        hist = _spot_tick_history.get(coin, _deque())
                        velocity_20s = 0.0
                        if len(hist) >= 4:
                            cutoff_ts = now - 20.0
                            oldest_in_win = next((p for t, p in hist if t >= cutoff_ts), hist[0][1])
                            velocity_20s = spot - oldest_in_win

                        max_entry_cap = float(getattr(multicoin_engine, "max_entry_price", 0.65))
                        spike_delta_thresh = 70.0 if coin == "BTC" else 5.0
                        adverse_vel_thresh = 18.0 if coin == "BTC" else 2.0

                        if prob_yes_pct >= 60.0 and 0.20 <= y_ask <= max_entry_cap:
                            if edge_y >= 4.0:
                                if not is_liquid_y:
                                    action_status = "HOLD_WIDE_SPREAD"
                                    action_reason = f"Illiquid book: bid {int(y_bid*100)}¢ / ask {int(y_ask*100)}¢ (spread {int(spread_y*100)}¢ > 10¢)"
                                elif seconds_left > 500 and delta > spike_delta_thresh and y_ask > 0.58:
                                    action_status = "HOLD_OVEREXTENDED_SPIKE"
                                    action_reason = f"{coin} delta {delta:+.1f} overextended early in candle (ask {int(y_ask*100)}¢ > 58¢)"
                                elif velocity_20s < -adverse_vel_thresh:
                                    action_status = "HOLD_ADVERSE_MOMENTUM"
                                    action_reason = f"Adverse spot momentum: {coin} dropping {velocity_20s:+.1f} in 20s (exhaustion pullback)"
                                elif ofi_z <= -2.5:
                                    action_status = "HOLD_ADVERSE_OFI"
                                    action_reason = f"Extreme counter-OFI on {coin}: Z_OFI={ofi_z:+.2f} <= -2.50"
                                else:
                                    signal = "BUY YES"
                                    edge_pct = edge_y
                                    prob_val = prob_yes_pct
                        elif prob_no_pct >= 60.0 and 0.20 <= n_ask <= max_entry_cap:
                            if edge_n >= 4.0:
                                if not is_liquid_n:
                                    action_status = "HOLD_WIDE_SPREAD"
                                    action_reason = f"Illiquid book: bid {int(n_bid*100)}¢ / ask {int(n_ask*100)}¢ (spread {int(spread_n*100)}¢ > 10¢)"
                                elif seconds_left > 500 and delta < -spike_delta_thresh and n_ask > 0.58:
                                    action_status = "HOLD_OVEREXTENDED_SPIKE"
                                    action_reason = f"{coin} delta {delta:+.1f} overextended early in candle (ask {int(n_ask*100)}¢ > 58¢)"
                                elif velocity_20s > adverse_vel_thresh:
                                    action_status = "HOLD_ADVERSE_MOMENTUM"
                                    action_reason = f"Adverse spot momentum: {coin} rallying {velocity_20s:+.1f} in 20s (exhaustion squeeze)"
                                elif ofi_z >= 2.5:
                                    action_status = "HOLD_ADVERSE_OFI"
                                    action_reason = f"Extreme counter-OFI on {coin}: Z_OFI={ofi_z:+.2f} >= +2.50"
                                else:
                                    signal = "BUY NO"
                                    edge_pct = edge_n
                                    prob_val = prob_no_pct

                        is_certainty_arb = False
                        if not signal and acc is not None and 3 < seconds_left <= 45:
                            cert_res = acc.evaluate_certainty_arb(
                                strike=open_p,
                                current_spot=spot,
                                seconds_left=seconds_left,
                                dollar_vol_15m=eff_vol,
                            )
                            if cert_res.get("is_certainty_arb") and cert_res.get("side"):
                                c_side = cert_res["side"]
                                c_ask = y_ask if c_side == "YES" else n_ask
                                c_fee = (calculate_kalshi_taker_fee(40, c_ask) / 40.0) if 0.0 < c_ask < 1.0 else 0.0
                                c_edge = (cert_res["prob"] - (c_ask + c_fee)) * 100.0 if c_ask > 0 else -100.0
                                c_liquid = is_liquid_y if c_side == "YES" else is_liquid_n
                                if c_liquid and 0.20 <= c_ask <= 0.95 and c_edge >= 3.0:
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
                                print(f"[MultiCoinProd] TRADE ENTERED: {coin} {trade_pos.get('side')} {trade_pos.get('contracts')}x @ {trade_pos.get('entry_price')} on {ticker}")
                                multicoin_brain_logs.append({
                                    "time": time.strftime("%H:%M:%S", time.localtime(now)),
                                    "coin": coin,
                                    "type": "ENTRY",
                                    "text": f"🚀 ENTERED {trade_pos.get('side')} ({trade_pos.get('contracts')}x @ {int(trade_pos.get('entry_price',0)*100)}¢) on {ticker} | Edge: +{edge_pct:.1f}% | Prob: {prob_val:.1f}%"
                                })

                    # Update live cockpit matrix for frontend
                    pos = multicoin_engine.active_positions.get(coin)
                    action_status = "HOLD"
                    action_reason = ""

                    if pos:
                        cur_bid = y_bid if pos["side"] == "YES" else n_bid
                        roi_unrealized = ((cur_bid - pos["entry_price"]) / max(0.01, pos["entry_price"])) * 100.0
                        action_status = f"IN_POSITION_{pos['side']}"
                        action_reason = f"Holding {pos['contracts']}x {pos['side']} @ {int(pos['entry_price']*100)}¢ (Now: {int(cur_bid*100)}¢ | ROI: {roi_unrealized:+.1f}%)"
                    elif seconds_left < 240:
                        action_status = "HOLD_EXPIRE"
                        action_reason = f"Expiration gate active ({int(seconds_left)}s < 240s cutoff)"
                    elif seconds_left > 660:
                        action_status = "HOLD_EARLY"
                        action_reason = f"Early candle noise filter ({int(seconds_left)}s > 660s)"
                    elif multicoin_engine.candle_stopped_out.get(coin) == close_time:
                        action_status = "HOLD_STOPPED_OUT"
                        action_reason = "Candle lockout: stopped out in this 15m candle"
                    # Displayed BUY uses the engine's actual entry-price cap
                    # (0.68 today), not the broader 0.20-0.80 prefilter, so the UI
                    # never advertises a setup evaluate_entry will reject.
                    elif prob_yes_pct >= 60.0 and edge_y >= 4.0 and 0.20 <= y_ask <= max_entry_price:
                        if is_liquid_y:
                            action_status = "BUY_YES"
                            action_reason = f"Conviction {prob_yes_pct:.1f}% & Edge +{edge_y:.1f}% meets criteria"
                        else:
                            action_status = "HOLD_WIDE_SPREAD"
                            action_reason = f"Illiquid book: bid {int(y_bid*100)}¢ / ask {int(y_ask*100)}¢ (spread {int(spread_y*100)}¢ > 10¢)"
                    elif prob_no_pct >= 60.0 and edge_n >= 4.0 and 0.20 <= n_ask <= max_entry_price:
                        if is_liquid_n:
                            action_status = "BUY_NO"
                            action_reason = f"Conviction {prob_no_pct:.1f}% & Edge +{edge_n:.1f}% meets criteria"
                        else:
                            action_status = "HOLD_WIDE_SPREAD"
                            action_reason = f"Illiquid book: bid {int(n_bid*100)}¢ / ask {int(n_ask*100)}¢ (spread {int(spread_n*100)}¢ > 10¢)"
                    else:
                        best_p = max(prob_yes_pct, prob_no_pct)
                        best_e = max(edge_y, edge_n)
                        best_ask = y_ask if prob_yes_pct >= prob_no_pct else n_ask
                        if best_p < 60.0:
                            action_reason = f"Low Conviction ({best_p:.1f}% < 60%)"
                        elif best_ask > max_entry_price:
                            action_reason = f"Price above {int(round(max_entry_price*100))}¢ cap ({int(best_ask*100)}¢ > {int(round(max_entry_price*100))}¢)"
                        elif best_ask < 0.20:
                            action_reason = f"Price below 20¢ floor ({int(best_ask*100)}¢ < 20¢)"
                        else:
                            action_reason = f"Negative Edge ({best_e:+.1f}% < +4.0%)"

                    multicoin_live_matrix[coin] = {
                        "coin": coin,
                        "ticker": ticker,
                        "spot": spot,
                        "open": open_p,
                        "delta": round(delta, 4),
                        "seconds_left": int(seconds_left),
                        "z_score": round(z, 2),
                        "prob_yes_pct": round(prob_yes_pct, 1),
                        "prob_no_pct": round(prob_no_pct, 1),
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

                # Periodic brain scan log
                if now - last_heartbeat > 15.0:
                    last_heartbeat = now
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

            await asyncio.sleep(1.0)

@asynccontextmanager
async def lifespan(app):
    global startup_reconciliation_report, live_demo_balance
    # Phase 3: Cold-Boot State Reconciliation with Kalshi (prod or demo)
    try:
        startup_reconciliation_report = await live_oms.reconcile_state_on_startup(
            db_path_or_db=live_db,
            cancel_resting_orders=False  # SAFE DEFAULT: reconcile reports orphans fail-closed; only kill switches cancel
        )
        print(f"[Lifespan] Cold-boot exchange state reconciliation complete: {startup_reconciliation_report.get('status')}")
    except Exception as e:
        print(f"[Lifespan] Reconciliation notice: {e}")

    # PORTFOLIO-WIDE baseline (condition 5: confirmed settlements only, never raw OPEN rows)
    try:
        pstate = init_portfolio_baseline(_portfolio_engine_dbs())
        print(f"[Lifespan] Portfolio day-start baseline ${pstate.get('day_start_equity')} across {len(pstate.get('per_engine', {}))} engine ledger(s)")
    except Exception as e:
        print(f"[Lifespan] Portfolio baseline init error: {e}")

    # Fetch live balance
    async with httpx.AsyncClient(timeout=4.0) as client:
        try:
            if live_auth.is_configured:
                headers = live_auth.sign_request("GET", "/trade-api/v2/portfolio/balance")
                resp = await client.get(f"{KALSHI_REST_URL}/portfolio/balance", headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    live_demo_balance["balance"] = data.get("balance", 5000)
                    live_demo_balance["balance_dollars"] = float(data.get("balance", 5000)) / 100.0
                    print(f"[Lifespan] Confirmed Live Balance: ${live_demo_balance['balance_dollars']:.2f}")
        except Exception as e:
            print(f"[Lifespan] Balance fetch error: {e}")

    # Initial bootstrap
    async with httpx.AsyncClient() as client:
        await metrics_provider.update_subsecond_spot(client)
        await metrics_provider.update_candles_and_technicals(client)
        await metrics_provider.update_derivatives_context(client)
        await scanner.scan_all_btc_markets(client)

    # Initialize Async SQLAlchemy 2.0 models
    try:
        await init_async_models()
        print("[Lifespan] Async SQLAlchemy 2.0 models initialized.")
    except Exception as e:
        print(f"[Lifespan] Async DB notice: {e}")

    # Real-time WebSocket broadcast loop (streams every 1s to active UI clients)
    async def ws_tick_broadcast_loop():
        while True:
            try:
                await asyncio.sleep(1.0)
                if ws_manager.connection_count > 0:
                    payload = assemble_portfolio_payload()
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
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.delete(f"{KALSHI_REST_URL}/portfolio/events/orders", headers=cancel_headers, params={"exchange_index": 2})
                print("[Lifespan] Batch cancel-all sent to Kalshi.")
                if group_id:
                    trigger_headers = live_auth.sign_request("PUT", f"/trade-api/v2/portfolio/order_groups/{group_id}/trigger")
                    await client.put(f"{KALSHI_REST_URL}/portfolio/order_groups/{group_id}/trigger", headers=trigger_headers)
                    print(f"[Lifespan] Order group {group_id} TRIGGERED (blocks new orders until manual reset).")
    except Exception as e:
        print(f"[Lifespan] Shutdown cancel error: {e}")
    live_ws.stop()
    for t in (task_ws, task1, task2, task3, task_multicoin, task_reconcile, task_paper_reconcile, task_killwatch, task_ws_broadcast):
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
        "top_ev_opportunities": scan_data.get("top_ev_opportunities", []),
        "strike_ladders_by_expiry": scan_data.get("strike_ladders_by_expiry", {}),
        "range_brackets": scan_data.get("range_brackets", []),
        "barrier_targets": scan_data.get("barrier_targets", []),
        "arbitrage_alerts": scan_data.get("arbitrage_alerts", []),
        "stats": scan_data.get("stats", {}),
        "bot": bot_data,
        "portfolio": assemble_portfolio_payload(),
        "multicoin": multicoin_engine.get_status_dict(),
        "multicoin_matrix": multicoin_live_matrix,
        "multicoin_brain_logs": (multicoin_brain_logs[-20:] if multicoin_brain_logs else []),
        "live_kalshi": live_kalshi_payload
    }

def assemble_tick_payload() -> Dict[str, Any]:
    """Lightweight 1-second tick payload (~5KB) preventing DOM thrashing and client memory churn."""
    scan_data = scanner.cache
    metrics_data = metrics_provider.get_summary_dict()
    bot_data = paper_bot.get_status_dict()

    active_mkt = scan_data.get("kxbtc15m", {})
    ticker = active_mkt.get("ticker", "")
    book = live_ws.get_or_create_book(ticker) if ticker else None
    sweep_10 = book.calculate_sweep_vwap("YES", 10) if book and book.asks else None
    sweep_25 = book.calculate_sweep_vwap("YES", 25) if book and book.asks else None

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
        "top_ev_opportunities": scan_data.get("top_ev_opportunities", []),
        "arbitrage_alerts": scan_data.get("arbitrage_alerts", []),
        "stats": scan_data.get("stats", {}),
        "bot": bot_data,
        "multicoin": multicoin_engine.get_status_dict(),
        "multicoin_matrix": multicoin_live_matrix,
        "multicoin_brain_logs": (multicoin_brain_logs[-20:] if multicoin_brain_logs else []),
        "live_kalshi": live_kalshi_payload
    }

async def get_live_scan(request):
    """Return full instantaneous state as JSON."""
    return JSONResponse(assemble_full_payload())

async def live_stream_sse(request):
    """
    Server-Sent Events (SSE) streaming endpoint.
    Streams live ticks, probability updates, bot state, and radar changes every 1.0 second.
    """
    async def event_generator():
        first_tick = True
        try:
            while True:
                if await request.is_disconnected():
                    break
                if first_tick:
                    payload = json.dumps(assemble_full_payload())
                    first_tick = False
                else:
                    payload = json.dumps(assemble_tick_payload())
                yield f"data: {payload}\n\n"
                await asyncio.sleep(1.0)
        except (asyncio.CancelledError, GeneratorExit, Exception):
            pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
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

routes = [
    Route("/", serve_dashboard, methods=["GET"]),
    Route("/dashboard", serve_dashboard, methods=["GET"]),
    Route("/cockpit", serve_dashboard, methods=["GET"]),
    Route("/api/scan", get_live_scan, methods=["GET"]),
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
        # Send immediate initial portfolio and market snapshot
        snapshot = assemble_portfolio_payload()
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
