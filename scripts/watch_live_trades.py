#!/usr/bin/env python3
"""
scripts/watch_live_trades.py — Monitor the live bot API and alert on new trades.

Polls /api/multicoin/status and /api/live/status every 2 seconds.
Prints a loud alert when a new trade is detected, and shows real-time
equity/PnL/trade count. Run in a separate terminal while the bot is live.

Usage:
    python3 scripts/watch_live_trades.py [--base-url http://localhost:8088]
"""

import argparse
import json
import signal
import sys
import time
from datetime import datetime, timezone

import httpx


def now_str():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def alert(msg: str):
    """Print a high-visibility alert."""
    print("\n" + "!" * 70)
    print(f"[{now_str()}]  🚨  {msg}")
    print("!" * 70 + "\n", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Watch live bot for new trades")
    parser.add_argument("--base-url", default="http://localhost:8088", help="Dashboard base URL")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Poll interval in seconds")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    print(f"[{now_str()}] Watching {base} for new trades (Ctrl-C to stop)...", flush=True)

    last_trade_id = None
    last_trade_count = None
    last_equity = None
    last_pnl = None
    client = httpx.Client(timeout=5.0)

    def get_json(path):
        try:
            r = client.get(base + path, timeout=5.0)
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            print(f"[{now_str()}] Request error for {path}: {e}", flush=True)
        return None

    try:
        while True:
            mc = get_json("/api/multicoin/status")
            live = get_json("/api/live/status")

            if mc:
                trade_count = mc.get("total_trades", 0)
                equity = mc.get("total_equity", 0.0)
                pnl = mc.get("total_pnl", 0.0)
                win_rate = mc.get("overall_win_rate_pct", 0.0)
                active_positions = mc.get("active_positions", {})

                # Equity/PnL change alert (even without new trade count)
                if last_equity is not None and abs(equity - last_equity) > 0.005:
                    delta = equity - last_equity
                    alert(f"EQUITY MOVED: ${last_equity:.2f} -> ${equity:.2f} (Δ${delta:+.2f})")
                if last_pnl is not None and abs(pnl - last_pnl) > 0.005:
                    delta = pnl - last_pnl
                    alert(f"PnL MOVED: ${last_pnl:+.2f} -> ${pnl:+.2f} (Δ${delta:+.2f})")

                # New trade count alert
                if last_trade_count is not None and trade_count > last_trade_count:
                    new_trades = trade_count - last_trade_count
                    alert(f"NEW TRADE(S): +{new_trades} trade(s)! Total trades: {trade_count}  Equity: ${equity:.2f}  PnL: ${pnl:+.2f}  Win rate: {win_rate:.1f}%")
                    if active_positions:
                        for coin, pos in active_positions.items():
                            print(f"    ACTIVE: {coin} {pos.get('side','?')} {pos.get('contracts','?')}x @ {pos.get('entry_price','?')}¢  ROI: {pos.get('roi_pct','?')}%", flush=True)

                # Show heartbeat every 30 seconds
                if int(time.time()) % 30 < 2:
                    ws = live.get("ws_connected", False) if live else False
                    armed = live.get("armed_for_live_trading", False) if live else False
                    print(f"[{now_str()}] Heartbeat: trades={trade_count} equity=${equity:.2f} pnl=${pnl:+.2f} ws={ws} armed={armed}", flush=True)

                last_trade_count = trade_count
                last_equity = equity
                last_pnl = pnl

            if live:
                ws_connected = live.get("ws_connected", False)
                if not ws_connected:
                    alert("WS FEED DOWN — no market data!")

            time.sleep(args.poll_interval)

    except KeyboardInterrupt:
        print(f"\n[{now_str()}] Watcher stopped.", flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
