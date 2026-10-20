#!/usr/bin/env python3
"""
verify_demo_connection.py — Live handshake test against Kalshi Demo API & WebSocket.
"""

import os
import sys
import asyncio
from dotenv import load_dotenv

# Load .env file
load_dotenv("/home/jooshoo/kalshi_scanner/.env")

from kalshi_client import (
    KalshiAuth,
    KalshiClient,
    AsyncOrderManager,
    DEFAULT_DEMO_REST_URL,
    DEFAULT_DEMO_WS_URL
)

async def test_live_demo():
    print("=" * 80)
    print("          KALSHI DEMO API & WEBSOCKET LIVE CONNECTIVITY TEST")
    print("=" * 80)

    auth = KalshiAuth.from_env()
    print(f"[*] API Key ID: {auth.key_id}")
    print(f"[*] Key Configured: {auth.is_configured}")

    if not auth.is_configured:
        print("[!] Error: RSA private key or key ID not loaded.")
        sys.exit(1)

    # 1. Test REST Authentication & Balance
    print("\n[+] 1. Testing REST API connection (/portfolio/balance)...")
    client = KalshiClient(auth=auth, base_url=DEFAULT_DEMO_REST_URL, dry_run=False)

    bal = await client.get_balance()
    print(f"  ✓ REST Response: {bal}")

    # 2. Test Positions
    print("\n[+] 2. Testing REST API positions (/portfolio/positions)...")
    positions = await client.get_positions()
    print(f"  ✓ Positions Response: {len(positions)} positions found.")
    for p in positions[:5]:
        print(f"    - {p}")

    # 3. Test WebSocket Handshake
    print("\n[+] 3. Testing WebSocket Connection to Demo Exchange...")
    print(f"[*] Connecting to: {DEFAULT_DEMO_WS_URL}")
    oms = AsyncOrderManager(client=client, ws_url=DEFAULT_DEMO_WS_URL, dry_run=False)

    # Start listener task and run for 5 seconds to test handshake and ping
    ws_task = asyncio.create_task(oms.connect_and_listen())
    await asyncio.sleep(4.0)

    print(f"  ✓ WebSocket Connected State: {oms.is_ws_connected}")
    if oms.is_ws_connected:
        print("  ✓ Live WebSocket connection established and authenticated successfully!")
        
        # Test subscribing to a sample KXBTC ticker
        test_ticker = "KXBTC15M-26OCT19-T68500"
        print(f"[*] Subscribing to test ticker: {test_ticker}")
        await oms.subscribe(test_ticker)
        await asyncio.sleep(2.0)

    oms.stop()
    ws_task.cancel()
    try:
        await ws_task
    except asyncio.CancelledError:
        pass

    await client.close()
    print("\n" + "=" * 80)
    print("       DEMO ENVIRONMENT VERIFICATION COMPLETED")
    print("=" * 80)

if __name__ == "__main__":
    asyncio.run(test_live_demo())
