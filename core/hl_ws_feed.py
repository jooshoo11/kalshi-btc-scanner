"""
core/hl_ws_feed.py — Persistent Hyperliquid L2 WebSocket & 5-Level OFI Stream.

Replaces 1-second REST polling of Hyperliquid `/info allMids` with a persistent
WebSocket (`wss://api.hyperliquid.xyz/ws`) subscribing to `allMids` and `l2Book`
for BTC and ETH, computing real-time 5-level Cont-Kukanov-Stoikov Order Flow
Imbalance (OFI) and rolling Z-scores.
"""

import asyncio
import json
import logging
import math
import time
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

logger = logging.getLogger("HyperliquidWS")

HL_WS_URL = "wss://api.hyperliquid.xyz/ws"


class HyperliquidWSFeed:
    """
    Async WebSocket client for Hyperliquid spot/perp mid-prices and L2 Order Flow Imbalance.
    """

    def __init__(self, coins: Optional[List[str]] = None, ws_url: str = HL_WS_URL):
        self.coins = [c.upper() for c in (coins or ["BTC", "ETH"])]
        self.ws_url = ws_url
        self.mids: Dict[str, float] = {}
        self.last_update_ts: Dict[str, float] = {}
        self.prev_books: Dict[str, Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]] = {}
        self.ofi_history: Dict[str, Deque[float]] = {c: deque(maxlen=60) for c in self.coins}
        self.ofi_latest_z: Dict[str, float] = {c: 0.0 for c in self.coins}
        self.connected: bool = False
        self._running: bool = False

    def is_fresh(self, coin: str, max_age: float = 3.0) -> bool:
        ts = self.last_update_ts.get(coin.upper(), 0.0)
        return (time.time() - ts) <= max_age and self.mids.get(coin.upper(), 0.0) > 0.0

    def get_spot(self, coin: str) -> float:
        return self.mids.get(coin.upper(), 0.0)

    def get_ofi_z(self, coin: str) -> float:
        return self.ofi_latest_z.get(coin.upper(), 0.0)

    def process_message(self, raw_msg: str) -> None:
        """Parse incoming Hyperliquid WS message and update mids / 5-level OFI."""
        try:
            msg = json.loads(raw_msg)
        except Exception:
            return

        channel = msg.get("channel")
        data = msg.get("data")
        now = time.time()

        if channel == "allMids" and isinstance(data, dict):
            mids = data.get("mids", {})
            for coin in self.coins:
                val = mids.get(coin)
                if val is not None:
                    try:
                        self.mids[coin] = float(val)
                        self.last_update_ts[coin] = now
                    except (TypeError, ValueError):
                        pass

        elif channel == "l2Book" and isinstance(data, dict):
            coin = str(data.get("coin", "")).upper()
            if coin not in self.coins:
                return
            levels = data.get("levels", [])
            if not isinstance(levels, list) or len(levels) < 2:
                return

            bids = [
                (float(lv["px"]), float(lv["sz"]))
                for lv in (levels[0] or [])[:5]
                if "px" in lv and "sz" in lv
            ]
            asks = [
                (float(lv["px"]), float(lv["sz"]))
                for lv in (levels[1] or [])[:5]
                if "px" in lv and "sz" in lv
            ]
            if bids and asks:
                self.mids[coin] = (bids[0][0] + asks[0][0]) / 2.0
                self.last_update_ts[coin] = now
                self._update_ofi(coin, bids, asks)

    def _update_ofi(
        self,
        coin: str,
        bids: List[Tuple[float, float]],
        asks: List[Tuple[float, float]],
    ) -> float:
        """
        Compute 5-level exponentially weighted Cont-Kukanov-Stoikov OFI:
        OFI_t = sum_{l=1..5} exp(-0.5 * (l - 1)) * (delta_bid_l - delta_ask_l)
        """
        prev = self.prev_books.get(coin)
        self.prev_books[coin] = (bids, asks)
        if prev is None:
            return 0.0

        prev_bids, prev_asks = prev
        ofi_val = 0.0
        max_levels = min(5, len(bids), len(asks), len(prev_bids), len(prev_asks))
        for lvl in range(max_levels):
            weight = math.exp(-0.5 * lvl)
            b_px, b_sz = bids[lvl]
            pb_px, pb_sz = prev_bids[lvl]
            if b_px > pb_px:
                d_bid = b_sz
            elif b_px < pb_px:
                d_bid = -pb_sz
            else:
                d_bid = b_sz - pb_sz

            a_px, a_sz = asks[lvl]
            pa_px, pa_sz = prev_asks[lvl]
            if a_px < pa_px:
                d_ask = a_sz
            elif a_px > pa_px:
                d_ask = -pa_sz
            else:
                d_ask = a_sz - pa_sz

            ofi_val += weight * (d_bid - d_ask)

        hist = self.ofi_history.setdefault(coin, deque(maxlen=60))
        hist.append(ofi_val)
        if len(hist) >= 10:
            mean_ofi = sum(hist) / len(hist)
            var_ofi = sum((x - mean_ofi) ** 2 for x in hist) / len(hist)
            std_ofi = math.sqrt(var_ofi)
            if std_ofi > 1e-9:
                self.ofi_latest_z[coin] = max(-5.0, min(5.0, (ofi_val - mean_ofi) / std_ofi))
            else:
                self.ofi_latest_z[coin] = 0.0
        return ofi_val

    async def connect_and_listen(self) -> None:
        """Persistent WebSocket loop with exponential backoff reconnect."""
        try:
            import websockets
        except ImportError:
            logger.warning("[HyperliquidWS] websockets package unavailable; REST fallback will be used.")
            return

        self._running = True
        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(
                    self.ws_url, ping_interval=20, ping_timeout=10, close_timeout=5
                ) as ws:
                    self.connected = True
                    backoff = 1.0
                    await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "allMids"}}))
                    for coin in self.coins:
                        await ws.send(
                            json.dumps({"method": "subscribe", "subscription": {"type": "l2Book", "coin": coin}})
                        )
                    logger.info(f"[HyperliquidWS] Connected & subscribed to allMids + l2Book for {self.coins}")
                    async for raw in ws:
                        if not self._running:
                            break
                        self.process_message(raw)
            except asyncio.CancelledError:
                self._running = False
                break
            except Exception as e:
                self.connected = False
                logger.warning(f"[HyperliquidWS] Disconnected ({e}); reconnecting in {backoff:.1f}s")
                await asyncio.sleep(backoff)
                backoff = min(30.0, backoff * 2.0)
        self.connected = False

    def stop(self) -> None:
        self._running = False
