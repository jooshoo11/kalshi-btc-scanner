#!/usr/bin/env python3
"""Standalone tick recorder (read-only; does not touch the trading bot).

Records, per coin (BTC, ETH):
  - Coinbase trades          (WS: wss://ws-feed.exchange.coinbase.com, channel "matches")
  - Kraken trades            (WS v2: wss://ws.kraken.com/v2, channel "trade")
  - Hyperliquid trades + top-5 L2 book (WS: wss://api.hyperliquid.xyz/ws)
  - Kalshi 15M market top-of-book, polled every 1s (public REST, production)

Output: data/ticks/<source>/<YYYY-MM-DD>/<HH>_<runid>.csv  (UTC, plain CSV, flushed every 5s; crash-safe)
Usage:  python3 scripts/tick_recorder.py [--coins BTC,ETH]
"""
import argparse, asyncio, csv, gzip, json, os, time
from datetime import datetime, timezone

import httpx
import websockets

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "ticks")
CB = {"BTC": "BTC-USD", "ETH": "ETH-USD"}
KR = {"BTC": "BTC/USD", "ETH": "ETH/USD"}
KX = {"BTC": "KXBTC15M", "ETH": "KXETH15M"}
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


class Sink:
    """Hourly-rotating gzip CSV writer per source."""
    def __init__(self, source, header):
        self.source, self.header, self.key, self.fh, self.w = source, header, None, None, None

    def write(self, row):
        now = datetime.now(timezone.utc)
        key = now.strftime("%Y-%m-%d/%H")
        if key != self.key:
            if self.fh:
                self.fh.close()
            # Unique file per process start + hour so a killed run never corrupts a later one.
            path = os.path.join(ROOT, self.source, f"{key}_{RUN_ID}.csv")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self.fh = open(path, "w", newline="")
            self.w = csv.writer(self.fh)
            self.w.writerow(self.header)
            self.key = key
        self.w.writerow(row)

    def flush(self):
        if self.fh:
            self.fh.flush()


TRADE_HDR = ["recv_ts", "exch_ts", "coin", "price", "size", "side", "trade_id"]
SINKS = {
    "coinbase": Sink("coinbase", TRADE_HDR),
    "kraken": Sink("kraken", TRADE_HDR),
    "hyperliquid_trades": Sink("hyperliquid_trades", TRADE_HDR),
    "hyperliquid_book": Sink("hyperliquid_book", ["recv_ts", "exch_ts", "coin"]
                             + [f"{s}{i}_{f}" for s in ("bid", "ask") for i in range(5) for f in ("px", "sz")]),
    "kalshi": Sink("kalshi", ["recv_ts", "coin", "ticker", "close_time", "yes_bid", "yes_ask",
                              "no_bid", "no_ask", "last_price", "volume", "open_interest", "floor_strike", "yes_bid_size", "yes_ask_size"]),
}


async def ws_loop(name, url, subscribe, handle):
    backoff = 1
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, max_size=2**23) as ws:
                for m in subscribe:
                    await ws.send(json.dumps(m))
                backoff = 1
                print(f"[{name}] connected", flush=True)
                async for raw in ws:
                    handle(json.loads(raw), time.time())
        except Exception as e:
            print(f"[{name}] error {e!r}; reconnect in {backoff}s", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


def coinbase(coins):
    rev = {CB[c]: c for c in coins}
    def h(m, t):
        if m.get("type") in ("match", "last_match"):
            SINKS["coinbase"].write([t, m.get("time"), rev.get(m["product_id"]), m["price"], m["size"], m["side"], m["trade_id"]])
    return ws_loop("coinbase", "wss://ws-feed.exchange.coinbase.com",
                   [{"type": "subscribe", "product_ids": [CB[c] for c in coins], "channels": ["matches"]}], h)


def kraken(coins):
    rev = {KR[c]: c for c in coins}
    def h(m, t):
        if m.get("channel") == "trade":
            for d in m.get("data", []):
                SINKS["kraken"].write([t, d.get("timestamp"), rev.get(d["symbol"]), d["price"], d["qty"], d["side"], d.get("trade_id")])
    return ws_loop("kraken", "wss://ws.kraken.com/v2",
                   [{"method": "subscribe", "params": {"channel": "trade", "symbol": [KR[c] for c in coins]}}], h)


def hyperliquid(coins):
    subs = []
    for c in coins:
        subs.append({"method": "subscribe", "subscription": {"type": "trades", "coin": c}})
        subs.append({"method": "subscribe", "subscription": {"type": "l2Book", "coin": c}})
    def h(m, t):
        ch, d = m.get("channel"), m.get("data")
        if ch == "trades":
            for x in d:
                SINKS["hyperliquid_trades"].write([t, x["time"], x["coin"], x["px"], x["sz"], "buy" if x["side"] == "B" else "sell", x.get("tid")])
        elif ch == "l2Book":
            row = [t, d["time"], d["coin"]]
            for lvl in d["levels"]:  # [bids, asks]
                for i in range(5):
                    row += [lvl[i]["px"], lvl[i]["sz"]] if i < len(lvl) else ["", ""]
            SINKS["hyperliquid_book"].write(row)
    return ws_loop("hyperliquid", "wss://api.hyperliquid.xyz/ws", subs, h)


async def kalshi_poll(coins):
    async with httpx.AsyncClient(timeout=5) as cli:
        while True:
            t0 = time.time()
            for c in coins:
                try:
                    r = await cli.get(f"{KALSHI}/markets", params={"series_ticker": KX[c], "status": "open"})
                    for mk in r.json().get("markets", []):
                        g = lambda k: mk.get(k + "_dollars", mk.get(k))
                        SINKS["kalshi"].write([t0, c, mk["ticker"], mk.get("close_time"), g("yes_bid"), g("yes_ask"),
                                               g("no_bid"), g("no_ask"), g("last_price"), mk.get("volume_fp", mk.get("volume")),
                                               mk.get("open_interest_fp", mk.get("open_interest")), mk.get("floor_strike"),
                                               mk.get("yes_bid_size_fp"), mk.get("yes_ask_size_fp")])
                except Exception as e:
                    print(f"[kalshi] {c} error {e!r}", flush=True)
            await asyncio.sleep(max(0, 1 - (time.time() - t0)))


async def flusher():
    while True:
        await asyncio.sleep(5)
        for s in SINKS.values():
            s.flush()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coins", default="BTC,ETH")
    coins = ap.parse_args().coins.upper().split(",")
    print(f"Recording {coins} -> {ROOT}", flush=True)
    await asyncio.gather(coinbase(coins), kraken(coins), hyperliquid(coins), kalshi_poll(coins), flusher())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        for s in SINKS.values():
            if s.fh:
                s.fh.close()
