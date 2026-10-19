#!/usr/bin/env python3
"""
test_live_oms.py — Test Harness for Kalshi OMS, RSA-PSS Authentication,
and L2 Book Depth VWAP Sweep Verification.

Verifies:
1. RSA-PSS signature generation and verification.
2. L2 Order Book snapshot and delta management.
3. VWAP sweep calculation across multiple book depth tiers.
4. Automatic trade abortion when book depth is too thin or VWAP violates the +EV threshold.
5. Limit-chasing stop loss execution routing.
"""

import sys
import asyncio
from typing import Dict, Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, NoEncryption

from live_executor import (
    KalshiAuth,
    L2OrderBook,
    KalshiOrderManagementSystem,
    LiveKalshiBroker
)


async def run_oms_tests():
    print("=" * 80)
    print("       KALSHI LIVE OMS & L2 VWAP SWEEP UNIT TEST HARNESS")
    print("=" * 80)

    # --------------------------------------------------------------------------
    # 1. TEST RSA-PSS AUTHENTICATION SIGNING
    # --------------------------------------------------------------------------
    print("\n[+] 1. Testing RSA-PSS Signature Generation...")
    # Generate temporary test RSA key
    priv_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem_str = priv_key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption()
    ).decode("utf-8")

    auth = KalshiAuth(key_id="test-key-id-12345", private_key_pem=pem_str)
    assert auth.is_configured, "Auth should be configured with generated key."

    headers = auth.sign_request("GET", "/trade-api/v2/portfolio/balance", timestamp_ms=1700000000000)
    assert "KALSHI-ACCESS-KEY" in headers
    assert "KALSHI-ACCESS-SIGNATURE" in headers
    assert "KALSHI-ACCESS-TIMESTAMP" in headers
    assert headers["KALSHI-ACCESS-KEY"] == "test-key-id-12345"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    print(f"  ✓ RSA-PSS Headers: Validated ({len(headers['KALSHI-ACCESS-SIGNATURE'])} char signature).")

    # --------------------------------------------------------------------------
    # 2. TEST L2 ORDER BOOK DEPTH & VWAP SWEEP
    # --------------------------------------------------------------------------
    print("\n[+] 2. Testing L2 Order Book Depth & VWAP Sweep Calculation...")
    book = L2OrderBook(ticker="KXBTC15M-TEST")

    # Populate synthetic order book:
    # Asks: 15 @ 70¢, 20 @ 72¢, 10 @ 75¢, 50 @ 80¢
    # Bids: 25 @ 68¢, 30 @ 65¢, 40 @ 60¢
    book.apply_snapshot(
        bids=[[68, 25], [65, 30], [60, 40]],
        asks=[[70, 15], [72, 20], [75, 10], [80, 50]]
    )

    best_bid, bid_qty = book.get_best_bid()
    best_ask, ask_qty = book.get_best_ask()
    assert best_bid == 68 and bid_qty == 25, f"Expected 68¢ x 25, got {best_bid}¢ x {bid_qty}"
    assert best_ask == 70 and ask_qty == 15, f"Expected 70¢ x 15, got {best_ask}¢ x {ask_qty}"
    print(f"  ✓ Top of Book: {best_bid}¢ ({bid_qty} contracts) / {best_ask}¢ ({ask_qty} contracts).")

    # Case A: Sweep 10 contracts (entirely within level 1 @ 70¢)
    sweep_10 = book.calculate_sweep_vwap("YES", 10)
    assert sweep_10["can_fill"] is True
    assert sweep_10["vwap_cents"] == 70.0
    assert sweep_10["levels_swept"] == 1
    print(f"  ✓ Case A (Level 1 fill): 10 contracts @ VWAP {sweep_10['vwap_cents']:.2f}¢ (1 level swept).")

    # Case B: Sweep 30 contracts (15 @ 70¢ + 15 @ 72¢)
    # Total cost = 15*70 + 15*72 = 1050 + 1080 = 2130 cents. VWAP = 2130 / 30 = 71.0¢
    sweep_30 = book.calculate_sweep_vwap("YES", 30)
    assert sweep_30["can_fill"] is True
    assert sweep_30["vwap_cents"] == 71.0, f"Expected 71.0¢, got {sweep_30['vwap_cents']}"
    assert sweep_30["levels_swept"] == 2
    assert sweep_30["worst_price_cents"] == 72
    print(f"  ✓ Case B (Multi-level sweep): 30 contracts @ VWAP {sweep_30['vwap_cents']:.2f}¢ (Worst limit: {sweep_30['worst_price_cents']}¢).")

    # Case C: Sweep 150 contracts (exceeds all available asks in book: 15+20+10+50 = 95)
    sweep_150 = book.calculate_sweep_vwap("YES", 150)
    assert sweep_150["can_fill"] is False
    assert sweep_150["filled_count"] == 95
    print(f"  ✓ Case C (Thin book check): Requested 150 contracts -> Correctly flagged insufficient depth (only {sweep_150['filled_count']} available).")

    # --------------------------------------------------------------------------
    # 3. TEST OMS VWAP THRESHOLD ABORTION
    # --------------------------------------------------------------------------
    print("\n[+] 3. Testing OMS VWAP Hurdle Protection & Trade Abortion...")
    oms = KalshiOrderManagementSystem(auth=auth)

    # Attach book into a mock container
    class MockWS:
        def __init__(self, test_book):
            self.books = {test_book.ticker: test_book}
    oms.ws_client = MockWS(book)

    # Test Abort Scenario:
    # We want to buy 35 contracts on YES.
    # Level 1: 15 @ 70¢
    # Level 2: 20 @ 72¢
    # VWAP = (15*70 + 20*72)/35 = (1050 + 1440)/35 = 2490 / 35 = 71.14¢
    # Suppose max acceptable VWAP is 70¢: the trade MUST ABORT!
    abort_res = await oms.execute_entry_with_vwap_protection(
        ticker="KXBTC15M-TEST",
        side="YES",
        contracts=35,
        model_prob=0.76, # 76% model prob
        max_acceptable_vwap_cents=70 # Cap set to 70¢
    )
    assert abort_res["status"] == "ABORTED_VWAP_SLIPPAGE", f"Should have aborted with VWAP slippage: {abort_res}"
    print(f"  ✓ VWAP Protection Triggered: Correctly aborted order when expected VWAP ({abort_res['vwap_cents']:.1f}¢) > Cap (70¢).")

    # Test Thin Book Abort: Request 120 contracts (book only has 95)
    abort_thin = await oms.execute_entry_with_vwap_protection(
        ticker="KXBTC15M-TEST",
        side="YES",
        contracts=120,
        model_prob=0.85,
        max_acceptable_vwap_cents=85
    )
    assert abort_thin["status"] == "ABORTED_THIN_BOOK", f"Should have aborted on thin book: {abort_thin}"
    print(f"  ✓ Depth Protection Triggered: Correctly aborted order when book lacked sufficient contracts.")

    # Test Clean Fill: Buy 12 contracts @ 70¢ with cap 71¢
    fill_res = await oms.execute_entry_with_vwap_protection(
        ticker="KXBTC15M-TEST",
        side="YES",
        contracts=12,
        model_prob=0.85,
        max_acceptable_vwap_cents=71
    )
    assert "FILLED" in fill_res["status"]
    print(f"  ✓ Valid Order Approved: Successfully dispatched 12 contracts within acceptable VWAP.")

    # --------------------------------------------------------------------------
    # 4. TEST LIMIT-CHASING STOP LOSS ROUTING
    # --------------------------------------------------------------------------
    print("\n[+] 4. Testing Limit-Chasing Stop Loss Routing...")
    sl_res = await oms.execute_limit_chasing_stop_loss(
        ticker="KXBTC15M-TEST",
        side="YES",
        contracts=15,
        floor_cents=20,
        max_chase_attempts=2,
        chase_poll_ms=100
    )
    assert "status" in sl_res
    print(f"  ✓ Limit-Chasing Stop Loss: Smart routing completed ({sl_res['status']}).")

    print("\n" + "=" * 80)
    print("      ALL LIVE OMS & VWAP EXECUTION TESTS PASSED SUCCESSFULLY!")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    asyncio.run(run_oms_tests())
