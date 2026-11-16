#!/usr/bin/env python3
"""
Read-only ETH production orderbook depth & spread validator (P0).
Queries Kalshi public production REST API for KXETH15M open markets and L2 orderbook,
verifies bid/ask spread and L1 depth, and writes .eth_prod_book_validated on success.
"""
import json
import sys
import time
from pathlib import Path
import httpx

KALSHI_PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"


def validate_eth_production_book(min_depth: int = 10, max_spread_cents: int = 10) -> dict:
    with httpx.Client(timeout=10.0) as client:
        r = client.get(f"{KALSHI_PROD_BASE}/markets", params={"series_ticker": "KXETH15M", "status": "open"})
        r.raise_for_status()
        markets = r.json().get("markets", [])
        if not markets:
            raise RuntimeError("No open KXETH15M markets found on Kalshi production API")

        m = markets[0]
        ticker = m["ticker"]
        ob_r = client.get(f"{KALSHI_PROD_BASE}/markets/{ticker}/orderbook")
        ob_r.raise_for_status()
        ob_json = ob_r.json()
        if "orderbook_fp" in ob_json:
            ob_fp = ob_json["orderbook_fp"]
            yes_levels = [(int(round(float(p) * 100)), int(round(float(q)))) for p, q in (ob_fp.get("yes_dollars") or [])]
            no_levels = [(int(round(float(p) * 100)), int(round(float(q)))) for p, q in (ob_fp.get("no_dollars") or [])]
        else:
            ob = ob_json.get("orderbook", {})
            yes_levels = [(int(p), int(q)) for p, q in (ob.get("yes") or [])]
            no_levels = [(int(p), int(q)) for p, q in (ob.get("no") or [])]

        best_yes_lvl = max((lvl for lvl in yes_levels if lvl[1] > 0), key=lambda x: x[0], default=(0, 0))
        best_no_lvl = max((lvl for lvl in no_levels if lvl[1] > 0), key=lambda x: x[0], default=(0, 0))
        best_yes_bid, best_yes_bid_qty = best_yes_lvl
        best_no_bid, best_no_bid_qty = best_no_lvl
        best_yes_ask = (100 - best_no_bid) if best_no_bid > 0 else 100
        spread = best_yes_ask - best_yes_bid

        total_yes_depth = sum(lvl[1] for lvl in yes_levels)
        total_no_depth = sum(lvl[1] for lvl in no_levels)

        passed = (
            0 <= spread <= max_spread_cents
            and (total_yes_depth >= min_depth or total_no_depth >= min_depth)
        )

        result = {
            "validated_at": time.time(),
            "ticker": ticker,
            "best_yes_bid": best_yes_bid,
            "best_yes_bid_qty": best_yes_bid_qty,
            "best_yes_ask": best_yes_ask,
            "best_no_bid_qty": best_no_bid_qty,
            "spread_cents": spread,
            "total_yes_depth": total_yes_depth,
            "total_no_depth": total_no_depth,
            "passed": passed,
        }
        return result


def main() -> int:
    try:
        res = validate_eth_production_book()
        print(json.dumps(res, indent=2))
        if res["passed"]:
            Path(".eth_prod_book_validated").write_text(json.dumps(res, indent=2) + "\n")
            print(f"[OK] ETH production book validated for {res['ticker']} (spread={res['spread_cents']}c)")
            return 0
        print(f"[WARN] ETH production book failed threshold checks: {res}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"[ERROR] ETH production book validation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
