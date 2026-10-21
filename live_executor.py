#!/usr/bin/env python3
"""
live_executor.py — Institutional Order Management System (OMS) & Kalshi V2 WebSocket Client.

Features:
- Kalshi API V2 Authentication: RSA-PSS (SHA-256) signature generation
- Asynchronous WebSocket Client: Real-time L2 orderbook_delta and ticker channels with exponential backoff reconnect
- L2 Order Book Depth & VWAP Sweep Engine: Aborts execution if book sweeping exceeds +EV threshold
- Limit-Chasing Stop Loss: Smart order routing that avoids selling into empty books or sweeping to zero
- Pluggable Broker Architecture: BaseExecutionBroker interface preserving paper trading baseline
"""

import os
import sys
import time
import math
import json
import base64
import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional, Tuple, Callable

import httpx
try:
    import websockets
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
except ImportError as e:
    logging.warning(f"Required crypto/ws libraries: {e}")

from prediction_engine import calculate_kalshi_fee

logger = logging.getLogger("KalshiOMS")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [KalshiOMS] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

KALSHI_PROD_REST_URL = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_PROD_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
KALSHI_DEMO_REST_URL = "https://demo-api.kalshi.co/trade-api/v2"
KALSHI_DEMO_WS_URL = "wss://demo-api.kalshi.co/trade-api/ws/v2"


# ==============================================================================
# 1. KALSHI RSA-PSS V2 AUTHENTICATOR
# ==============================================================================

class KalshiAuth:
    """
    Kalshi API V2 Cryptographic Authentication Handler.
    Signs requests using RSA-PSS with SHA-256 and MGF1.
    """
    def __init__(self, key_id: Optional[str] = None, private_key_pem: Optional[str] = None, private_key_path: Optional[str] = None):
        self.key_id = (key_id or "").strip()
        self._private_key = None

        if private_key_pem:
            self._load_from_pem(private_key_pem.encode("utf-8"))
        elif private_key_path and os.path.exists(private_key_path):
            with open(private_key_path, "rb") as f:
                self._load_from_pem(f.read())

    @classmethod
    def from_env(cls) -> "KalshiAuth":
        """
        Securely load RSA-PSS credentials from environment variables.
        Checks:
          - KALSHI_API_KEY_ID or KALSHI_KEY_ID
          - KALSHI_PRIVATE_KEY_PEM (unescapes newlines like '\\n')
          - KALSHI_PRIVATE_KEY_PATH (path to private key file)
        """
        key_id = os.environ.get("KALSHI_API_KEY_ID") or os.environ.get("KALSHI_KEY_ID") or ""
        pem_content = os.environ.get("KALSHI_PRIVATE_KEY_PEM")
        key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")

        if pem_content:
            formatted_pem = pem_content.replace("\\n", "\n").strip()
            return cls(key_id=key_id, private_key_pem=formatted_pem)
        elif key_path:
            return cls(key_id=key_id, private_key_path=key_path)

        return cls(key_id=key_id)

    def _load_from_pem(self, pem_bytes: bytes):
        try:
            self._private_key = load_pem_private_key(pem_bytes, password=None)
        except Exception as e:
            logger.error(f"Failed to load RSA private key: {e}")

    @property
    def is_configured(self) -> bool:
        return bool(self.key_id and self._private_key)

    def sign_request(self, method: str, path: str, timestamp_ms: Optional[int] = None) -> Dict[str, str]:
        """
        Generate Kalshi V2 authentication headers:
        Message = timestamp_ms + method + path
        Signature = Base64(RSA-PSS(SHA256(Message)))
        """
        if not self.is_configured:
            return {}

        ts = timestamp_ms or int(time.time() * 1000)
        # Remove query parameters if present from signature path
        clean_path = path.split("?")[0]
        if not clean_path.startswith("/trade-api/"):
            if clean_path.startswith("/"):
                clean_path = f"/trade-api/v2{clean_path}"
            else:
                clean_path = f"/trade-api/v2/{clean_path}"
        msg = f"{ts}{method.upper()}{clean_path}".encode("utf-8")

        sig = self._private_key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256()
        )
        b64_sig = base64.b64encode(sig).decode("utf-8")

        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": b64_sig,
            "KALSHI-ACCESS-TIMESTAMP": str(ts),
            "Content-Type": "application/json"
        }


# ==============================================================================
# 2. L2 ORDER BOOK & VWAP SWEEP CALCULATOR
# ==============================================================================

class L2OrderBook:
    """
    In-memory Level-2 Order Book with microsecond VWAP depth sweep calculation.
    Prices are tracked in integer cents [1 - 99].
    """
    def __init__(self, ticker: str):
        self.ticker = ticker
        self.bids: Dict[int, int] = {} # price_cents -> quantity
        self.asks: Dict[int, int] = {} # price_cents -> quantity
        self.last_update_ts: float = 0.0

    def apply_snapshot(self, bids: List[List[int]], asks: List[List[int]]):
        """Initialize book from full snapshot [[price_cents, qty], ...]"""
        self.bids = {int(p): int(q) for p, q in bids if int(q) > 0}
        self.asks = {int(p): int(q) for p, q in asks if int(q) > 0}
        self.last_update_ts = time.time()

    def apply_delta(self, side: str, price_cents: int, delta_qty: int):
        """Update price level from orderbook_delta stream."""
        p = int(price_cents)
        book = self.bids if side.lower() == "yes_bid" or side.lower() == "bid" else self.asks
        new_qty = book.get(p, 0) + int(delta_qty)
        if new_qty <= 0:
            book.pop(p, None)
        else:
            book[p] = new_qty
        self.last_update_ts = time.time()

    def get_best_bid(self) -> Tuple[int, int]:
        """Return (best_bid_cents, qty) or (0, 0)"""
        if not self.bids:
            return 0, 0
        p = max(self.bids.keys())
        return p, self.bids[p]

    def get_best_ask(self) -> Tuple[int, int]:
        """Return (best_ask_cents, qty) or (100, 0)"""
        if not self.asks:
            return 100, 0
        p = min(self.asks.keys())
        return p, self.asks[p]

    def calculate_sweep_vwap(self, side: str, count: int) -> Dict[str, Any]:
        """
        Simulate walking the book to fill `count` contracts:
        - Calculates Volume-Weighted Average Price (VWAP)
        - Computes worst fill price (tail slippage)
        - Checks whether sufficient liquidity exists to fill desired size
        """
        if count <= 0:
            return {"can_fill": False, "vwap_cents": 0.0, "reason": "Invalid count"}

        # For BUY YES: walk asks ascending
        # For SELL YES / BUY NO: walk bids descending
        is_buy_yes = (side.upper() == "YES" or side.upper() == "BUY")
        book = self.asks if is_buy_yes else self.bids
        if not book:
            return {"can_fill": False, "vwap_cents": 0.0, "reason": "Empty order book"}

        sorted_prices = sorted(book.keys()) if is_buy_yes else sorted(book.keys(), reverse=True)

        accumulated_qty = 0
        total_cost_cents = 0
        levels_swept = 0
        worst_price = 0

        for p in sorted_prices:
            available_at_level = book[p]
            needed = count - accumulated_qty
            fill_at_level = min(available_at_level, needed)

            total_cost_cents += fill_at_level * p
            accumulated_qty += fill_at_level
            levels_swept += 1
            worst_price = p

            if accumulated_qty >= count:
                break

        can_fill = (accumulated_qty >= count)
        vwap_cents = (total_cost_cents / float(accumulated_qty)) if accumulated_qty > 0 else 0.0

        return {
            "can_fill": can_fill,
            "vwap_cents": round(vwap_cents, 2),
            "vwap_dollars": round(vwap_cents / 100.0, 4),
            "total_cost_cents": total_cost_cents,
            "total_cost_dollars": round(total_cost_cents / 100.0, 2),
            "filled_count": accumulated_qty,
            "requested_count": count,
            "worst_price_cents": worst_price,
            "levels_swept": levels_swept
        }


# ==============================================================================
# 3. ASYNC KALSHI V2 WEBSOCKET CLIENT
# ==============================================================================

class KalshiWebSocketClient:
    """
    Production-grade WebSocket connection to Kalshi Trade API v2.
    Streams orderbook_delta and ticker channels with auto-reconnection.
    """
    def __init__(self, ws_url: str = KALSHI_PROD_WS_URL, auth: Optional[KalshiAuth] = None):
        self.ws_url = ws_url
        self.auth = auth
        self.books: Dict[str, L2OrderBook] = {}
        self.is_running = False
        self._ws = None
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 30.0
        self.subscribed_tickers: set = set()
        self.on_book_update_callbacks: List[Callable[[str, L2OrderBook], None]] = []

    def get_or_create_book(self, ticker: str) -> L2OrderBook:
        if ticker not in self.books:
            self.books[ticker] = L2OrderBook(ticker)
        return self.books[ticker]

    async def connect_and_listen(self):
        """Long-running async connection loop with exponential backoff."""
        self.is_running = True
        logger.info(f"Connecting to Kalshi WebSocket: {self.ws_url}")

        while self.is_running:
            try:
                headers = {}
                if self.auth and self.auth.is_configured:
                    headers = self.auth.sign_request("GET", "/trade-api/ws/v2")

                connect_kwargs = {"ping_interval": 20, "ping_timeout": 10}
                import inspect
                if "additional_headers" in inspect.signature(websockets.connect).parameters:
                    connect_kwargs["additional_headers"] = headers
                else:
                    connect_kwargs["extra_headers"] = headers

                async with websockets.connect(self.ws_url, **connect_kwargs) as ws:
                    self._ws = ws
                    self._reconnect_delay = 1.0
                    logger.info("Connected to Kalshi WebSocket.")

                    # Resubscribe to tracked tickers
                    if self.subscribed_tickers:
                        await self._send_subscribe(list(self.subscribed_tickers))

                    async for message in ws:
                        await self._handle_message(message)

            except asyncio.CancelledError:
                logger.info("WebSocket connection loop cancelled.")
                break
            except Exception as e:
                logger.warning(f"WebSocket disconnected ({e}). Retrying in {self._reconnect_delay:.1f}s...")
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._max_reconnect_delay, self._reconnect_delay * 1.8)

    async def _send_subscribe(self, tickers: List[str]):
        """Subscribe to orderbook_delta and ticker channels."""
        if not self._ws or not tickers:
            return
        msg = {
            "id": int(time.time() * 1000),
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta", "ticker"],
                "market_tickers": tickers
            }
        }
        await self._ws.send(json.dumps(msg))
        logger.info(f"Subscribed to WebSocket channels for: {tickers}")

    @property
    def is_ws_connected(self) -> bool:
        if self._ws is None:
            return False
        if hasattr(self._ws, "state"):
            return getattr(self._ws.state, "name", "") == "OPEN"
        return getattr(self._ws, "open", not getattr(self._ws, "closed", True))

    async def subscribe_ticker(self, ticker: str):
        self.subscribed_tickers.add(ticker)
        self.get_or_create_book(ticker)
        if self.is_ws_connected:
            await self._send_subscribe([ticker])

    async def subscribe(self, ticker: str):
        """Alias for subscribe_ticker."""
        await self.subscribe_ticker(ticker)

    async def _handle_message(self, raw_msg: str):
        try:
            data = json.loads(raw_msg)
            msg_type = data.get("type")
            msg_data = data.get("msg", {})

            if msg_type == "orderbook_snapshot":
                ticker = msg_data.get("market_ticker")
                if ticker:
                    book = self.get_or_create_book(ticker)
                    raw_yes = msg_data.get("yes_dollars_fp") or msg_data.get("yes") or msg_data.get("bids") or []
                    raw_no = msg_data.get("no_dollars_fp") or msg_data.get("no") or msg_data.get("asks") or []

                    yes_levels = []
                    for item in raw_yes:
                        try:
                            p_cents = int(round(float(item[0]) * 100)) if "." in str(item[0]) else int(item[0])
                            qty = int(round(float(item[1])))
                            if p_cents > 0 and qty > 0:
                                yes_levels.append([p_cents, qty])
                        except Exception:
                            pass

                    # In Kalshi binary structure, NO bids at P are equivalent to YES asks at (100 - P)
                    asks_from_no = []
                    for item in raw_no:
                        try:
                            p_cents = int(round(float(item[0]) * 100)) if "." in str(item[0]) else int(item[0])
                            qty = int(round(float(item[1])))
                            if p_cents > 0 and qty > 0:
                                asks_from_no.append([100 - p_cents, qty])
                        except Exception:
                            pass

                    book.apply_snapshot(yes_levels, asks_from_no)
                    self._notify_book_update(ticker, book)

            elif msg_type == "orderbook_delta":
                ticker = msg_data.get("market_ticker")
                if ticker:
                    book = self.get_or_create_book(ticker)
                    raw_side = str(msg_data.get("side", "")).lower()

                    try:
                        raw_price = msg_data.get("price_dollars", msg_data.get("price", 0))
                        price = int(round(float(raw_price) * 100)) if "." in str(raw_price) else int(raw_price)
                        raw_delta = msg_data.get("delta_fp", msg_data.get("delta", 0))
                        delta = int(round(float(raw_delta)))

                        if raw_side in ("yes", "bid", "yes_bid"):
                            book.apply_delta("yes_bid", price, delta)
                        elif raw_side in ("no", "ask", "yes_ask"):
                            ask_price = (100 - price) if raw_side == "no" else price
                            book.apply_delta("yes_ask", ask_price, delta)
                        self._notify_book_update(ticker, book)
                    except Exception as e:
                        pass

        except Exception as e:
            logger.debug(f"Error parsing WS message: {e}")

    def _notify_book_update(self, ticker: str, book: L2OrderBook):
        for cb in self.on_book_update_callbacks:
            try:
                cb(ticker, book)
            except Exception:
                pass

    def stop(self):
        self.is_running = False
        if self._ws:
            asyncio.create_task(self._ws.close())


# ==============================================================================
# 4. STALE-TICK LATENCY & HEARTBEAT WATCHDOG
# ==============================================================================

class StaleTickWatchdog:
    """
    Stale-Tick Latency & Heartbeat Watchdog.
    Monitors market data ticks from Hyperliquid, Kraken, and Coinbase feeds.
    Halts trading if the timestamp delta between market feeds and system clock exceeds 1500ms.
    Provides auto-recovery when tick latencies stabilize below 800ms for consecutive samples.
    """
    def __init__(
        self,
        max_allowed_delta_ms: float = 1500.0,
        recovery_threshold_ms: float = 800.0,
        recovery_ticks: int = 3
    ):
        self.max_allowed_delta_ms = max_allowed_delta_ms
        self.recovery_threshold_ms = recovery_threshold_ms
        self.recovery_ticks = recovery_ticks
        
        self.is_halted: bool = False
        self.halt_reason: str = ""
        self.last_tick_time: float = time.time()
        self.last_delta_ms: float = 0.0
        self._consecutive_healthy_ticks: int = 0
        self._total_halts_triggered: int = 0

    def record_tick(
        self,
        feed_timestamp: Optional[float] = None,
        reported_latency_ms: Optional[float] = None
    ) -> bool:
        """
        Record incoming feed tick and check for latency staleness.
        - feed_timestamp: UNIX epoch timestamp from exchange.
        - reported_latency_ms: HTTP/WS roundtrip ping time in ms.
        Returns: True if safe to trade, False if halted/stale.
        """
        now = time.time()
        self.last_tick_time = now

        if feed_timestamp and feed_timestamp > 0:
            ts = feed_timestamp if feed_timestamp < 1e11 else feed_timestamp / 1000.0
            delta_ms = abs(now - ts) * 1000.0
        elif reported_latency_ms is not None:
            delta_ms = float(reported_latency_ms)
        else:
            delta_ms = 0.0

        self.last_delta_ms = round(delta_ms, 1)

        if delta_ms > self.max_allowed_delta_ms:
            if not self.is_halted:
                self.is_halted = True
                self._total_halts_triggered += 1
                self.halt_reason = (
                    f"STALE_TICK_DELTA_EXCEEDED: Feed delta {delta_ms:.1f}ms exceeds "
                    f"safety ceiling {self.max_allowed_delta_ms:.0f}ms."
                )
                logger.error(f"[Watchdog Circuit Breaker] TRIPPED! {self.halt_reason}")
            self._consecutive_healthy_ticks = 0
            return False
        else:
            if self.is_halted:
                if delta_ms <= self.recovery_threshold_ms:
                    self._consecutive_healthy_ticks += 1
                    if self._consecutive_healthy_ticks >= self.recovery_ticks:
                        logger.info(
                            f"[Watchdog Circuit Breaker] RECOVERED: Feed latency stabilized at "
                            f"{delta_ms:.1f}ms for {self.recovery_ticks} consecutive ticks. Resuming bot operations."
                        )
                        self.is_halted = False
                        self.halt_reason = ""
                else:
                    self._consecutive_healthy_ticks = 0
            return not self.is_halted

    def check_heartbeat(self) -> bool:
        """Periodic heartbeat check. If no tick arrived in max_allowed_delta_ms, trip halt."""
        now = time.time()
        time_since_last_tick_ms = (now - self.last_tick_time) * 1000.0
        if time_since_last_tick_ms > self.max_allowed_delta_ms:
            if not self.is_halted:
                self.is_halted = True
                self._total_halts_triggered += 1
                self.halt_reason = (
                    f"NO_TICKS_RECEIVED: {time_since_last_tick_ms:.0f}ms elapsed since last market data tick."
                )
                logger.error(f"[Watchdog Circuit Breaker] TRIPPED! {self.halt_reason}")
            return False
        return not self.is_halted

    def is_safe_to_trade(self) -> bool:
        """Safety check combining heartbeat and circuit breaker state."""
        if self.is_halted:
            return False
        return self.check_heartbeat()

    def get_status(self) -> Dict[str, Any]:
        return {
            "is_halted": self.is_halted,
            "status": "HALTED" if self.is_halted else "HEALTHY",
            "halt_reason": self.halt_reason,
            "last_delta_ms": self.last_delta_ms,
            "max_allowed_delta_ms": self.max_allowed_delta_ms,
            "time_since_last_tick_ms": round((time.time() - self.last_tick_time) * 1000.0, 1),
            "total_halts": self._total_halts_triggered
        }


# ==============================================================================
# 5. KALSHI COLD-BOOT STATE RECONCILER
# ==============================================================================

class KalshiStateReconciler:
    """
    Cold-boot state synchronization engine.
    Queries Kalshi REST API (/portfolio/positions and /portfolio/orders) on boot
    and reconciles exchange state against the local SQLite kalshi_audit.db.
    """
    def __init__(self, auth: Optional[KalshiAuth] = None, rest_url: Optional[str] = None, dry_run: bool = False):
        self.auth = auth or KalshiAuth.from_env()
        if rest_url is None:
            env_choice = os.environ.get("KALSHI_ENVIRONMENT", "demo").lower()
            rest_url = KALSHI_PROD_REST_URL if env_choice == "prod" else KALSHI_DEMO_REST_URL
        self.rest_url = rest_url.rstrip("/")
        self.dry_run = dry_run

    async def fetch_exchange_positions(self) -> List[Dict[str, Any]]:
        """Query GET /portfolio/positions."""
        if self.dry_run or not self.auth or not self.auth.is_configured or self.auth.key_id.startswith("test-"):
            return []

        endpoint = "/portfolio/positions"
        headers = self.auth.sign_request("GET", endpoint)
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{self.rest_url}{endpoint}", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("market_positions") or data.get("positions") or []
            else:
                logger.error(f"[Reconciler] Failed to fetch positions ({resp.status_code}): {resp.text}")
                return []

    async def fetch_resting_orders(self) -> List[Dict[str, Any]]:
        """Query GET /portfolio/orders?status=resting."""
        if self.dry_run or not self.auth or not self.auth.is_configured or self.auth.key_id.startswith("test-"):
            return []

        endpoint = "/portfolio/orders"
        headers = self.auth.sign_request("GET", endpoint)
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{self.rest_url}{endpoint}?status=resting", headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("orders") or []
            else:
                logger.error(f"[Reconciler] Failed to fetch orders ({resp.status_code}): {resp.text}")
                return []

    async def cancel_order(self, order_id: str, market_ticker: Optional[str] = None, exchange_index: int = 2) -> bool:
        """Cancel an open resting order on exchange."""
        if self.dry_run or not self.auth or not self.auth.is_configured or self.auth.key_id.startswith("test-"):
            return True
        endpoint = f"/portfolio/events/orders/{order_id}"
        headers = self.auth.sign_request("DELETE", endpoint)
        params = {"exchange_index": exchange_index}
        if market_ticker:
            params["market_ticker"] = market_ticker
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.delete(f"{self.rest_url}{endpoint}", headers=headers, params=params)
            return resp.status_code in (200, 204)

    async def reconcile_state_on_startup(
        self,
        db_path_or_db: Any = "kalshi_audit.db",
        cancel_resting_orders: bool = True
    ) -> Dict[str, Any]:
        """
        Cold-boot safety sequence executed before the WebSocket listener starts:
        1. Fetch all open orders (/portfolio/orders with status=resting).
        2. Issue batch or individual cancel requests for all resting orders.
        3. Fetch all open positions (/portfolio/positions).
        4. Log any discrepancies between the live positions and the local SQLite state.
        """
        logger.info("[Cold-Boot Reconciler] Starting pre-flight state reconciliation sequence...")

        import sqlite3
        if hasattr(db_path_or_db, "_get_conn"):
            conn = db_path_or_db._get_conn()
            should_close = False
        elif isinstance(db_path_or_db, str):
            conn = sqlite3.connect(db_path_or_db)
            conn.row_factory = sqlite3.Row
            should_close = True
        else:
            raise ValueError("Invalid database instance or path provided to reconciler")

        report: Dict[str, Any] = {
            "timestamp": time.time(),
            "resting_orders_fetched": 0,
            "resting_orders_canceled": [],
            "live_positions_fetched": 0,
            "active_exchange_positions": 0,
            "local_db_open_trades": 0,
            "discrepancies_found": [],
            "reconciled_closed_in_db": [],
            "reconciled_opened_in_db": [],
            "reconciled_count_mismatches": [],
            "status": "SUCCESS"
        }

        try:
            # ------------------------------------------------------------------
            # STEP 1: Fetch all open orders (status=resting)
            # ------------------------------------------------------------------
            logger.info("[Cold-Boot Reconciler] Step 1/4: Fetching open resting orders from Kalshi exchange...")
            resting_orders = await self.fetch_resting_orders()
            report["resting_orders_fetched"] = len(resting_orders)
            logger.info(f"[Cold-Boot Reconciler] Step 1 Complete: Fetched {len(resting_orders)} resting order(s).")

            # ------------------------------------------------------------------
            # STEP 2: Issue cancel requests for all resting orders
            # ------------------------------------------------------------------
            if cancel_resting_orders and resting_orders:
                logger.info(f"[Cold-Boot Reconciler] Step 2/4: Canceling {len(resting_orders)} open resting order(s)...")
                for ord_item in resting_orders:
                    ord_id = ord_item.get("order_id", "")
                    if ord_id:
                        canceled = await self.cancel_order(ord_id)
                        if canceled:
                            report["resting_orders_canceled"].append(ord_id)
                            logger.info(f"[Cold-Boot Reconciler] Successfully canceled resting order: {ord_id}")
                        else:
                            logger.warning(f"[Cold-Boot Reconciler] Failed to cancel resting order: {ord_id}")
            else:
                logger.info("[Cold-Boot Reconciler] Step 2 Complete: No resting orders require cancellation.")

            # ------------------------------------------------------------------
            # STEP 3: Fetch all open positions (/portfolio/positions)
            # ------------------------------------------------------------------
            logger.info("[Cold-Boot Reconciler] Step 3/4: Fetching live portfolio positions from Kalshi exchange...")
            exchange_positions = await self.fetch_exchange_positions()
            report["live_positions_fetched"] = len(exchange_positions)
            report["active_exchange_positions"] = len(exchange_positions)

            # Map active exchange positions by ticker (only positions with > 0 contracts)
            pos_map: Dict[str, Dict[str, Any]] = {}
            for p in exchange_positions:
                tk = p.get("ticker", "")
                contracts = int(p.get("position", 0))
                if tk and contracts > 0:
                    pos_map[tk] = p
            logger.info(f"[Cold-Boot Reconciler] Step 3 Complete: {len(pos_map)} active position(s) on exchange.")

            # ------------------------------------------------------------------
            # STEP 4: Log any discrepancies between live positions and local SQLite state
            # ------------------------------------------------------------------
            logger.info("[Cold-Boot Reconciler] Step 4/4: Cross-referencing live exchange positions with local SQLite state...")
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM trades WHERE status = 'OPEN'")
            db_open_trades = [dict(r) for r in cursor.fetchall()]
            report["local_db_open_trades"] = len(db_open_trades)

            db_tickers = {t["ticker"] for t in db_open_trades}

            # Case 4A: Trade open in SQLite but closed/settled on exchange
            for db_trade in db_open_trades:
                trade_id = db_trade["id"]
                ticker = db_trade["ticker"]
                contracts = int(db_trade["contracts"])
                cost = float(db_trade["cost_dollars"])

                if ticker not in pos_map:
                    msg = (
                        f"[DISCREPANCY: OFFLINE_SETTLEMENT] DB Trade #{trade_id} ({ticker}, {contracts} contracts) "
                        f"is marked OPEN in SQLite, but not found on exchange. Marking CLOSED in SQLite."
                    )
                    logger.warning(msg)
                    report["discrepancies_found"].append(msg)

                    cursor.execute("""
                        UPDATE trades
                        SET exit_price = 0.0,
                            exit_timestamp = ?,
                            exit_reason = 'EXCHANGE_RECONCILED_OFFLINE',
                            pnl_dollars = ?,
                            pnl_pct = -100.0,
                            status = 'CLOSED'
                        WHERE id = ?
                    """, (time.time(), -cost, trade_id))
                    conn.commit()
                    report["reconciled_closed_in_db"].append({"trade_id": trade_id, "ticker": ticker})

                else:
                    # Case 4C: Position exists in both, check contract count match
                    ex_contracts = int(pos_map[ticker].get("position", 0))
                    if ex_contracts != contracts:
                        msg = (
                            f"[DISCREPANCY: COUNT_MISMATCH] Contract count mismatch on {ticker}: "
                            f"SQLite has {contracts}, live exchange has {ex_contracts}. Syncing count in SQLite."
                        )
                        logger.warning(msg)
                        report["discrepancies_found"].append(msg)
                        cursor.execute("UPDATE trades SET contracts = ? WHERE id = ?", (ex_contracts, trade_id))
                        conn.commit()
                        report["reconciled_count_mismatches"].append({
                            "trade_id": trade_id,
                            "ticker": ticker,
                            "db_contracts": contracts,
                            "ex_contracts": ex_contracts
                        })

            # Case 4B: Live position on exchange missing from SQLite
            for ticker, pos in pos_map.items():
                if ticker not in db_tickers:
                    contracts = int(pos.get("position", 0))
                    exposure = float(pos.get("market_exposure", 0.0))
                    entry_p = (exposure / contracts) if contracts > 0 else 0.50
                    msg = (
                        f"[DISCREPANCY: UNTRACKED_POSITION] Live position on {ticker} ({contracts} contracts) "
                        f"found on exchange but missing from SQLite. Syncing to SQLite trades table."
                    )
                    logger.warning(msg)
                    report["discrepancies_found"].append(msg)

                    cursor.execute("""
                        INSERT INTO trades (
                            timestamp, ticker, series, side, entry_price, contracts,
                            cost_dollars, fee_dollars, status, rationale
                        ) VALUES (?, ?, 'KXBTC15M', 'yes', ?, ?, ?, 0.0, 'OPEN', 'RECONCILED_FROM_EXCHANGE')
                    """, (time.time(), ticker, entry_p, contracts, exposure))
                    conn.commit()
                    report["reconciled_opened_in_db"].append({"ticker": ticker, "contracts": contracts})

            if not report["discrepancies_found"]:
                logger.info("[Cold-Boot Reconciler] Step 4 Complete: 0 discrepancies found. Local SQLite and live exchange are 100% in sync.")
            else:
                logger.info(f"[Cold-Boot Reconciler] Step 4 Complete: Resolved {len(report['discrepancies_found'])} discrepancy(ies).")

            logger.info(
                f"[Cold-Boot Reconciler] Complete: Canceled {len(report['resting_orders_canceled'])} order(s), "
                f"Closed {len(report['reconciled_closed_in_db'])} trade(s) in DB, "
                f"Synced {len(report['reconciled_opened_in_db'])} missing position(s) to DB."
            )
            return report

        except Exception as e:
            logger.error(f"[Cold-Boot Reconciler] Error during reconciliation: {e}")
            report["status"] = "ERROR"
            report["error"] = str(e)
            return report
        finally:
            if should_close:
                conn.close()

    async def reconcile_with_db(self, db_path_or_db: Any, cancel_orphaned_orders: bool = True) -> Dict[str, Any]:
        """Backward-compatible alias for reconcile_state_on_startup."""
        return await self.reconcile_state_on_startup(db_path_or_db=db_path_or_db, cancel_resting_orders=cancel_orphaned_orders)


# ==============================================================================
# 6. ORDER MANAGEMENT SYSTEM (OMS) & LIMIT-CHASING ENGINE
# ==============================================================================

class KalshiOrderManagementSystem:
    """
    Order Management System enforcing:
    1. Stale-Tick Watchdog check (blocks execution if market data feed exceeds 1500ms latency)
    2. L2 Book VWAP sweep checks (abort if sweep violates +EV threshold)
    3. Limit-chasing stop loss routing (prevents selling into 1¢ bids or empty books)
    4. REST execution and cancellation
    """
    def __init__(
        self,
        auth: Optional[KalshiAuth] = None,
        rest_url: Optional[str] = None,
        ws_client: Optional[KalshiWebSocketClient] = None,
        watchdog: Optional[StaleTickWatchdog] = None,
        dry_run: bool = False
    ):
        self.auth = auth or KalshiAuth.from_env()
        if rest_url is None:
            env_choice = os.environ.get("KALSHI_ENVIRONMENT", "demo").lower()
            rest_url = KALSHI_PROD_REST_URL if env_choice == "prod" else KALSHI_DEMO_REST_URL
        self.rest_url = rest_url.rstrip("/")
        self.ws_client = ws_client
        self.watchdog = watchdog or StaleTickWatchdog()
        self.reconciler = KalshiStateReconciler(auth=self.auth, rest_url=self.rest_url, dry_run=dry_run)
        self.dry_run = dry_run

    async def execute_trade(
        self,
        ticker: str,
        direction: str,
        target_contracts: int,
        edge_threshold: float,
        model_prob: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        OMS VWAP Execution with Book-Sweeping Protection & Safety Gate:
        1. Inspects local L2 WebSocket cache (orderbook_delta).
        2. Walks down the L2 book (bids if selling, asks if buying) until target_contracts is filled.
        3. Calculates the Volume-Weighted Average Price (VWAP) of that theoretical sweep.
        4. Safety Gate: If the VWAP price eliminates the mathematical edge (i.e. exceeds edge_threshold),
           aborts the trade and logs a VWAP_SLIPPAGE_ABORT.
        """
        # 0. Safety Gate: Stale-Tick Watchdog
        if not self.watchdog.is_safe_to_trade():
            logger.error(f"[OMS] ABORT: Execution blocked by Stale-Tick Watchdog! ({self.watchdog.halt_reason})")
            return {
                "status": "ABORTED_STALE_TICK",
                "reason": self.watchdog.halt_reason or "Market data feed delta exceeded 1500ms safety limit",
                "watchdog": self.watchdog.get_status()
            }

        if target_contracts <= 0:
            return {"status": "ABORTED_INVALID_COUNT", "reason": "Target contracts must be > 0"}

        # 1. Query Local L2 WebSocket Cache
        dir_upper = direction.upper()
        is_buy_no = dir_upper in ("NO", "BUY_NO")
        is_buy_yes = dir_upper in ("BUY", "YES", "BUY_YES")
        is_buy = is_buy_yes or is_buy_no
        side_label = "no" if is_buy_no else "yes"

        book: Optional[L2OrderBook] = None
        if self.ws_client and ticker in self.ws_client.books:
            book = self.ws_client.books[ticker]

        if not book:
            if not self.dry_run and (self.auth and self.auth.is_configured and not self.auth.key_id.startswith("test-")):
                logger.warning(f"[OMS] ABORT: No L2 book in local cache for {ticker}.")
                return {
                    "status": "ABORTED_EMPTY_BOOK",
                    "reason": f"No L2 order book found in WebSocket cache for {ticker}"
                }

        # 2. Walk down L2 book to calculate VWAP
        if book:
            # Buying YES sweeps YES asks ascending; Buying NO or Selling YES sweeps YES bids descending
            order_levels = book.asks if is_buy_yes else book.bids
            if not order_levels:
                logger.warning(f"[OMS] ABORT: L2 book for {ticker} has 0 depth on {'asks' if is_buy_yes else 'bids'}.")
                return {"status": "ABORTED_EMPTY_BOOK", "reason": "Empty order book on requested side"}

            sorted_prices = sorted(order_levels.keys()) if is_buy_yes else sorted(order_levels.keys(), reverse=True)
            accumulated_qty = 0
            total_cost_cents = 0
            worst_price_cents = 0
            levels_swept = 0

            for p in sorted_prices:
                avail = order_levels[p]
                needed = target_contracts - accumulated_qty
                fill = min(avail, needed)
                # For BUY NO, a YES bid at p cents equals a NO ask at (100 - p) cents
                effective_level_price = (100 - p) if is_buy_no else p
                total_cost_cents += fill * effective_level_price
                accumulated_qty += fill
                worst_price_cents = effective_level_price
                levels_swept += 1
                if accumulated_qty >= target_contracts:
                    break

            if accumulated_qty <= 0:
                logger.warning(f"[OMS] ABORT: Order book for {ticker} has 0 depth on requested side.")
                return {
                    "status": "ABORTED_EMPTY_BOOK",
                    "reason": "No resting liquidity on book"
                }
            elif accumulated_qty < target_contracts:
                logger.info(
                    f"[OMS] Dynamic book sizing: Scaling order from {target_contracts} down to {accumulated_qty} contracts to match available liquidity."
                )
                target_contracts = accumulated_qty

            vwap_cents = round(total_cost_cents / float(accumulated_qty), 2)
        else:
            # Synthetic fallback for mock tests without active WS client
            worst_price_cents = int(round(edge_threshold * 100)) if edge_threshold <= 1.0 else int(round(edge_threshold))
            vwap_cents = float(worst_price_cents)
            levels_swept = 1

        vwap_dollars = vwap_cents / 100.0

        # 3. Safety Gate: Evaluate Mathematical Edge vs edge_threshold
        if model_prob is not None:
            # Model probability provided: edge_threshold is the minimum required net edge %
            kalshi_fee = calculate_kalshi_fee(1, vwap_dollars)
            net_edge = model_prob - (vwap_dollars + kalshi_fee)
            min_edge_pct = edge_threshold if edge_threshold > 1.0 else (edge_threshold * 100.0)
            actual_edge_pct = round(net_edge * 100.0, 2)

            if actual_edge_pct < min_edge_pct:
                logger.warning(
                    f"[OMS] VWAP_SLIPPAGE_ABORT: Sweep VWAP ({vwap_cents:.1f}¢) leaves net edge {actual_edge_pct:.1f}% "
                    f"which violates threshold {min_edge_pct:.1f}% on {ticker}."
                )
                return {
                    "status": "VWAP_SLIPPAGE_ABORT",
                    "reason": f"Sweep VWAP {vwap_cents:.1f}¢ leaves net edge {actual_edge_pct:.1f}% < threshold {min_edge_pct:.1f}%",
                    "vwap_cents": vwap_cents,
                    "worst_price_cents": worst_price_cents,
                    "net_edge_pct": actual_edge_pct,
                    "required_edge_pct": min_edge_pct
                }
        else:
            # Price threshold mode: edge_threshold is maximum acceptable price
            max_acceptable_cents = int(round(edge_threshold * 100)) if edge_threshold <= 1.0 else int(round(edge_threshold))
            if is_buy and vwap_cents > max_acceptable_cents:
                logger.warning(
                    f"[OMS] VWAP_SLIPPAGE_ABORT: Sweep VWAP ({vwap_cents:.1f}¢) exceeds "
                    f"edge threshold ({max_acceptable_cents}¢) on {ticker}."
                )
                return {
                    "status": "VWAP_SLIPPAGE_ABORT",
                    "reason": f"Sweep VWAP {vwap_cents:.1f}¢ exceeds edge threshold {max_acceptable_cents}¢",
                    "vwap_cents": vwap_cents,
                    "worst_price_cents": worst_price_cents,
                    "max_acceptable_cents": max_acceptable_cents
                }

        # 4. Dispatch Marketable Limit Order capped at worst sweep price
        logger.info(
            f"[OMS] Executing {target_contracts}x {direction} on {ticker} | "
            f"Sweep VWAP: {vwap_cents:.1f}¢ | Cap Limit: {worst_price_cents}¢ | Levels: {levels_swept}"
        )
        order_res = await self._place_rest_order(
            ticker=ticker,
            action="buy" if is_buy else "sell",
            side=side_label,
            count=target_contracts,
            price_cents=worst_price_cents,
            order_type="limit"
        )
        order_id = order_res.get("order_id") or order_res.get("order", {}).get("order_id")
        if not order_id or order_res.get("status") == "FILLED_SIMULATION":
            order_res["status"] = "FILLED"
            order_res["filled_count"] = target_contracts
            order_res["order_id"] = order_id or f"sim_{int(time.time()*1000)}"
            return order_res

        # Allow matching engine to execute against book
        await asyncio.sleep(0.6)
        order_status = await self._get_order_status(order_id)
        filled_count = order_status.get("fill_count", 0)
        remaining_count = order_status.get("remaining_count", target_contracts - filled_count)
        status_str = order_status.get("status", "").lower()

        # If any contracts remain resting/open, cancel immediately so no orphaned orders linger
        if remaining_count > 0 and status_str in ("resting", "open", "pending", "partially_filled"):
            logger.info(f"[OMS Entry] Cancelling unfilled resting contracts ({remaining_count} resting on book)...")
            cancel_ok = await self._cancel_rest_order(order_id, ticker=ticker)
            await asyncio.sleep(0.8)
            final_status = await self._get_order_status(order_id)
            filled_count = final_status.get("fill_count", filled_count)
            status_str = final_status.get("status", status_str).lower()

        if filled_count <= 0:
            logger.warning(f"[OMS Entry] Order {order_id} received 0 fills on exchange book. Entry canceled cleanly.")
            return {
                "status": "UNFILLED",
                "reason": "Order received 0 fills on orderbook",
                "filled_count": 0
            }

        logger.info(f"[OMS Entry] Order {order_id} confirmed filled {filled_count}/{target_contracts} contracts @ {worst_price_cents}¢.")
        return {
            "status": "FILLED",
            "order_id": order_id,
            "filled_count": filled_count,
            "fill_price_cents": worst_price_cents,
            "ticker": ticker
        }

    async def execute_entry_with_vwap_protection(
        self,
        ticker: str,
        side: str,
        contracts: int,
        model_prob: float,
        max_acceptable_vwap_cents: int,
        min_net_edge_pct: float = 4.0
    ) -> Dict[str, Any]:
        """
        Calculates L2 book sweep VWAP before firing.
        Aborts if book is too thin or VWAP violates the net +EV threshold.
        """
        # 0. Safety Check: Stale-Tick Watchdog
        if not self.watchdog.is_safe_to_trade():
            logger.error(f"[OMS] ABORT: Execution blocked by Stale-Tick Watchdog! ({self.watchdog.halt_reason})")
            return {
                "status": "ABORTED_STALE_TICK",
                "reason": self.watchdog.halt_reason or "Market data feed delta exceeded 1500ms safety limit",
                "watchdog": self.watchdog.get_status()
            }

        # 1. Inspect L2 Order Book Depth
        vwap_res = None
        if self.ws_client and ticker in self.ws_client.books:
            book = self.ws_client.books[ticker]
            vwap_res = book.calculate_sweep_vwap(side, contracts)

        if vwap_res and not vwap_res["can_fill"]:
            avail_q = vwap_res.get("filled_count", 0)
            if avail_q > 0:
                logger.info(
                    f"[OMS] Dynamic book sizing: Scaling order from {contracts} down to {avail_q} contracts to match book depth."
                )
                contracts = avail_q
                vwap_res = book.calculate_sweep_vwap(side, contracts)
            else:
                logger.warning(
                    f"[OMS] ABORT: Insufficient liquidity on {ticker}. Requested {contracts} contracts, book held 0."
                )
                return {
                    "status": "ABORTED_THIN_BOOK",
                    "reason": f"Insufficient book depth: Available 0 < Needed {contracts}"
                }

        effective_vwap_cents = vwap_res["vwap_cents"] if vwap_res else max_acceptable_vwap_cents
        effective_worst_price = vwap_res["worst_price_cents"] if vwap_res else max_acceptable_vwap_cents

        # 2. Check if VWAP violates the +EV threshold
        if effective_vwap_cents > max_acceptable_vwap_cents:
            logger.warning(
                f"[OMS] ABORT: VWAP sweep ({effective_vwap_cents:.1f}¢) exceeds "
                f"maximum acceptable price ({max_acceptable_vwap_cents}¢)."
            )
            return {
                "status": "ABORTED_VWAP_SLIPPAGE",
                "reason": f"VWAP {effective_vwap_cents:.1f}¢ exceeds max {max_acceptable_vwap_cents}¢",
                "vwap_cents": effective_vwap_cents
            }

        # 3. Verify Net Edge Post-VWAP & Kalshi Fee
        vwap_dollars = effective_vwap_cents / 100.0
        fee_per_contract = calculate_kalshi_fee(1, vwap_dollars)
        net_edge = model_prob - (vwap_dollars + fee_per_contract)
        if (net_edge * 100.0) < min_net_edge_pct:
            logger.warning(f"[OMS] ABORT: Post-sweep edge ({net_edge*100:.1f}%) < min {min_net_edge_pct}%.")
            return {
                "status": "ABORTED_INSUFFICIENT_EDGE",
                "reason": f"Post-sweep net edge {net_edge*100:.1f}% below minimum {min_net_edge_pct}%"
            }

        # 4. Dispatch Marketable Limit Order (capped at worst sweep price to prevent rogue fills)
        logger.info(
            f"[OMS] Executing {contracts}x {side} on {ticker} | "
            f"Expected VWAP: {effective_vwap_cents:.1f}¢ | Cap Limit: {effective_worst_price}¢"
        )
        order_res = await self._place_rest_order(
            ticker=ticker,
            action="buy",
            side=side.lower(),
            count=contracts,
            price_cents=effective_worst_price,
            order_type="limit"
        )
        order_id = order_res.get("order_id")
        if not order_id or order_res.get("status") == "FILLED_SIMULATION":
            order_res["filled_count"] = contracts
            return order_res

        # Verify actual fill status on the exchange book
        await asyncio.sleep(0.8)
        order_status = await self._get_order_status(order_id)
        filled_count = order_status.get("fill_count", 0)
        remaining_count = order_status.get("remaining_count", contracts - filled_count)

        # Cancel any resting unfilled contracts immediately so no rogue fills happen
        if remaining_count > 0 and order_status.get("status") == "resting":
            logger.info(f"[OMS Entry] Cancelling unfilled resting contracts ({remaining_count} resting)...")
            await self._cancel_rest_order(order_id)
            await asyncio.sleep(0.2)
            final_status = await self._get_order_status(order_id)
            filled_count = final_status.get("fill_count", filled_count)

        if filled_count <= 0:
            logger.warning(f"[OMS Entry] Order {order_id} received 0 fills on exchange book. Entry aborted.")
            return {
                "status": "UNFILLED",
                "reason": "Order received 0 fills on orderbook",
                "filled_count": 0
            }

        logger.info(f"[OMS Entry] Order {order_id} confirmed filled {filled_count}/{contracts} contracts.")
        return {
            "status": "FILLED",
            "order_id": order_id,
            "filled_count": filled_count,
            "fill_price_cents": effective_worst_price,
            "ticker": ticker
        }

    async def execute_complete_exit(
        self,
        ticker: str,
        side: str,
        contracts: int,
        target_price_cents: int,
        floor_cents: int = 15,
        reason: str = "TAKE_PROFIT",
        max_chase_attempts: int = 6,
        chase_poll_ms: int = 600
    ) -> Dict[str, Any]:
        """
        Guaranteed complete position exit:
        - Never leaves un-exited contracts resting on thin orderbooks.
        - Checks Kalshi V2 fill_count_fp and remaining_count_fp after each attempt.
        - Sweeps available bids down to floor_cents until 100% of contracts are closed.
        """
        logger.info(
            f"[OMS Complete Exit] Initiating guaranteed exit: {contracts} contracts {side} on {ticker} "
            f"({reason} target: {target_price_cents}¢, floor: {floor_cents}¢)"
        )

        remaining = int(contracts)
        total_filled = 0
        total_proceeds_cents = 0
        is_no = (side.upper() == "NO")

        for attempt in range(1, max_chase_attempts + 1):
            if remaining <= 0:
                break

            # Query best bid from book
            best_bid_cents = 0
            if self.ws_client and ticker in self.ws_client.books:
                book = self.ws_client.books[ticker]
                if is_no:
                    best_yes_ask = book.get_best_ask()[0]
                    if best_yes_ask < 100:
                        best_bid_cents = 100 - best_yes_ask
                else:
                    best_bid_cents = book.get_best_bid()[0]

            # Determine limit price for this attempt
            if attempt == 1 and reason == "TAKE_PROFIT":
                limit_p = max(floor_cents, target_price_cents)
            else:
                if best_bid_cents > 0:
                    limit_p = max(floor_cents, min(best_bid_cents, target_price_cents) if reason == "TAKE_PROFIT" else best_bid_cents)
                else:
                    limit_p = floor_cents

            logger.info(
                f"[OMS Complete Exit] Attempt {attempt}/{max_chase_attempts}: "
                f"Selling {remaining}x {side} @ {limit_p}¢ (Book bid: {best_bid_cents}¢)"
            )

            order_res = await self._place_rest_order(
                ticker=ticker,
                action="sell",
                side=side.lower(),
                count=remaining,
                price_cents=limit_p,
                order_type="limit"
            )

            order_id = order_res.get("order_id")
            if not order_id:
                # Mock or simulation
                total_filled = contracts
                total_proceeds_cents = contracts * limit_p
                remaining = 0
                break

            await asyncio.sleep(chase_poll_ms / 1000.0)

            order_status = await self._get_order_status(order_id)
            status_str = order_status.get("status", "").lower()
            filled = order_status.get("fill_count", 0)
            rem = order_status.get("remaining_count", max(0, remaining - filled))

            if filled > 0:
                actual_newly_filled = min(remaining, filled)
                total_filled += actual_newly_filled
                total_proceeds_cents += actual_newly_filled * limit_p
                remaining -= actual_newly_filled
                logger.info(f"[OMS Complete Exit] Order {order_id} filled {actual_newly_filled}x @ {limit_p}¢ ({remaining} remaining)")

            if status_str == "executed" or remaining <= 0:
                logger.info(f"[OMS Complete Exit] All {contracts} contracts fully executed!")
                remaining = 0
                break

            # If not completely filled and still resting, cancel resting portion
            if status_str == "resting" or rem > 0:
                logger.info(f"[OMS Complete Exit] Order {order_id} partially unfilled ({rem} resting). Cancelling to chase...")
                await self._cancel_rest_order(order_id)
                await asyncio.sleep(0.2)
                # Re-check status after cancellation to catch race fills
                final_check = await self._get_order_status(order_id)
                latest_filled = final_check.get("fill_count", filled)
                extra = latest_filled - filled
                if extra > 0 and remaining > 0:
                    extra = min(remaining, extra)
                    total_filled += extra
                    total_proceeds_cents += extra * limit_p
                    remaining -= extra

        # If any remaining after chase loop, place final order at floor
        if remaining > 0:
            logger.warning(f"[OMS Complete Exit] {remaining} contracts remain after chase. Posting final attempt at floor {floor_cents}¢...")
            final_res = await self._place_rest_order(
                ticker=ticker,
                action="sell",
                side=side.lower(),
                count=remaining,
                price_cents=floor_cents,
                order_type="limit"
            )
            final_id = final_res.get("order_id")
            if final_id:
                await asyncio.sleep(0.5)
                f_status = await self._get_order_status(final_id)
                f_filled = f_status.get("fill_count", 0)
                if f_filled > 0:
                    act_f = min(remaining, f_filled)
                    total_filled += act_f
                    total_proceeds_cents += act_f * floor_cents
                    remaining -= act_f
                if f_status.get("status") == "resting":
                    await self._cancel_rest_order(final_id)

        avg_price = (total_proceeds_cents / max(1, total_filled)) if total_filled > 0 else float(target_price_cents)
        return {
            "status": "FILLED" if remaining == 0 else "PARTIAL",
            "filled_contracts": total_filled,
            "remaining_contracts": remaining,
            "avg_price_cents": avg_price,
            "fill_price_dollars": avg_price / 100.0,
            "total_proceeds_dollars": total_proceeds_cents / 100.0
        }

    async def execute_limit_chasing_stop_loss(
        self,
        ticker: str,
        side: str,
        contracts: int,
        floor_cents: int = 15,
        max_chase_attempts: int = 4,
        chase_poll_ms: int = 800
    ) -> Dict[str, Any]:
        """
        Smart Stop Loss routing:
        - Never dumps a raw Market Sell into an illiquid book.
        - Posts an aggressive limit at (current_best_bid - 1¢).
        - Monitors fill. If unfilled and bid drops, re-quotes at new bid down to floor_cents.
        """
        logger.info(f"[OMS] Initiating Limit-Chasing Stop Loss: {contracts} contracts on {ticker} (Floor: {floor_cents}¢)")

        for attempt in range(1, max_chase_attempts + 1):
            # Query best bid from book
            best_bid_cents = 0
            if self.ws_client and ticker in self.ws_client.books:
                best_bid_cents, _ = self.ws_client.books[ticker].get_best_bid()

            if best_bid_cents <= 0:
                best_bid_cents = floor_cents + 5

            target_limit = max(floor_cents, best_bid_cents - 1)
            logger.info(f"[OMS] Chase Attempt {attempt}/{max_chase_attempts}: Posting limit at {target_limit}¢")

            order_res = await self._place_rest_order(
                ticker=ticker,
                action="sell",
                side=side.lower(),
                count=contracts,
                price_cents=target_limit,
                order_type="limit"
            )

            order_id = order_res.get("order_id")
            if not order_id:
                # If mock/dry-run or direct fill
                return order_res

            # Wait to observe fill
            await asyncio.sleep(chase_poll_ms / 1000.0)

            # Check fill status
            order_status = await self._get_order_status(order_id)
            if order_status.get("status") == "executed" or order_status.get("remaining_count", 0) == 0:
                logger.info(f"[OMS] Limit-chasing stop loss filled completely at {target_limit}¢!")
                return {"status": "FILLED", "fill_price_cents": target_limit, "order_id": order_id}

            # If still open, cancel and chase lower if attempt remaining
            if attempt < max_chase_attempts:
                logger.info(f"[OMS] Order {order_id} unfilled. Cancelling to chase market bid...")
                await self._cancel_rest_order(order_id)

        # Final fallback: post at floor
        logger.warning(f"[OMS] Reached maximum chase attempts. Posting final limit at safety floor: {floor_cents}¢")
        return await self._place_rest_order(
            ticker=ticker,
            action="sell",
            side=side.lower(),
            count=contracts,
            price_cents=floor_cents,
            order_type="limit"
        )

    async def _place_rest_order(
        self,
        ticker: str,
        action: str,
        side: str,
        count: int,
        price_cents: int,
        order_type: str = "limit"
    ) -> Dict[str, Any]:
        """REST client dispatch to Kalshi V2 orders endpoint."""
        if self.dry_run or not self.auth or not self.auth.is_configured or self.auth.key_id.startswith("test-"):
            # Dry-run / Mock simulation mode
            logger.info(f"[OMS Simulation] Placed mock order: {action} {count}x {side} on {ticker} @ {price_cents}¢")
            return {
                "status": "FILLED_SIMULATION",
                "order_id": f"mock_ord_{int(time.time()*1000)}",
                "ticker": ticker,
                "price_cents": price_cents,
                "count": count
            }

        endpoint = "/portfolio/events/orders"
        url = f"{self.rest_url}{endpoint}"
        import uuid
        # Kalshi V2 /portfolio/events/orders uses a single YES orderbook ("bid" or "ask" on YES):
        # - BUY YES @ P  -> "bid" @ P
        # - SELL YES @ P -> "ask" @ P
        # - BUY NO @ P   -> "ask" @ (100 - P)
        # - SELL NO @ P  -> "bid" @ (100 - P)
        is_no_side = (side.lower() == "no")
        is_buy_action = (action.lower() == "buy")
        if is_no_side:
            v2_side = "ask" if is_buy_action else "bid"
            v2_price_cents = max(1, min(99, 100 - int(price_cents)))
        else:
            v2_side = "bid" if is_buy_action else "ask"
            v2_price_cents = max(1, min(99, int(price_cents)))

        payload = {
            "ticker": ticker,
            "side": v2_side,
            "type": order_type.lower(),
            "count": str(int(count)),
            "price": f"{v2_price_cents / 100.0:.4f}",
            "time_in_force": "good_till_canceled",
            "client_order_id": str(uuid.uuid4()),
            "self_trade_prevention_type": "taker_at_cross"
        }

        headers = self.auth.sign_request("POST", endpoint)
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code in (200, 201):
                data = resp.json()
                ord_info = data.get("order", {})
                data["order_id"] = ord_info.get("order_id") or data.get("order_id")
                data["status"] = ord_info.get("status", "resting")
                data["ticker"] = ticker
                data["count"] = count
                data["price_cents"] = price_cents
                return data
            else:
                logger.error(f"[OMS Error] Order rejected ({resp.status_code}): {resp.text}")
                return {"status": "REJECTED", "code": resp.status_code, "error": resp.text}

    async def _get_order_status(self, order_id: str) -> Dict[str, Any]:
        """Query status of active order."""
        if self.dry_run or not self.auth or not self.auth.is_configured or self.auth.key_id.startswith("test-") or "mock_" in order_id:
            return {"status": "executed", "remaining_count": 0, "fill_count": 1, "initial_count": 1}

        endpoint = f"/portfolio/orders/{order_id}"
        headers = self.auth.sign_request("GET", endpoint)
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{self.rest_url}{endpoint}", headers=headers)
            if resp.status_code == 200:
                ord_data = resp.json().get("order", {})
                try:
                    rem_fp = float(ord_data.get("remaining_count_fp", ord_data.get("remaining_count", 0)))
                    fill_fp = float(ord_data.get("fill_count_fp", ord_data.get("fill_count", 0)))
                    init_fp = float(ord_data.get("initial_count_fp", ord_data.get("count", 0)))
                    ord_data["remaining_count"] = int(round(rem_fp))
                    ord_data["fill_count"] = int(round(fill_fp))
                    ord_data["initial_count"] = int(round(init_fp))
                except Exception:
                    pass
                return ord_data
            return {"status": "unknown"}

    async def _cancel_rest_order(self, order_id: str, ticker: Optional[str] = None) -> bool:
        """Cancel an open order via Kalshi V2 events API."""
        if self.dry_run or not self.auth or not self.auth.is_configured or self.auth.key_id.startswith("test-") or "mock_" in order_id:
            return True

        endpoint = f"/portfolio/events/orders/{order_id}"
        headers = self.auth.sign_request("DELETE", endpoint)
        params = {"exchange_index": 2}
        if ticker:
            params["market_ticker"] = ticker
        async with httpx.AsyncClient(timeout=4.0) as client:
            try:
                resp = await client.delete(f"{self.rest_url}{endpoint}", headers=headers, params=params)
                if resp.status_code in (200, 204):
                    logger.info(f"[OMS] Successfully canceled resting order {order_id} on exchange.")
                    return True
                else:
                    logger.error(f"[OMS] Order cancellation failed ({resp.status_code}): {resp.text}")
                    return False
            except Exception as e:
                logger.error(f"[OMS] Order cancellation exception for {order_id}: {e}")
                return False

    async def reconcile_state_on_startup(
        self,
        db_path_or_db: Any = "kalshi_audit.db",
        cancel_resting_orders: bool = True
    ) -> Dict[str, Any]:
        """
        Cold-boot safety sequence executed before the WebSocket listener starts:
        1. Fetch all open orders (/portfolio/orders with status=resting).
        2. Issue batch or individual cancel requests for all resting orders.
        3. Fetch all open positions (/portfolio/positions).
        4. Log any discrepancies between the live positions and the local SQLite state.
        """
        return await self.reconciler.reconcile_state_on_startup(
            db_path_or_db=db_path_or_db,
            cancel_resting_orders=cancel_resting_orders
        )

    async def reconcile_exchange_state(
        self,
        db_path_or_db: Any = "kalshi_audit.db",
        cancel_orphaned_orders: bool = True
    ) -> Dict[str, Any]:
        """
        Backward-compatible alias for reconcile_state_on_startup.
        """
        return await self.reconcile_state_on_startup(
            db_path_or_db=db_path_or_db,
            cancel_resting_orders=cancel_orphaned_orders
        )


async def reconcile_state_on_startup(
    auth: Optional[KalshiAuth] = None,
    rest_url: Optional[str] = None,
    db_path: str = "kalshi_audit.db",
    cancel_resting_orders: bool = True,
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Top-level standalone cold-boot reconciliation runner:
    1. Fetch all open orders (/portfolio/orders with status=resting).
    2. Issue cancel requests for all resting orders.
    3. Fetch all open positions (/portfolio/positions).
    4. Log any discrepancies between the live positions and the local SQLite state.
    """
    reconciler = KalshiStateReconciler(auth=auth, rest_url=rest_url, dry_run=dry_run)
    return await reconciler.reconcile_state_on_startup(
        db_path_or_db=db_path,
        cancel_resting_orders=cancel_resting_orders
    )



# ==============================================================================
# 5. PLUGGABLE EXECUTION BROKER INTERFACE
# ==============================================================================

class BaseExecutionBroker(ABC):
    """Abstract interface defining trade execution contract."""
    @abstractmethod
    async def submit_entry(self, ticker: str, side: str, contracts: int, model_prob: float, max_price_cents: int) -> Dict[str, Any]:
        pass

    @abstractmethod
    async def exit_position(self, ticker: str, side: str, contracts: int, reason: str, exit_price_cents: int) -> Dict[str, Any]:
        pass


class LiveKalshiBroker(BaseExecutionBroker):
    """Live execution broker connected to Kalshi OMS and WebSocket."""
    def __init__(self, oms: KalshiOrderManagementSystem):
        self.oms = oms

    async def submit_entry(self, ticker: str, side: str, contracts: int, model_prob: float, max_price_cents: int) -> Dict[str, Any]:
        return await self.oms.execute_trade(
            ticker=ticker,
            direction=side,
            target_contracts=contracts,
            edge_threshold=max_price_cents,
            model_prob=model_prob
        )

    async def exit_position(self, ticker: str, side: str, contracts: int, reason: str, exit_price_cents: int) -> Dict[str, Any]:
        if reason == "STOP_LOSS":
            return await self.oms.execute_limit_chasing_stop_loss(
                ticker=ticker,
                side=side,
                contracts=contracts,
                floor_cents=15
            )
        else:
            # Take profit / normal market exit
            return await self.oms._place_rest_order(
                ticker=ticker,
                action="sell",
                side=side.lower(),
                count=contracts,
                price_cents=exit_price_cents,
                order_type="limit"
            )
