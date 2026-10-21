import asyncio
import os
import json
import time
import math
import sqlite3
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
from core.multi_coin_trader import MultiCoinPaperEngine, calculate_kalshi_taker_fee
from live_executor import (
    KALSHI_DEMO_REST_URL,
    KALSHI_DEMO_WS_URL,
    KALSHI_PROD_REST_URL,
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
from phoenix.otel import register

# Initialize OpenTelemetry tracer provider for Phoenix observability
tracer_provider = register(project_name="kalshi-multi-coin-bot", auto_instrument=True, batch=True)

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
multicoin_engine = MultiCoinPaperEngine(initial_balance=100.0, db_path="multi_coin_paper.db")
multicoin_live_matrix: Dict[str, Dict[str, Any]] = {}
multicoin_brain_logs: List[Dict[str, Any]] = []

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
                # Suppress normal network hiccups or log unexpected
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
                # Suppress normal network hiccups or log unexpected
                pass

def normal_cdf(x: float) -> float:
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

async def multi_coin_production_loop():
    """
    Continuous Multi-Asset (BTC, ETH, SOL, XRP) production paper trading loop.
    Evaluates drift-diffusion edge against real Kalshi Production order books (api.elections.kalshi.com)
    and dynamically rebalances performance-based capital weights ($100 initial baseline).
    """
    print("Starting Multi-Coin Production Paper Trading Loop (BTC, ETH, SOL, XRP)...")
    coin_configs = {
        "BTC": {"series": "KXBTC15M", "vol_15m": 95.0},
        "ETH": {"series": "KXETH15M", "vol_15m": 6.5},
        "SOL": {"series": "KXSOL15M", "vol_15m": 1.2},
        "XRP": {"series": "KXXRP15M", "vol_15m": 0.025}
    }

    candle_opens: Dict[str, float] = {}
    last_candle_fetch = 0.0
    cached_markets: Dict[str, Dict[str, Any]] = {}
    last_mkt_fetch = 0.0
    last_heartbeat = 0.0

    async with httpx.AsyncClient(timeout=4.0) as client:
        while True:
            try:
                now = time.time()
                now_ms = int(now * 1000)
                candle_15m_start = (now_ms // (15 * 60 * 1000)) * (15 * 60 * 1000)
                candle_15m_end = candle_15m_start + (15 * 60 * 1000)
                seconds_left = max(1.0, float((candle_15m_end - now_ms) / 1000.0))

                # 1. Refresh spot prices from Hyperliquid
                hl_resp = await client.post("https://api.hyperliquid.xyz/info", json={"type": "allMids"})
                spot_prices = {}
                if hl_resp.status_code == 200:
                    mids = hl_resp.json()
                    for coin in coin_configs:
                        val = mids.get(coin)
                        if val is not None:
                            spot_prices[coin] = float(val)

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
                        except Exception:
                            pass
                    last_candle_fetch = now

                # 3. Refresh active production markets every 10s
                if now - last_mkt_fetch > 10.0 or not cached_markets:
                    for coin, cfg in coin_configs.items():
                        try:
                            m_resp = await client.get(f"{KALSHI_PROD_REST_URL}/markets?series_ticker={cfg['series']}&status=open")
                            if m_resp.status_code == 200:
                                mkts = m_resp.json().get("markets", [])
                                if mkts:
                                    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                                    active = [m for m in mkts if (m.get("close_time") or "") >= now_iso]
                                    cached_markets[coin] = active[0] if active else mkts[0]
                        except Exception:
                            pass
                    last_mkt_fetch = now

                # 4. Evaluate each coin
                for coin, cfg in coin_configs.items():
                    spot = spot_prices.get(coin, 0.0)
                    open_p = candle_opens.get(coin, spot)
                    mkt = cached_markets.get(coin)
                    if not spot or not mkt:
                        continue

                    ticker = mkt.get("ticker", "")
                    close_time = mkt.get("close_time", "")
                    y_ask = float(mkt.get("yes_ask_dollars") or 0.0)
                    y_bid = float(mkt.get("yes_bid_dollars") or 0.0)
                    n_ask = float(mkt.get("no_ask_dollars") or 0.0)
                    n_bid = float(mkt.get("no_bid_dollars") or 0.0)

                    # Model probability
                    delta = spot - open_p
                    sigma = cfg["vol_15m"] * math.sqrt(seconds_left / 900.0)
                    z = delta / max(0.0001, sigma)
                    prob_yes = normal_cdf(z)
                    prob_no = 1.0 - prob_yes
                    prob_yes_pct = prob_yes * 100.0
                    prob_no_pct = prob_no * 100.0

                    # Calculate net post-fee edges
                    fee_y = calculate_kalshi_taker_fee(1, y_ask) if y_ask > 0 else 0.0
                    fee_n = calculate_kalshi_taker_fee(1, n_ask) if n_ask > 0 else 0.0
                    edge_y = (prob_yes - (y_ask + fee_y)) * 100.0 if y_ask > 0 else -100.0
                    edge_n = (prob_no - (n_ask + fee_n)) * 100.0 if n_ask > 0 else -100.0

                    # Evaluate open positions
                    if coin in multicoin_engine.active_positions:
                        pos = multicoin_engine.active_positions[coin]
                        cur_bid = y_bid if pos["side"] == "YES" else n_bid
                        if cur_bid <= 0.01:
                            cur_bid = pos.get("current_bid", pos["entry_price"])
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

                        if prob_yes_pct >= 60.0 and 0.20 <= y_ask <= 0.80:
                            if edge_y >= 4.0:
                                signal = "BUY YES"
                                edge_pct = edge_y
                                prob_val = prob_yes_pct
                        elif prob_no_pct >= 60.0 and 0.20 <= n_ask <= 0.80:
                            if edge_n >= 4.0:
                                signal = "BUY NO"
                                edge_pct = edge_n
                                prob_val = prob_no_pct

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
                                candle_id=close_time
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
                    elif seconds_left < 90:
                        action_status = "HOLD_EXPIRE"
                        action_reason = f"Expiration gate active ({int(seconds_left)}s < 90s cutoff)"
                    elif seconds_left > 660:
                        action_status = "HOLD_EARLY"
                        action_reason = f"Early candle noise filter ({int(seconds_left)}s > 660s)"
                    elif multicoin_engine.candle_stopped_out.get(coin) == close_time:
                        action_status = "HOLD_STOPPED_OUT"
                        action_reason = "Candle lockout: stopped out in this 15m candle"
                    elif prob_yes_pct >= 60.0 and edge_y >= 4.0 and 0.20 <= y_ask <= 0.80:
                        action_status = "BUY_YES"
                        action_reason = f"Conviction {prob_yes_pct:.1f}% & Edge +{edge_y:.1f}% meets criteria"
                    elif prob_no_pct >= 60.0 and edge_n >= 4.0 and 0.20 <= n_ask <= 0.80:
                        action_status = "BUY_NO"
                        action_reason = f"Conviction {prob_no_pct:.1f}% & Edge +{edge_n:.1f}% meets criteria"
                    else:
                        best_p = max(prob_yes_pct, prob_no_pct)
                        best_e = max(edge_y, edge_n)
                        if best_p < 60.0:
                            action_reason = f"Low Conviction ({best_p:.1f}% < 60%)"
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
                    summary = " | ".join([f"{c}: d={spot_prices.get(c,0)-candle_opens.get(c,0):+.2f}" for c in coin_configs])
                    mkts_str = ", ".join([f"{c}:{cached_markets.get(c, {}).get('ticker', 'none')}" for c in coin_configs])
                    print(f"[MultiCoinProd] Heartbeat | {summary} | Markets: [{mkts_str}] | Active: {list(multicoin_engine.active_positions.keys())}")
                    multicoin_brain_logs.append({
                        "time": time.strftime("%H:%M:%S", time.localtime(now)),
                        "type": "SCAN",
                        "text": f"Scanned 4 coins ({int(seconds_left)}s left) | BTC: {spot_prices.get('BTC',0)-candle_opens.get('BTC',0):+.1f} | ETH: {spot_prices.get('ETH',0)-candle_opens.get('ETH',0):+.2f} | SOL: {spot_prices.get('SOL',0)-candle_opens.get('SOL',0):+.2f} | XRP: {spot_prices.get('XRP',0)-candle_opens.get('XRP',0):+.4f} | Equity: ${multicoin_engine.total_equity:.2f}"
                    })
                    if len(multicoin_brain_logs) > 30:
                        multicoin_brain_logs.pop(0)

            except Exception as e:
                print(f"[MultiCoinProd] Exception: {e}")

            await asyncio.sleep(1.0)

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
    task_multicoin = asyncio.create_task(multi_coin_production_loop())
    yield
    live_ws.stop()
    task_ws.cancel()
    task1.cancel()
    task2.cancel()
    task3.cancel()
    task_multicoin.cancel()

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
        "multicoin": multicoin_engine.get_status_dict(),
        "multicoin_matrix": multicoin_live_matrix,
        "multicoin_brain_logs": multicoin_brain_logs[-20:],
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
        "multicoin": multicoin_engine.get_status_dict(),
        "multicoin_matrix": multicoin_live_matrix,
        "multicoin_brain_logs": multicoin_brain_logs[-20:],
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

async def get_multicoin_status(request):
    """Return multi-coin paper trading engine status."""
    return JSONResponse(multicoin_engine.get_status_dict())

async def get_multicoin_trades(request):
    """Return recent multi-coin trades from SQLite."""
    return JSONResponse(multicoin_engine.db.get_recent_trades(limit=50))

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
        with sqlite3.connect("kalshi_live_demo.db") as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT id, timestamp, ticker, side, entry_price, exit_price, exit_reason, pnl_dollars, contracts FROM trades WHERE timestamp >= ? ORDER BY id DESC", (now - block_window,))
            live_trades_15m = [dict(r) for r in c.fetchall()]
    except Exception:
        pass

    paper_trades_15m = []
    try:
        with sqlite3.connect("kalshi_audit.db") as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT id, timestamp, ticker, side, entry_price, exit_price, exit_reason, pnl_dollars, contracts FROM trades WHERE timestamp >= ? ORDER BY id DESC", (now - block_window,))
            paper_trades_15m = [dict(r) for r in c.fetchall()]
    except Exception:
        pass

    multicoin_trades_15m = []
    try:
        with sqlite3.connect("multi_coin_paper.db") as conn:
            conn.row_factory = sqlite3.Row
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

    # Multi-Coin Portfolio & Block Audit Synchronization routes
    Route("/api/multicoin/status", get_multicoin_status, methods=["GET"]),
    Route("/api/multicoin/trades", get_multicoin_trades, methods=["GET"]),
    Route("/api/audit/block_summary", get_audit_block_summary, methods=["GET"]),
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
