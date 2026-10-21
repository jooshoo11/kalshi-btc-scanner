import asyncio
import os
import json
import time
from typing import Dict, Any, Optional
from starlette.applications import Starlette
from starlette.responses import JSONResponse, HTMLResponse, StreamingResponse, Response
from starlette.routing import Route
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import uvicorn
import httpx

from btc_metrics import BitcoinMetricsProvider
from prediction_engine import QuantPredictionEngine
from kalshi_btc_scanner import KalshiBitcoinScanner
from bot_database import AuditDatabase
from paper_trader import PaperTradingBot
from live_executor import (
    KALSHI_DEMO_REST_URL,
    KALSHI_DEMO_WS_URL,
    KalshiAuth,
    KalshiWebSocketClient,
    KalshiOrderManagementSystem,
    StaleTickWatchdog,
    KalshiStateReconciler
)
from dotenv import load_dotenv

# Load credentials from .env
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# Global safety and execution instances
watchdog = StaleTickWatchdog(max_allowed_delta_ms=1500.0, recovery_threshold_ms=800.0, recovery_ticks=3)
metrics_provider = BitcoinMetricsProvider(watchdog=watchdog)
prediction_engine = QuantPredictionEngine(risk_free_rate=0.045, taker_fee_cents=0.5)
scanner = KalshiBitcoinScanner(metrics_provider, prediction_engine)
db = AuditDatabase("kalshi_audit.db")
live_db = AuditDatabase("kalshi_live_demo.db")
paper_bot = PaperTradingBot(db, initial_balance=50.0, watchdog=watchdog)

# Live Kalshi Demo OMS & WebSocket instances
live_auth = KalshiAuth.from_env()
live_ws = KalshiWebSocketClient(ws_url=KALSHI_DEMO_WS_URL, auth=live_auth)
live_oms = KalshiOrderManagementSystem(
    auth=live_auth,
    rest_url=KALSHI_DEMO_REST_URL,
    ws_client=live_ws,
    watchdog=watchdog,
    dry_run=False
)
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
                    resp = await client.get(f"{KALSHI_DEMO_REST_URL}/portfolio/balance", headers=headers, timeout=3.0)
                    if resp.status_code == 200:
                        data = resp.json()
                        live_demo_balance["balance"] = data.get("balance", 5254)
                        live_demo_balance["balance_dollars"] = float(data.get("balance", 5254)) / 100.0
            except Exception as e:
                pass
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
                pass

@asynccontextmanager
async def lifespan(app):
    global startup_reconciliation_report, live_demo_balance
    # Phase 3: Cold-Boot State Reconciliation with Kalshi Demo (isolated to live_db)
    try:
        startup_reconciliation_report = await live_oms.reconcile_state_on_startup(
            db_path_or_db=live_db,
            cancel_resting_orders=True
        )
        print(f"[Lifespan] Cold-boot exchange state reconciliation complete: {startup_reconciliation_report.get('status')}")
    except Exception as e:
        print(f"[Lifespan] Reconciliation notice: {e}")

    # Fetch live demo balance
    async with httpx.AsyncClient(timeout=4.0) as client:
        try:
            if live_auth.is_configured:
                headers = live_auth.sign_request("GET", "/trade-api/v2/portfolio/balance")
                resp = await client.get(f"{KALSHI_DEMO_REST_URL}/portfolio/balance", headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    live_demo_balance["balance"] = data.get("balance", 5000)
                    live_demo_balance["balance_dollars"] = float(data.get("balance", 5000)) / 100.0
                    print(f"[Lifespan] Confirmed Live Demo Balance: ${live_demo_balance['balance_dollars']:.2f}")
        except Exception as e:
            print(f"[Lifespan] Balance fetch error: {e}")

    # Initial bootstrap
    async with httpx.AsyncClient() as client:
        await metrics_provider.update_subsecond_spot(client)
        await metrics_provider.update_candles_and_technicals(client)
        await metrics_provider.update_derivatives_context(client)
        await scanner.scan_all_btc_markets(client)

    task_ws = asyncio.create_task(live_ws.connect_and_listen())
    task1 = asyncio.create_task(fast_spot_loop())
    task2 = asyncio.create_task(full_scan_loop())
    task3 = asyncio.create_task(technicals_loop())
    yield
    live_ws.stop()
    task_ws.cancel()
    task1.cancel()
    task2.cancel()
    task3.cancel()

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
        "rest_url": KALSHI_DEMO_REST_URL,
        "ws_url": KALSHI_DEMO_WS_URL,
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
        "rest_url": KALSHI_DEMO_REST_URL,
        "ws_url": KALSHI_DEMO_WS_URL,
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
        "rest_url": KALSHI_DEMO_REST_URL,
        "ws_url": KALSHI_DEMO_WS_URL,
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
    report = await live_oms.reconcile_state_on_startup(db_path_or_db=live_db, cancel_resting_orders=True)
    startup_reconciliation_report = report
    return JSONResponse(report)

async def post_live_refresh_balance(request):
    """Query fresh demo balance from Kalshi REST API."""
    global live_demo_balance
    async with httpx.AsyncClient(timeout=4.0) as client:
        try:
            if live_auth.is_configured:
                headers = live_auth.sign_request("GET", "/trade-api/v2/portfolio/balance")
                resp = await client.get(f"{KALSHI_DEMO_REST_URL}/portfolio/balance", headers=headers)
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

routes = [
    Route("/", serve_dashboard, methods=["GET"]),
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
]

app = Starlette(
    routes=routes,
    lifespan=lifespan,
    middleware=[
        Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    ]
)

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8088, log_level="info")
