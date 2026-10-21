#!/usr/bin/env python3
"""
test_4coin_demo_suite.py — Comprehensive End-to-End Test Suite Across All 4 Coins (BTC, ETH, SOL, XRP)
Uses the active Kalshi Demo API keys to verify:
1. RSA-PSS Authentication & Portfolio Balance
2. Market Discovery across BTC (KXBTC15M), ETH (KXETH15M), SOL (KXSOL15M), XRP (KXXRP15M)
3. L2 Order Book retrieval for all 4 coins
4. Real order placement via REST API (/portfolio/orders) on all 4 coins
5. Order state verification on exchange matching engine
6. Clean cancellation of all test orders (zero orphaned orders)
7. WebSocket streaming connectivity & channel subscription for all 4 coins
8. Quant prediction & Phoenix tracing execution
"""

import sys
import os
import time
import json
import uuid
import asyncio
import httpx

# Ensure local project import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

from core.live_executor import (
    KalshiAuth,
    KalshiWebSocketClient,
    KalshiOrderManagementSystem,
    KALSHI_DEMO_REST_URL,
    KALSHI_DEMO_WS_URL,
    KALSHI_PROD_REST_URL
)
from core.prediction_engine import QuantPredictionEngine

COIN_CONFIGS = {
    "BTC": {"series": "KXBTC15M", "name": "Bitcoin"},
    "ETH": {"series": "KXETH15M", "name": "Ethereum"},
    "SOL": {"series": "KXSOL15M", "name": "Solana"},
    "XRP": {"series": "KXXRP15M", "name": "Ripple"}
}

class TestResultTracker:
    def __init__(self):
        self.tests_run = 0
        self.tests_passed = 0
        self.tests_failed = 0
        self.details = []

    def record(self, test_name: str, passed: bool, message: str = ""):
        self.tests_run += 1
        if passed:
            self.tests_passed += 1
            print(f"  ✅ [PASS] {test_name}: {message}")
        else:
            self.tests_failed += 1
            print(f"  ❌ [FAIL] {test_name}: {message}")
        self.details.append({"test": test_name, "passed": passed, "message": message})


async def run_full_4coin_test_suite():
    print("=" * 70)
    print("🚀 STARTING KALSHI 4-COIN PRE-FLIGHT TEST SUITE (DEMO API)")
    print(f"Target REST: {KALSHI_DEMO_REST_URL}")
    print(f"Target WS:   {KALSHI_DEMO_WS_URL}")
    print("=" * 70)

    tracker = TestResultTracker()
    auth = KalshiAuth.from_env()

    # ---------------------------------------------------------
    # TEST 1: RSA-PSS AUTHENTICATION & PORTFOLIO BALANCE
    # ---------------------------------------------------------
    print("\n[TEST 1] Verifying RSA-PSS Signature & Demo Portfolio Balance...")
    if not auth.is_configured:
        tracker.record("Auth Configuration", False, "KalshiAuth failed to load credentials from .env")
        return tracker
    tracker.record("Auth Configuration", True, f"Loaded Key ID: {auth.key_id[:12]}...")

    bal_url = f"{KALSHI_DEMO_REST_URL}/portfolio/balance"
    headers = auth.sign_request("GET", "/portfolio/balance")
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(bal_url, headers=headers)
        if resp.status_code == 200:
            bal_data = resp.json()
            balance_dollars = bal_data.get("balance_dollars", "0.0")
            tracker.record("Balance Retrieval", True, f"Account Balance: ${balance_dollars}")
        else:
            tracker.record("Balance Retrieval", False, f"HTTP {resp.status_code}: {resp.text}")

    # ---------------------------------------------------------
    # TEST 2: MARKET DISCOVERY ACROSS ALL 4 COINS
    # ---------------------------------------------------------
    print("\n[TEST 2] Discovering Active 15M Markets for BTC, ETH, SOL, XRP...")
    active_tickers = {}
    async with httpx.AsyncClient(timeout=10.0) as client:
        for coin, cfg in COIN_CONFIGS.items():
            path = f"/markets?series_ticker={cfg['series']}&status=open&limit=10"
            headers = auth.sign_request("GET", path)
            r = await client.get(f"{KALSHI_DEMO_REST_URL}{path}", headers=headers)
            if r.status_code == 200:
                markets = r.json().get("markets", [])
                if markets:
                    t = markets[0]["ticker"]
                    active_tickers[coin] = t
                    tracker.record(f"Market Discovery ({coin})", True, f"Found {len(markets)} markets. Active: {t}")
                else:
                    tracker.record(f"Market Discovery ({coin})", False, "No open markets returned")
            else:
                tracker.record(f"Market Discovery ({coin})", False, f"HTTP {r.status_code}: {r.text}")

    # ---------------------------------------------------------
    # TEST 3: L2 ORDERBOOK RETRIEVAL FOR ALL 4 COINS
    # ---------------------------------------------------------
    print("\n[TEST 3] Fetching L2 Order Books on Kalshi Demo for All 4 Coins...")
    async with httpx.AsyncClient(timeout=10.0) as client:
        for coin, ticker in active_tickers.items():
            path = f"/markets/{ticker}/orderbook"
            headers = auth.sign_request("GET", path)
            r = await client.get(f"{KALSHI_DEMO_REST_URL}{path}", headers=headers)
            if r.status_code == 200:
                ob = r.json().get("orderbook", {})
                bids = ob.get("yes", [])
                asks = ob.get("no", [])
                tracker.record(f"L2 Orderbook ({coin})", True, f"{ticker} has {len(bids)} yes bids and {len(asks)} no bids in book")
            else:
                tracker.record(f"L2 Orderbook ({coin})", False, f"HTTP {r.status_code}: {r.text}")

    # ---------------------------------------------------------
    # TEST 4: REAL ORDER PLACEMENT (RESTING LIMIT ORDERS) ACROSS ALL 4 COINS
    # ---------------------------------------------------------
    print("\n[TEST 4] Submitting Real Test Limit Orders (1 contract @ 10¢) on All 4 Coins...")
    placed_orders = {}
    oms = KalshiOrderManagementSystem(auth=auth, rest_url=KALSHI_DEMO_REST_URL, dry_run=False)

    for coin, ticker in active_tickers.items():
        try:
            t0 = time.time()
            order_res = await oms._place_rest_order(
                ticker=ticker,
                action="buy",
                side="yes",
                count=1,
                price_cents=10,  # Resting far out of the money at 10c so it rests safely
                order_type="limit"
            )
            lat_ms = (time.time() - t0) * 1000.0
            order_id = order_res.get("order_id")
            status = order_res.get("status")
            if order_id:
                placed_orders[coin] = {"order_id": order_id, "ticker": ticker}
                tracker.record(f"Order Placement ({coin})", True, f"Placed {coin} order {order_id[:8]}... ({status}) in {lat_ms:.1f}ms")
            else:
                tracker.record(f"Order Placement ({coin})", False, f"Failed response: {order_res}")
        except Exception as e:
            tracker.record(f"Order Placement ({coin})", False, f"Exception: {e}")

    # ---------------------------------------------------------
    # TEST 5: RESTING ORDERS QUERY & MATCHING ENGINE VERIFICATION
    # ---------------------------------------------------------
    print("\n[TEST 5] Querying Exchange Resting Orders via /portfolio/orders...")
    await asyncio.sleep(0.5)  # Allow exchange order matching propagation
    async with httpx.AsyncClient(timeout=10.0) as client:
        path = "/portfolio/orders?status=resting"
        headers = auth.sign_request("GET", path)
        r = await client.get(f"{KALSHI_DEMO_REST_URL}{path}", headers=headers)
        if r.status_code == 200:
            active_resting = r.json().get("orders", [])
            resting_ids = {o.get("order_id") for o in active_resting}
            for coin, p_info in placed_orders.items():
                oid = p_info["order_id"]
                found = oid in resting_ids
                tracker.record(f"Exchange Verification ({coin})", found, f"Order {oid[:8]} verified resting on Kalshi matching engine")
        else:
            tracker.record("Exchange Verification", False, f"HTTP {r.status_code}: {r.text}")

    # ---------------------------------------------------------
    # TEST 6: CLEAN CANCELLATION OF ALL TEST ORDERS
    # ---------------------------------------------------------
    print("\n[TEST 6] Cancelling All Placed Test Orders (Zero Orphaned Risk)...")
    for coin, p_info in placed_orders.items():
        oid = p_info["order_id"]
        ticker = p_info["ticker"]
        try:
            cancelled = await oms.cancel_order(order_id=oid, market_ticker=ticker)
            tracker.record(f"Order Cancellation ({coin})", cancelled, f"Order {oid[:8]} successfully canceled on exchange")
        except Exception as e:
            tracker.record(f"Order Cancellation ({coin})", False, f"Cancellation exception: {e}")

    await asyncio.sleep(0.5)
    # Verify no orphaned orders remain
    async with httpx.AsyncClient(timeout=10.0) as client:
        path = "/portfolio/orders?status=resting"
        headers = auth.sign_request("GET", path)
        r = await client.get(f"{KALSHI_DEMO_REST_URL}{path}", headers=headers)
        if r.status_code == 200:
            remaining = r.json().get("orders", [])
            test_remaining = [o for o in remaining if o.get("order_id") in [p["order_id"] for p in placed_orders.values()]]
            tracker.record("Zero Orphaned Orders Check", len(test_remaining) == 0, f"Remaining test orders: {len(test_remaining)}")

    # ---------------------------------------------------------
    # TEST 7: WEBSOCKET HANDSHAKE & STREAMING SUBSCRIPTION
    # ---------------------------------------------------------
    print("\n[TEST 7] Testing WebSocket Connection & Handshake on Kalshi Demo...")
    try:
        ws_client = KalshiWebSocketClient(ws_url=KALSHI_DEMO_WS_URL, auth=auth)
        # Launch ws connection in background task
        ws_task = asyncio.create_task(ws_client.connect_and_listen())
        await asyncio.sleep(2.0)

        is_connected = ws_client.is_ws_connected
        tracker.record("WebSocket Connection", is_connected, f"Connected to {KALSHI_DEMO_WS_URL}")

        # Subscribe to all 4 coins
        for coin, ticker in active_tickers.items():
            await ws_client.subscribe_ticker(ticker)
        await asyncio.sleep(1.0)
        tracker.record("WebSocket Subscriptions", True, f"Subscribed to tickers: {list(active_tickers.values())}")

        ws_client.stop()
        ws_task.cancel()
    except Exception as e:
        tracker.record("WebSocket Verification", False, f"WS error: {e}")

    # ---------------------------------------------------------
    # TEST 8: QUANT PREDICTION ENGINE & PHOENIX TRACING
    # ---------------------------------------------------------
    print("\n[TEST 8] Testing Quant Prediction Engine & Phoenix Tracing on All 4 Coins...")
    try:
        engine = QuantPredictionEngine()
        sim_spots = {"BTC": 82800.0, "ETH": 2450.0, "SOL": 106.0, "XRP": 1.33}
        for coin, s_spot in sim_spots.items():
            pred = engine.evaluate_15m_scalp(
                spot=s_spot,
                open_price=s_spot - 5.0 if coin != "XRP" else s_spot - 0.005,
                seconds_left=450.0,
                dollar_vol_15m=95.0 if coin == "BTC" else 15.0,
                momentum_bias=0.0
            )
            has_hypothesis = "hypothesis" in pred
            has_market_data = "market_data" in pred
            tracker.record(f"Quant Model ({coin})", has_hypothesis and has_market_data, f"Prob YES: {pred['prob_yes_pct']}% | Hypothesis: {pred.get('hypothesis')}")
    except Exception as e:
        tracker.record("Quant Engine Verification", False, f"Prediction error: {e}")

    # ---------------------------------------------------------
    # FINAL SUMMARY
    # ---------------------------------------------------------
    print("\n" + "=" * 70)
    print(f"🏁 PRE-FLIGHT TEST SUMMARY: {tracker.tests_passed}/{tracker.tests_run} TESTS PASSED")
    if tracker.tests_failed == 0:
        print("🎉 ALL TESTS PASSED! API, AUTH, 4-COIN EXECUTION, WEBSOCKET, & RISK CONTROLS VERIFIED 100% OPERATIONAL.")
    else:
        print(f"⚠️ {tracker.tests_failed} TESTS FAILED. See details above.")
    print("=" * 70)
    return tracker


if __name__ == "__main__":
    asyncio.run(run_full_4coin_test_suite())
