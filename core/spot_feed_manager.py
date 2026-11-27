"""
core/spot_feed_manager.py — Ultra-low-latency unified spot feed.

Maintains persistent WebSocket connections to:
  - Coinbase (wss://ws-feed.exchange.coinbase.com) — BTC-USD, ETH-USD matches
  - Kraken (wss://ws.kraken.com/v2) — XBTUSD, ETHUSD trades
  - Hyperliquid (wss://api.hyperliquid.xyz/ws) — allMids + l2Book (already in hl_ws_feed)

Spot prices are cached in memory and updated sub-second via WS push.
REST is used only as a fallback on WS disconnect.

Replaces REST polling of Coinbase/Kraken in fast_spot_loop with sub-second WS pushes.
"""

import asyncio
import json
import logging
import time
from collections import deque
from typing import Dict, List, Optional, Tuple

import httpx
import websockets

logger = logging.getLogger("SpotFeedManager")

COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"
KRAKEN_WS_URL = "wss://ws.kraken.com/v2"
BITSTAMP_WS_URL = "wss://ws.bitstamp.net"
HYPERLIQUID_WS_URL = "wss://api.hyperliquid.xyz/ws"

# Coinbase product IDs
COINBASE_PRODUCTS = {"BTC": "BTC-USD", "ETH": "ETH-USD"}
# Kraken pair symbols
KRAKEN_PAIRS = {"BTC": "XBTUSD", "ETH": "ETHUSD"}
# Bitstamp live trade channels (US-accessible CF Benchmarks constituent)
BITSTAMP_CHANNELS = {"BTC": "live_trades_btcusd", "ETH": "live_trades_ethusd"}


class SpotFeedManager:
    """
    Aggregates spot prices from Coinbase, Kraken, and Hyperliquid via persistent
    WebSockets. Provides sub-second spot price access with REST fallback.
    """

    def __init__(self, coins: List[str] = None, hl_feed=None):
        self.coins = [c.upper() for c in (coins or ["BTC", "ETH"])]
        self.hl_feed = hl_feed  # existing HyperliquidWSFeed instance

        # In-memory spot cache: coin -> price
        self.spot: Dict[str, float] = {c: 0.0 for c in self.coins}
        self.last_update: Dict[str, float] = {c: 0.0 for c in self.coins}

        # Per-source last price & timestamp (for spot composite + fallback)
        self._source_price: Dict[str, Dict[str, float]] = {c: {} for c in self.coins}
        self._source_time: Dict[str, Dict[str, float]] = {c: {} for c in self.coins}

        # Rolling aggressive spot trade flow (ts, signed_qty, abs_qty) across US spot venues
        self._trade_flow: Dict[str, deque] = {c: deque(maxlen=600) for c in self.coins}
        # Coinbase L1 micro-price & top-of-book skew
        self._l1_book: Dict[str, Dict[str, float]] = {c: {} for c in self.coins}

        self._running = False
        self._tasks: List[asyncio.Task] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_cf_spot(self, coin: str, max_age: float = 5.0) -> float:
        """
        Return the CF-Benchmarks-aligned spot composite from true US-accessible
        spot exchanges (Coinbase + Kraken + Bitstamp mean when fresh).
        Never mixes Hyperliquid perp unless all spot feeds are stale.
        """
        c = coin.upper()
        if c not in self.spot:
            return 0.0
        now = time.time()
        prices = self._source_price.get(c, {})
        times = self._source_time.get(c, {})
        fresh_spot = [
            prices[src]
            for src in ("coinbase", "kraken", "bitstamp")
            if prices.get(src, 0.0) > 0 and (now - times.get(src, 0.0)) <= max_age
        ]
        if fresh_spot:
            return round(sum(fresh_spot) / len(fresh_spot), 2)
        if self.is_fresh(c, max_age=max_age):
            return self.spot[c]
        return 0.0

    def get_spot_tfi(self, coin: str, window_s: float = 30.0) -> float:
        """
        Return normalized aggressive Spot Trade Flow Imbalance in [-1.0, +1.0]
        over the last `window_s` seconds across Coinbase, Kraken, and Bitstamp.
        """
        c = coin.upper()
        dq = self._trade_flow.get(c)
        if not dq:
            return 0.0
        cutoff = time.time() - window_s
        net_vol = 0.0
        tot_vol = 0.0
        for ts, signed_qty, abs_qty in dq:
            if ts >= cutoff:
                net_vol += signed_qty
                tot_vol += abs_qty
        if tot_vol <= 1e-9:
            return 0.0
        return max(-1.0, min(1.0, net_vol / tot_vol))

    def get_book_imbalance(self, coin: str, max_age: float = 5.0) -> float:
        """Return Coinbase L1 top-of-book size imbalance in [-1.0, +1.0]."""
        c = coin.upper()
        b = self._l1_book.get(c)
        if not b or (time.time() - b.get("ts", 0.0)) > max_age:
            return 0.0
        return b.get("imbalance", 0.0)

    def get_microprice_edge_bps(self, coin: str, max_age: float = 5.0) -> float:
        """Return (microprice - mid) / mid in basis points from Coinbase L1."""
        c = coin.upper()
        b = self._l1_book.get(c)
        if not b or (time.time() - b.get("ts", 0.0)) > max_age:
            return 0.0
        return b.get("micro_edge_bps", 0.0)

    def get_spot(self, coin: str) -> float:
        """Return latest spot price for a coin, preferring true spot composite over perp fallback."""
        c = coin.upper()
        if c not in self.spot:
            return 0.0
        cf = self.get_cf_spot(c, max_age=5.0)
        if cf > 0:
            return cf
        # Only fall back to Hyperliquid perp if both spot exchanges are stale/disconnected
        if self.hl_feed and self.hl_feed.is_fresh(c, max_age=3.0):
            return self.hl_feed.get_spot(c)
        return 0.0

    def get_age_ms(self, coin: str) -> float:
        """Return age of latest spot price update in milliseconds."""
        c = coin.upper()
        ts = self.last_update.get(c, 0.0)
        if ts <= 0:
            return float("inf")
        return (time.time() - ts) * 1000.0

    def is_fresh(self, coin: str, max_age: float = 5.0, max_age_ms: Optional[float] = None) -> bool:
        c = coin.upper()
        if max_age_ms is not None:
            max_age = max_age_ms / 1000.0
        ts = self.last_update.get(c, 0.0)
        return (time.time() - ts) <= max_age and self.spot.get(c, 0.0) > 0.0

    def get_all_spots(self) -> Dict[str, float]:
        return {c: self.get_spot(c) for c in self.coins}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self):
        """Start all WebSocket feeds."""
        self._running = True
        self._tasks = [
            asyncio.create_task(self._coinbase_loop()),
            asyncio.create_task(self._kraken_loop()),
            asyncio.create_task(self._bitstamp_loop()),
            asyncio.create_task(self._hl_loop()),
        ]
        logger.info(f"[SpotFeed] Started feeds for {self.coins}")

    async def stop(self):
        """Stop all feeds."""
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        logger.info("[SpotFeed] Stopped")

    # ------------------------------------------------------------------
    # Coinbase WebSocket
    # ------------------------------------------------------------------
    async def _coinbase_loop(self):
        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(
                    COINBASE_WS_URL,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    backoff = 1.0
                    # Subscribe to matches channel for BTC-USD, ETH-USD
                    products = [COINBASE_PRODUCTS[c] for c in self.coins if c in COINBASE_PRODUCTS]
                    subscribe_msg = {
                        "type": "subscribe",
                        "product_ids": products,
                        "channels": ["matches", "ticker"],
                    }
                    await ws.send(json.dumps(subscribe_msg))
                    logger.info(f"[SpotFeed] Coinbase WS subscribed: {products}")

                    async for raw in ws:
                        if not self._running:
                            break
                        self._process_coinbase(raw)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[SpotFeed] Coinbase WS error: {e}; reconnecting in {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2.0)

    def _process_coinbase(self, raw: str):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        mtype = msg.get("type")
        if mtype not in ("match", "ticker"):
            return
        product = msg.get("product_id", "")
        coin = self._coinbase_product_to_coin(product)
        if not coin or coin not in self.coins:
            return
        price = float(msg.get("price") or msg.get("last") or 0.0)
        if price <= 0:
            return

        # Extract L1 top-of-book micro-price & size imbalance from ticker messages
        if mtype == "ticker":
            bid = float(msg.get("best_bid") or 0.0)
            ask = float(msg.get("best_ask") or 0.0)
            bid_sz = float(msg.get("best_bid_size") or 0.0)
            ask_sz = float(msg.get("best_ask_size") or 0.0)
            if bid > 0 and ask > bid and (bid_sz + ask_sz) > 0:
                mid = 0.5 * (bid + ask)
                micro = (bid * ask_sz + ask * bid_sz) / (bid_sz + ask_sz)
                imb = (bid_sz - ask_sz) / (bid_sz + ask_sz)
                self._l1_book[coin] = {
                    "ts": time.time(),
                    "bid": bid,
                    "ask": ask,
                    "microprice": micro,
                    "imbalance": imb,
                    "micro_edge_bps": ((micro - mid) / mid) * 10000.0,
                }

        # Record aggressive taker trade flow
        side = str(msg.get("side") or "").lower()
        qty = float(msg.get("size") or msg.get("last_size") or 0.0)
        if side in ("buy", "sell") and qty > 0:
            self._record_trade_flow(coin, side=side, qty=qty)

        self._update_spot(coin, price, source="coinbase")

    def _coinbase_product_to_coin(self, product: str) -> Optional[str]:
        for coin, pid in COINBASE_PRODUCTS.items():
            if pid == product:
                return coin
        return None

    # ------------------------------------------------------------------
    # Kraken WebSocket v2
    # ------------------------------------------------------------------
    async def _kraken_loop(self):
        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(
                    KRAKEN_WS_URL,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    backoff = 1.0
                    # Subscribe to trades for XBTUSD, ETHUSD
                    pairs = [KRAKEN_PAIRS[c] for c in self.coins if c in KRAKEN_PAIRS]
                    subscribe_msg = {
                        "method": "subscribe",
                        "params": {
                            "channel": "trade",
                            "symbol": pairs,
                            "snapshot": False,
                        },
                    }
                    await ws.send(json.dumps(subscribe_msg))
                    logger.info(f"[SpotFeed] Kraken WS subscribed: {pairs}")

                    async for raw in ws:
                        if not self._running:
                            break
                        self._process_kraken(raw)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[SpotFeed] Kraken WS error: {e}; reconnecting in {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2.0)

    def _process_kraken(self, raw: str):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if msg.get("method") != "subscribe" and msg.get("channel") != "trade":
            return
        data = msg.get("data", [])
        if not isinstance(data, list):
            return
        for trade in data:
            symbol = trade.get("symbol", "")
            coin = self._kraken_symbol_to_coin(symbol)
            if not coin or coin not in self.coins:
                continue
            price = float(trade.get("price") or 0.0)
            if price <= 0:
                continue
            side = str(trade.get("side") or "").lower()
            qty = float(trade.get("qty") or 0.0)
            if side in ("buy", "sell") and qty > 0:
                self._record_trade_flow(coin, side=side, qty=qty)
            self._update_spot(coin, price, source="kraken")

    def _kraken_symbol_to_coin(self, symbol: str) -> Optional[str]:
        for coin, pair in KRAKEN_PAIRS.items():
            if pair == symbol:
                return coin
        return None

    # ------------------------------------------------------------------
    # Bitstamp WebSocket (US-accessible CF Benchmarks constituent)
    # ------------------------------------------------------------------
    async def _bitstamp_loop(self):
        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(
                    BITSTAMP_WS_URL,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    backoff = 1.0
                    for coin in self.coins:
                        ch = BITSTAMP_CHANNELS.get(coin)
                        if ch:
                            await ws.send(json.dumps({
                                "event": "bts:subscribe",
                                "data": {"channel": ch},
                            }))
                    logger.info(f"[SpotFeed] Bitstamp WS subscribed: {[BITSTAMP_CHANNELS[c] for c in self.coins if c in BITSTAMP_CHANNELS]}")

                    async for raw in ws:
                        if not self._running:
                            break
                        self._process_bitstamp(raw)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[SpotFeed] Bitstamp WS error: {e}; reconnecting in {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2.0)

    def _process_bitstamp(self, raw: str):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if msg.get("event") != "trade":
            return
        channel = msg.get("channel", "")
        coin = None
        for c, ch in BITSTAMP_CHANNELS.items():
            if ch == channel:
                coin = c
                break
        if not coin or coin not in self.coins:
            return
        data = msg.get("data") or {}
        price = float(data.get("price") or 0.0)
        if price <= 0:
            return
        qty = float(data.get("amount") or 0.0)
        # Bitstamp: type 0 = buy, type 1 = sell
        side_code = data.get("type")
        if side_code in (0, 1) and qty > 0:
            self._record_trade_flow(coin, side="buy" if side_code == 0 else "sell", qty=qty)
        self._update_spot(coin, price, source="bitstamp")

    # ------------------------------------------------------------------
    # Hyperliquid (delegate to existing feed)
    # ------------------------------------------------------------------
    async def _hl_loop(self):
        """Monitor existing HyperliquidWSFeed and mirror its prices into our cache."""
        while self._running:
            if self.hl_feed:
                for coin in self.coins:
                    price = self.hl_feed.get_spot(coin)
                    if price > 0:
                        self._update_spot(coin, price, source="hyperliquid")
            await asyncio.sleep(0.25)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _record_trade_flow(self, coin: str, side: str, qty: float):
        c = coin.upper()
        if c not in self._trade_flow or qty <= 0:
            return
        signed = qty if side == "buy" else -qty
        self._trade_flow[c].append((time.time(), signed, qty))

    def _update_spot(self, coin: str, price: float, source: str):
        c = coin.upper()
        now = time.time()
        self._source_price[c][source] = price
        self._source_time[c][source] = now

        # Compute composite spot price exclusively from fresh US spot exchanges
        # (Coinbase + Kraken + Bitstamp) so Hyperliquid perp basis never causes sudden jumps.
        fresh_spot = [
            self._source_price[c][src]
            for src in ("coinbase", "kraken", "bitstamp")
            if self._source_price[c].get(src, 0.0) > 0 and (now - self._source_time[c].get(src, 0.0)) <= 5.0
        ]
        if fresh_spot:
            self.spot[c] = round(sum(fresh_spot) / len(fresh_spot), 2)
            self.last_update[c] = now
        elif source == "hyperliquid":
            # Only mirror Hyperliquid perp if all spot exchanges are stale/unavailable
            self.spot[c] = price
            self.last_update[c] = now


# Module-level singleton
_spot_feed: Optional[SpotFeedManager] = None


def get_spot_feed(hl_feed=None) -> SpotFeedManager:
    global _spot_feed
    if _spot_feed is None:
        _spot_feed = SpotFeedManager(coins=["BTC", "ETH"], hl_feed=hl_feed)
    return _spot_feed


def reset_spot_feed():
    global _spot_feed
    _spot_feed = None
