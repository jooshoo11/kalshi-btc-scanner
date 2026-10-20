#!/usr/bin/env python3
"""
kalshi_client.py — Asynchronous Kalshi V2 Client & WebSocket OMS for Demo Execution.

Features:
- Configured specifically for Kalshi Demo Environment:
  - REST API: https://demo-api.kalshi.co/trade-api/v2
  - WebSocket: wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2
- Secure RSA-PSS private key loading from environment variables (never hardcoded)
- Strictly non-blocking async REST client methods (httpx.AsyncClient)
- AsyncOrderManager with real-time L2 order book cache for KXBTC15M contracts
- Real-time VWAP sweep inspection, limit-chasing stop losses, and Stale-Tick Watchdog
"""

import os
import sys
import time
import math
import json
import base64
import asyncio
import logging
from typing import Dict, Any, List, Optional, Tuple, Callable

import httpx
try:
    import websockets
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
except ImportError as e:
    logging.warning(f"Crypto / WebSocket dependencies missing: {e}")

from prediction_engine import calculate_kalshi_fee

logger = logging.getLogger("KalshiClient")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [KalshiClient] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# ==============================================================================
# CONFIGURATION & DEMO ENDPOINTS
# ==============================================================================
DEFAULT_DEMO_REST_URL = "https://demo-api.kalshi.co/trade-api/v2"
DEFAULT_DEMO_WS_URL = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"


# ==============================================================================
# 1. RSA-PSS AUTHENTICATOR & ENV KEY LOADER
# ==============================================================================

class KalshiAuth:
    """
    Kalshi API V2 Cryptographic Authentication Handler.
    Signs requests using RSA-PSS with SHA-256 and PKCS#1 v2.1 MGF1 padding.
    """
    def __init__(
        self,
        key_id: Optional[str] = None,
        private_key_pem: Optional[str] = None,
        private_key_path: Optional[str] = None
    ):
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
            # Handle potential escaped newlines from environment variables (.env files)
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
        clean_path = path.split("?")[0]
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
# 2. ASYNC KALSHI REST CLIENT
# ==============================================================================

class KalshiClient:
    """
    Non-blocking asynchronous Kalshi REST API Client targeting the Demo Environment.
    """
    def __init__(
        self,
        auth: Optional[KalshiAuth] = None,
        base_url: str = DEFAULT_DEMO_REST_URL,
        dry_run: bool = False,
        timeout_seconds: float = 5.0
    ):
        self.auth = auth or KalshiAuth.from_env()
        self.base_url = base_url.rstrip("/")
        self.dry_run = dry_run
        self.timeout = timeout_seconds
        self._http_client: Optional[httpx.AsyncClient] = None

    async def get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=self.timeout)
        return self._http_client

    async def close(self):
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None

    async def _request(self, method: str, endpoint: str, params: Optional[Dict] = None, json_data: Optional[Dict] = None) -> Dict[str, Any]:
        """Core non-blocking request dispatcher with RSA-PSS authentication."""
        url = f"{self.base_url}{endpoint}"
        sign_path = f"/trade-api/v2{endpoint}" if not endpoint.startswith("/trade-api/v2") else endpoint
        headers = self.auth.sign_request(method, sign_path) if self.auth and self.auth.is_configured else {"Content-Type": "application/json"}

        client = await self.get_http_client()
        try:
            resp = await client.request(method, url, headers=headers, params=params, json=json_data)
            if resp.status_code in (200, 201):
                return resp.json()
            else:
                logger.error(f"[KalshiClient] HTTP {method} {endpoint} failed ({resp.status_code}): {resp.text}")
                return {"error": resp.text, "status_code": resp.status_code}
        except Exception as e:
            logger.error(f"[KalshiClient] Network error on {method} {endpoint}: {e}")
            return {"error": str(e), "status_code": 500}

    async def get_balance(self) -> Dict[str, Any]:
        """Query portfolio balance (/portfolio/balance)."""
        if self.dry_run or not self.auth.is_configured or self.auth.key_id.startswith("test-"):
            return {"balance": 100000, "payout": 0}  # $1,000.00 mock demo balance
        return await self._request("GET", "/portfolio/balance")

    async def get_positions(self, ticker: Optional[str] = None) -> List[Dict[str, Any]]:
        """Query open portfolio positions (/portfolio/positions)."""
        if self.dry_run or not self.auth.is_configured or self.auth.key_id.startswith("test-"):
            return []
        params = {"ticker": ticker} if ticker else None
        res = await self._request("GET", "/portfolio/positions", params=params)
        return res.get("market_positions") or res.get("positions") or []

    async def get_resting_orders(self, ticker: Optional[str] = None) -> List[Dict[str, Any]]:
        """Query resting open orders (/portfolio/orders?status=resting)."""
        if self.dry_run or not self.auth.is_configured or self.auth.key_id.startswith("test-"):
            return []
        params = {"status": "resting"}
        if ticker:
            params["ticker"] = ticker
        res = await self._request("GET", "/portfolio/orders", params=params)
        return res.get("orders") or []

    async def get_market(self, ticker: str) -> Dict[str, Any]:
        """Query market details (/markets/{ticker})."""
        return await self._request("GET", f"/markets/{ticker}")

    async def get_orderbook(self, ticker: str, depth: int = 10) -> Dict[str, Any]:
        """Query resting orderbook snapshot via REST (/markets/{ticker}/orderbook)."""
        return await self._request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})

    async def place_order(
        self,
        ticker: str,
        action: str,
        side: str,
        count: int,
        price_cents: int,
        order_type: str = "limit",
        client_order_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Place an order via POST /portfolio/orders.
        - price_cents: price in cents [1 - 99]
        """
        if self.dry_run or not self.auth.is_configured or self.auth.key_id.startswith("test-"):
            logger.info(f"[KalshiClient Mock] Placed order: {action} {count}x {side} on {ticker} @ {price_cents}¢")
            return {
                "order_id": f"mock_ord_{int(time.time()*1000)}",
                "ticker": ticker,
                "status": "executed",
                "action": action,
                "side": side,
                "count": count,
                "price_cents": price_cents
            }

        payload = {
            "action": action.lower(),
            "type": order_type.lower(),
            "ticker": ticker,
            "side": side.lower(),
            "count": int(count),
            "yes_price": int(price_cents) if side.lower() == "yes" else int(100 - price_cents),
            "client_order_id": client_order_id or f"kalshi_bot_{int(time.time()*1000)}"
        }
        return await self._request("POST", "/portfolio/orders", json_data=payload)

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an active order via DELETE /portfolio/orders/{order_id}."""
        if self.dry_run or not self.auth.is_configured or self.auth.key_id.startswith("test-") or "mock_" in order_id:
            return True
        res = await self._request("DELETE", f"/portfolio/orders/{order_id}")
        return "error" not in res

    async def get_order(self, order_id: str) -> Dict[str, Any]:
        """Query status of a specific order via GET /portfolio/orders/{order_id}."""
        if self.dry_run or not self.auth.is_configured or self.auth.key_id.startswith("test-") or "mock_" in order_id:
            return {"order": {"status": "executed", "remaining_count": 0}}
        return await self._request("GET", f"/portfolio/orders/{order_id}")


# ==============================================================================
# 3. L2 ORDER BOOK DATA STRUCTURE
# ==============================================================================

class L2OrderBook:
    """
    In-memory Level-2 Order Book with O(1) level updates and VWAP sweep calculation.
    Prices are represented in integer cents [1 - 99].
    """
    def __init__(self, ticker: str):
        self.ticker = ticker
        self.bids: Dict[int, int] = {}  # price_cents -> quantity
        self.asks: Dict[int, int] = {}  # price_cents -> quantity
        self.last_update_ts: float = 0.0

    def apply_snapshot(self, bids: List[List[int]], asks: List[List[int]]):
        """Initialize book from full snapshot [[price_cents, qty], ...]"""
        self.bids = {int(p): int(q) for p, q in bids if int(q) > 0}
        self.asks = {int(p): int(q) for p, q in asks if int(q) > 0}
        self.last_update_ts = time.time()

    def apply_delta(self, side: str, price_cents: int, delta_qty: int):
        """Update price level from orderbook_delta stream."""
        p = int(price_cents)
        book = self.bids if side.lower() in ("yes_bid", "bid") else self.asks
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

        is_buy_yes = (side.upper() in ("YES", "BUY"))
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
# 4. STALE-TICK LATENCY & HEARTBEAT WATCHDOG
# ==============================================================================

class StaleTickWatchdog:
    """
    Stale-Tick Latency & Heartbeat Watchdog.
    Halts trading if timestamp delta between feeds and local clock exceeds 1500ms.
    """
    def __init__(self, max_allowed_delta_ms: float = 1500.0, recovery_threshold_ms: float = 800.0, recovery_ticks: int = 3):
        self.max_allowed_delta_ms = max_allowed_delta_ms
        self.recovery_threshold_ms = recovery_threshold_ms
        self.recovery_ticks = recovery_ticks
        self.is_halted: bool = False
        self.halt_reason: str = ""
        self.last_tick_time: float = time.time()
        self.last_delta_ms: float = 0.0
        self._consecutive_healthy_ticks: int = 0
        self._total_halts_triggered: int = 0

    def record_tick(self, feed_timestamp: Optional[float] = None, reported_latency_ms: Optional[float] = None) -> bool:
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
                logger.error(f"[Watchdog] TRIPPED! {self.halt_reason}")
            self._consecutive_healthy_ticks = 0
            return False
        else:
            if self.is_halted:
                if delta_ms <= self.recovery_threshold_ms:
                    self._consecutive_healthy_ticks += 1
                    if self._consecutive_healthy_ticks >= self.recovery_ticks:
                        logger.info(f"[Watchdog] RECOVERED: Feed latency stabilized at {delta_ms:.1f}ms.")
                        self.is_halted = False
                        self.halt_reason = ""
                else:
                    self._consecutive_healthy_ticks = 0
            return not self.is_halted

    def check_heartbeat(self) -> bool:
        now = time.time()
        time_since_last_tick_ms = (now - self.last_tick_time) * 1000.0
        if time_since_last_tick_ms > self.max_allowed_delta_ms:
            if not self.is_halted:
                self.is_halted = True
                self._total_halts_triggered += 1
                self.halt_reason = f"NO_TICKS_RECEIVED: {time_since_last_tick_ms:.0f}ms elapsed since last tick."
                logger.error(f"[Watchdog] TRIPPED! {self.halt_reason}")
            return False
        return not self.is_halted

    def is_safe_to_trade(self) -> bool:
        if self.is_halted:
            return False
        return self.check_heartbeat()

    def get_status(self) -> Dict[str, Any]:
        return {
            "is_halted": self.is_halted,
            "status": "HALTED" if self.is_halted else "HEALTHY",
            "halt_reason": self.halt_reason,
            "last_delta_ms": self.last_delta_ms,
            "time_since_last_tick_ms": round((time.time() - self.last_tick_time) * 1000.0, 1)
        }


# ==============================================================================
# 5. ASYNC ORDER MANAGER (WEBSOCKET OMS)
# ==============================================================================

class AsyncOrderManager:
    """
    High-Performance WebSocket OMS for KXBTC15M contracts:
    - Maintains real-time L2 order book cache without REST polling.
    - Computes instantaneous VWAP depth sweeps.
    - Executes trades with safety circuit breakers and limit-chasing stop loss logic.
    """
    def __init__(
        self,
        client: Optional[KalshiClient] = None,
        ws_url: str = DEFAULT_DEMO_WS_URL,
        watchdog: Optional[StaleTickWatchdog] = None,
        dry_run: bool = False
    ):
        self.client = client or KalshiClient(dry_run=dry_run)
        self.ws_url = ws_url
        self.watchdog = watchdog or StaleTickWatchdog()
        self.dry_run = dry_run

        self.books: Dict[str, L2OrderBook] = {}
        self.subscribed_tickers: set = set()
        self.is_running: bool = False
        self._ws = None
        self._listener_task: Optional[asyncio.Task] = None
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 30.0
        self.on_book_update_callbacks: List[Callable[[str, L2OrderBook], None]] = []

    def get_or_create_book(self, ticker: str) -> L2OrderBook:
        if ticker not in self.books:
            self.books[ticker] = L2OrderBook(ticker)
        return self.books[ticker]

    def get_book(self, ticker: str) -> Optional[L2OrderBook]:
        return self.books.get(ticker)

    def get_best_bid_ask(self, ticker: str) -> Tuple[int, int, int, int]:
        book = self.get_book(ticker)
        if not book:
            return 0, 0, 100, 0
        best_bid, bid_qty = book.get_best_bid()
        best_ask, ask_qty = book.get_best_ask()
        return best_bid, bid_qty, best_ask, ask_qty

    def get_vwap_for_size(self, ticker: str, side: str, count: int) -> Dict[str, Any]:
        book = self.get_or_create_book(ticker)
        return book.calculate_sweep_vwap(side, count)

    def start_background_listener(self):
        """Spawns WebSocket listener in the running asyncio loop."""
        if self._listener_task is None or self._listener_task.done():
            self.is_running = True
            self._listener_task = asyncio.create_task(self.connect_and_listen())

    async def connect_and_listen(self):
        """Long-running WebSocket loop with auto-reconnection and exponential backoff."""
        self.is_running = True
        logger.info(f"[AsyncOrderManager] Connecting to Kalshi WebSocket: {self.ws_url}")

        while self.is_running:
            try:
                headers = {}
                if self.client.auth and self.client.auth.is_configured:
                    headers = self.client.auth.sign_request("GET", "/trade-api/ws/v2")

                connect_kwargs = {"ping_interval": 20, "ping_timeout": 10}
                import inspect
                if "additional_headers" in inspect.signature(websockets.connect).parameters:
                    connect_kwargs["additional_headers"] = headers
                else:
                    connect_kwargs["extra_headers"] = headers

                async with websockets.connect(self.ws_url, **connect_kwargs) as ws:
                    self._ws = ws
                    self._reconnect_delay = 1.0
                    logger.info("[AsyncOrderManager] Connected to Kalshi WebSocket.")

                    if self.subscribed_tickers:
                        await self._send_subscribe(list(self.subscribed_tickers))

                    async for message in ws:
                        await self._handle_ws_message(message)

            except asyncio.CancelledError:
                logger.info("[AsyncOrderManager] WebSocket listener task cancelled.")
                break
            except Exception as e:
                logger.warning(f"[AsyncOrderManager] WebSocket disconnected ({e}). Reconnecting in {self._reconnect_delay:.1f}s...")
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._max_reconnect_delay, self._reconnect_delay * 1.8)

    async def _send_subscribe(self, tickers: List[str]):
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
        logger.info(f"[AsyncOrderManager] Subscribed to channels for: {tickers}")

    @property
    def is_ws_connected(self) -> bool:
        if self._ws is None:
            return False
        if hasattr(self._ws, "state"):
            return getattr(self._ws.state, "name", "") == "OPEN"
        return getattr(self._ws, "open", not getattr(self._ws, "closed", True))

    async def subscribe(self, tickers: List[str] | str):
        ticker_list = [tickers] if isinstance(tickers, str) else tickers
        for t in ticker_list:
            self.subscribed_tickers.add(t)
            self.get_or_create_book(t)

        if self.is_ws_connected:
            await self._send_subscribe(ticker_list)

    async def _handle_ws_message(self, raw_msg: str):
        try:
            data = json.loads(raw_msg)
            msg_type = data.get("type")
            msg_data = data.get("msg", {})

            if msg_type == "orderbook_snapshot":
                ticker = msg_data.get("market_ticker")
                if ticker:
                    book = self.get_or_create_book(ticker)
                    book.apply_snapshot(msg_data.get("bids", []), msg_data.get("asks", []))
                    self._notify_book_update(ticker, book)

            elif msg_type == "orderbook_delta":
                ticker = msg_data.get("market_ticker")
                if ticker:
                    book = self.get_or_create_book(ticker)
                    side = msg_data.get("side", "yes_ask")
                    price = msg_data.get("price", 0)
                    delta = msg_data.get("delta", 0)
                    book.apply_delta(side, price, delta)
                    self._notify_book_update(ticker, book)

            elif msg_type == "ticker":
                ts = msg_data.get("ts")
                if ts:
                    self.watchdog.record_tick(feed_timestamp=ts)

        except Exception as e:
            logger.debug(f"[AsyncOrderManager] Error parsing WS message: {e}")

    def _notify_book_update(self, ticker: str, book: L2OrderBook):
        for cb in self.on_book_update_callbacks:
            try:
                cb(ticker, book)
            except Exception:
                pass

    async def execute_trade(
        self,
        ticker: str,
        side: str,
        count: int,
        price: float | int,
        min_net_edge_pct: float = 2.5,
        model_prob: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Executes an entry trade with pre-trade checks:
        - Stale-Tick Watchdog check
        - L2 Book Depth VWAP sweep validation
        - Capped marketable limit order dispatch to avoid book-sweeping slippage
        """
        price_cents = int(round(price * 100)) if isinstance(price, float) and price <= 1.0 else int(round(price))

        # 1. Stale-Tick Watchdog Check
        if not self.watchdog.is_safe_to_trade():
            logger.error(f"[AsyncOrderManager] Trade blocked by Stale-Tick Watchdog! ({self.watchdog.halt_reason})")
            return {
                "status": "ABORTED_STALE_TICK",
                "reason": self.watchdog.halt_reason or "Market data latency exceeded safety threshold",
                "watchdog": self.watchdog.get_status()
            }

        # 2. L2 Book VWAP Inspection
        book = self.get_book(ticker)
        vwap_res = book.calculate_sweep_vwap(side, count) if book else None

        if vwap_res and not vwap_res["can_fill"]:
            logger.warning(f"[AsyncOrderManager] Aborting trade on {ticker}: Insufficient book liquidity ({vwap_res['filled_count']}/{count}).")
            return {
                "status": "ABORTED_THIN_BOOK",
                "reason": f"Insufficient book depth: Available {vwap_res['filled_count']} < Needed {count}"
            }

        effective_vwap_cents = vwap_res["vwap_cents"] if vwap_res else price_cents
        effective_worst_price = vwap_res["worst_price_cents"] if vwap_res else price_cents

        # 3. Model Edge Verification (if model_prob provided)
        if model_prob is not None:
            vwap_dollars = effective_vwap_cents / 100.0
            fee_per_contract = calculate_kalshi_fee(1, vwap_dollars)
            net_edge = model_prob - (vwap_dollars + fee_per_contract)
            if (net_edge * 100.0) < min_net_edge_pct:
                logger.warning(f"[AsyncOrderManager] Aborting trade on {ticker}: Post-sweep net edge ({net_edge*100:.1f}%) < min {min_net_edge_pct}%.")
                return {
                    "status": "ABORTED_INSUFFICIENT_EDGE",
                    "reason": f"Post-sweep net edge {net_edge*100:.1f}% below minimum {min_net_edge_pct}%"
                }

        # 4. Dispatch Marketable Limit Order capped at worst price
        logger.info(
            f"[AsyncOrderManager] Executing {count}x {side} on {ticker} | "
            f"Expected VWAP: {effective_vwap_cents:.1f}¢ | Cap Limit: {effective_worst_price}¢"
        )
        return await self.client.place_order(
            ticker=ticker,
            action="buy",
            side=side.lower(),
            count=count,
            price_cents=effective_worst_price,
            order_type="limit"
        )

    async def execute_stop_loss(
        self,
        ticker: str,
        side: str,
        count: int,
        floor_cents: int = 15,
        max_chase_attempts: int = 4,
        chase_poll_ms: int = 800
    ) -> Dict[str, Any]:
        """
        Smart Limit-Chasing Stop Loss:
        - Avoids dumping market sell orders into 1¢ bids.
        - Chases market bid downward with aggressive limits until filled or floor is reached.
        """
        logger.info(f"[AsyncOrderManager] Initiating Limit-Chasing Stop Loss: {count} contracts on {ticker} (Floor: {floor_cents}¢)")

        for attempt in range(1, max_chase_attempts + 1):
            book = self.get_book(ticker)
            best_bid_cents, _ = book.get_best_bid() if book else (0, 0)
            if best_bid_cents <= 0:
                best_bid_cents = floor_cents + 5

            target_limit = max(floor_cents, best_bid_cents - 1)
            logger.info(f"[AsyncOrderManager] Chase Attempt {attempt}/{max_chase_attempts}: Posting limit at {target_limit}¢")

            order_res = await self.client.place_order(
                ticker=ticker,
                action="sell",
                side=side.lower(),
                count=count,
                price_cents=target_limit,
                order_type="limit"
            )

            order_id = order_res.get("order_id")
            if not order_id or "mock_" in order_id:
                return order_res

            await asyncio.sleep(chase_poll_ms / 1000.0)

            order_status = await self.client.get_order(order_id)
            status_data = order_status.get("order", order_status)
            if status_data.get("status") == "executed" or status_data.get("remaining_count", 0) == 0:
                logger.info(f"[AsyncOrderManager] Limit-chasing stop loss filled completely at {target_limit}¢!")
                return {"status": "FILLED", "fill_price_cents": target_limit, "order_id": order_id}

            if attempt < max_chase_attempts:
                logger.info(f"[AsyncOrderManager] Order {order_id} unfilled. Cancelling to chase market bid...")
                await self.client.cancel_order(order_id)

        # Fallback to floor
        logger.warning(f"[AsyncOrderManager] Max chase attempts reached. Posting final limit at safety floor: {floor_cents}¢")
        return await self.client.place_order(
            ticker=ticker,
            action="sell",
            side=side.lower(),
            count=count,
            price_cents=floor_cents,
            order_type="limit"
        )

    def stop(self):
        self.is_running = False
        if self._ws:
            asyncio.create_task(self._ws.close())
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
