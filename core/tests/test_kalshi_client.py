#!/usr/bin/env python3
"""
test_kalshi_client.py — Comprehensive Test Suite for kalshi_client.py & AsyncOrderManager.

Verifies:
1. RSA-PSS Authentication & Environment Key Loading (string, escaped newlines, file path).
2. KalshiClient Demo URL configuration and non-blocking REST endpoints.
3. AsyncOrderManager L2 Book caching (snapshots, deltas, best bid/ask).
4. Real-time VWAP sweep calculations across depth tiers.
5. execute_trade() method with liquidity, EV threshold, and Stale-Tick Watchdog protection.
6. execute_stop_loss() limit-chasing routing logic.
"""

import os
import sys
import tempfile
import asyncio
from typing import Dict, Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption

from kalshi_client import (
    DEFAULT_DEMO_REST_URL,
    DEFAULT_DEMO_WS_URL,
    KalshiAuth,
    KalshiClient,
    L2OrderBook,
    StaleTickWatchdog,
    AsyncOrderManager
)


async def test_auth_and_env_loading():
    print("\n[+] 1. Testing RSA-PSS Auth & Environment Variable Loading...")

    # Generate temporary RSA key
    priv_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_bytes = priv_key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption()
    )
    pem_str = pem_bytes.decode("utf-8")

    # Case A: Direct PEM String
    auth_direct = KalshiAuth(key_id="test_key_1", private_key_pem=pem_str)
    assert auth_direct.is_configured, "Auth direct should be configured"
    headers = auth_direct.sign_request("GET", "/trade-api/v2/portfolio/balance", timestamp_ms=1700000000000)
    assert headers["KALSHI-ACCESS-KEY"] == "test_key_1"
    assert "KALSHI-ACCESS-SIGNATURE" in headers
    assert len(headers["KALSHI-ACCESS-SIGNATURE"]) > 100
    print("  ✓ Direct PEM string signing verified.")

    # Case B: Load from Environment with escaped newlines (e.g. from .env file)
    escaped_pem = pem_str.replace("\n", "\\n")
    os.environ["KALSHI_API_KEY_ID"] = "env_key_id_999"
    os.environ["KALSHI_PRIVATE_KEY_PEM"] = escaped_pem
    if "KALSHI_PRIVATE_KEY_PATH" in os.environ:
        del os.environ["KALSHI_PRIVATE_KEY_PATH"]

    auth_env = KalshiAuth.from_env()
    assert auth_env.is_configured, "Auth from env (escaped PEM) should be configured"
    assert auth_env.key_id == "env_key_id_999"
    sig_headers = auth_env.sign_request("POST", "/trade-api/v2/portfolio/orders")
    assert sig_headers["KALSHI-ACCESS-KEY"] == "env_key_id_999"
    print("  ✓ Environment variable with escaped newlines successfully parsed & signed.")

    # Case C: Load from File Path via Environment
    with tempfile.NamedTemporaryFile("wb", delete=False) as tf:
        tf.write(pem_bytes)
        temp_path = tf.name

    try:
        del os.environ["KALSHI_PRIVATE_KEY_PEM"]
        os.environ["KALSHI_PRIVATE_KEY_PATH"] = temp_path
        auth_file = KalshiAuth.from_env()
        assert auth_file.is_configured, "Auth from file path should be configured"
        print("  ✓ Environment file path loading verified.")
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        # Cleanup test env vars
        os.environ.pop("KALSHI_API_KEY_ID", None)
        os.environ.pop("KALSHI_PRIVATE_KEY_PATH", None)


async def test_kalshi_client_rest():
    print("\n[+] 2. Testing KalshiClient Demo Configuration & REST Interface...")
    client = KalshiClient(dry_run=True)
    assert client.base_url == DEFAULT_DEMO_REST_URL, f"Expected {DEFAULT_DEMO_REST_URL}, got {client.base_url}"
    print(f"  ✓ Default base URL configured to Demo: {client.base_url}")

    bal = await client.get_balance()
    assert "balance" in bal
    print(f"  ✓ get_balance() returned: {bal}")

    positions = await client.get_positions()
    assert isinstance(positions, list)
    print(f"  ✓ get_positions() returned {len(positions)} positions.")

    order = await client.place_order(
        ticker="KXBTC15M-26OCT19-T68500",
        action="buy",
        side="yes",
        count=10,
        price_cents=65
    )
    assert order["ticker"] == "KXBTC15M-26OCT19-T68500"
    assert order["status"] == "executed"
    print(f"  ✓ place_order() executed mock order {order['order_id']} for 10x @ 65¢.")

    canceled = await client.cancel_order(order["order_id"])
    assert canceled is True
    print("  ✓ cancel_order() verified.")

    await client.close()


async def test_order_book_and_vwap():
    print("\n[+] 3. Testing L2OrderBook Delta Tracking & VWAP Sweeps...")
    book = L2OrderBook(ticker="KXBTC15M-26OCT19-T68500")

    # Snapshot: Bids (68¢x20, 65¢x30), Asks (70¢x10, 72¢x20, 75¢x30)
    book.apply_snapshot(
        bids=[[68, 20], [65, 30]],
        asks=[[70, 10], [72, 20], [75, 30]]
    )

    best_bid, bid_qty = book.get_best_bid()
    best_ask, ask_qty = book.get_best_ask()
    assert best_bid == 68 and bid_qty == 20
    assert best_ask == 70 and ask_qty == 10
    print(f"  ✓ Snapshot Top of Book: {best_bid}¢ ({bid_qty}) / {best_ask}¢ ({ask_qty})")

    # Delta update: new ask level @ 69¢ for 5 contracts
    book.apply_delta("yes_ask", 69, 5)
    best_ask, ask_qty = book.get_best_ask()
    assert best_ask == 69 and ask_qty == 5
    print(f"  ✓ Delta update applied: New Best Ask = {best_ask}¢ ({ask_qty} contracts)")

    # VWAP Sweep for 15 contracts:
    # 5 contracts @ 69¢ + 10 contracts @ 70¢ = 345 + 700 = 1045 cents. VWAP = 1045 / 15 = 69.67¢
    sweep = book.calculate_sweep_vwap("YES", 15)
    assert sweep["can_fill"] is True
    assert sweep["vwap_cents"] == 69.67
    assert sweep["worst_price_cents"] == 70
    assert sweep["levels_swept"] == 2
    print(f"  ✓ VWAP Sweep 15 contracts: {sweep['vwap_cents']}¢ (Worst limit: {sweep['worst_price_cents']}¢)")


async def test_async_order_manager_execution():
    print("\n[+] 4. Testing AsyncOrderManager execute_trade() Safety & Execution...")
    watchdog = StaleTickWatchdog(max_allowed_delta_ms=1500.0)
    client = KalshiClient(dry_run=True)
    oms = AsyncOrderManager(client=client, watchdog=watchdog, ws_url=DEFAULT_DEMO_WS_URL, dry_run=True)

    assert oms.ws_url == DEFAULT_DEMO_WS_URL
    print(f"  ✓ AsyncOrderManager WebSocket URL configured to: {oms.ws_url}")

    ticker = "KXBTC15M-TEST"
    book = oms.get_or_create_book(ticker)
    book.apply_snapshot(
        bids=[[60, 20]],
        asks=[[65, 10], [67, 15], [70, 20]]
    )

    # 1. Successful execution
    res1 = await oms.execute_trade(ticker=ticker, side="yes", count=10, price=0.65)
    assert res1["status"] == "executed"
    assert res1["price_cents"] == 65
    print(f"  ✓ Trade 1 executed successfully: {res1['count']}x @ {res1['price_cents']}¢")

    # 2. Insufficient Liquidity Abort
    res2 = await oms.execute_trade(ticker=ticker, side="yes", count=500, price=0.70)
    assert res2["status"] == "ABORTED_THIN_BOOK"
    print(f"  ✓ Trade 2 thin book abort verified: {res2['reason']}")

    # 3. Model Edge Violation Abort
    # Sweeping 20 contracts needs 10 @ 65¢ + 10 @ 67¢ = VWAP 66¢. If model prob is 66%, net edge after fees is negative!
    res3 = await oms.execute_trade(ticker=ticker, side="yes", count=20, price=0.67, model_prob=0.66, min_net_edge_pct=2.5)
    assert res3["status"] == "ABORTED_INSUFFICIENT_EDGE"
    print(f"  ✓ Trade 3 EV protection abort verified: {res3['reason']}")

    # 4. Stale-Tick Watchdog Halt
    watchdog.record_tick(reported_latency_ms=2500.0) # Simulate stale market feed
    res4 = await oms.execute_trade(ticker=ticker, side="yes", count=5, price=0.65)
    assert res4["status"] == "ABORTED_STALE_TICK"
    print(f"  ✓ Trade 4 Stale-Tick Watchdog abort verified: {res4['reason']}")

    # Reset watchdog for stop loss test
    watchdog.is_halted = False
    watchdog.last_tick_time = asyncio.get_event_loop().time()

    # 5. Limit-chasing stop loss
    sl_res = await oms.execute_stop_loss(ticker=ticker, side="yes", count=10, floor_cents=15)
    assert sl_res["action"] == "sell"
    print(f"  ✓ Limit-chasing stop loss order placed: {sl_res}")

    await client.close()


async def main():
    print("=" * 80)
    print("       KALSHI CLIENT & ASYNC ORDER MANAGER UNIT TEST SUITE")
    print("=" * 80)
    await test_auth_and_env_loading()
    await test_kalshi_client_rest()
    await test_order_book_and_vwap()
    await test_async_order_manager_execution()
    print("\n" + "=" * 80)
    print("       ALL KALSHI CLIENT & OMS TESTS PASSED (100% SUCCESS)")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
