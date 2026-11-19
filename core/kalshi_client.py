#!/usr/bin/env python3
"""
core/kalshi_client.py — Production Multi-Series Kalshi V2 REST & WebSocket Client.

Architecture:
1. Generic Multi-Series REST Client:
   - Configurable for Demo (demo-api.kalshi.co) and Production (api.elections.kalshi.com / api.kalshi.co).
   - Supports arbitrary crypto series: KXBTC15M, KXETH15M, KXSOL15M, KXXRP15M, etc.
   - Built-in Token Bucket Rate Limiting to prevent HTTP 429 throttling.
   - Automatic Retry with Exponential Backoff & Jitter on transient 429/5xx errors.
   - Structured Request/Response Audit Logging.
2. Cryptographic RSA-PSS Authentication:
   - Safe API key and private key loading from .env or environment variables.
   - PKCS#1 v2.1 RSA-PSS SHA-256 request signing (never logs raw keys or secrets).
3. Exact Non-Linear Kalshi Taker Fee Calculation:
   - Formula: ceil(0.07 * contracts * price * (1 - price) * 100) / 100.
4. WebSocket Subscription Manager:
   - Manages concurrent subscriptions across multiple series and market tickers.
   - Automated reconnection with exponential backoff and jitter.
   - Heartbeat and stale-tick watchdog integration.
   - Real-time L2 order book cache with sub-millisecond VWAP sweep calculation.
"""

import os
import sys
import time
import math
import json
import base64
import random
import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Callable, Union

import httpx

try:
    import websockets
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
except ImportError as e:
    logging.warning(f"Crypto / WebSocket dependencies missing: {e}")

logger = logging.getLogger("KalshiClient")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [KalshiClient] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# ==============================================================================
# ENVIRONMENT CONFIGURATIONS
# ==============================================================================
DEFAULT_DEMO_REST_URL = "https://demo-api.kalshi.co/trade-api/v2"
DEFAULT_DEMO_WS_URL = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"

DEFAULT_PROD_REST_URL = "https://api.elections.kalshi.com/trade-api/v2"
DEFAULT_PROD_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"

# Supported 15-Minute Crypto Series
SUPPORTED_SERIES = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M"]


# ==============================================================================
# 1. NON-LINEAR KALSHI TAKER FEE CALCULATION
# ==============================================================================

def calculate_kalshi_taker_fee(contracts: int, price: float) -> float:
    """
    Standard Kalshi non-linear taker fee formula:
    Fee = ceil(0.07 * contracts * price * (1 - price) * 100) / 100
    
    Parameters:
      contracts: Number of contracts (must be > 0)
      price: Price per contract in dollars [0.01 - 0.99]
    """
    if contracts <= 0 or price <= 0.0 or price >= 1.0:
        return 0.0
    raw_fee = 0.07 * float(contracts) * float(price) * (1.0 - float(price))
    return math.ceil(raw_fee * 100.0) / 100.0


# Backwards compatibility alias
calculate_kalshi_fee = calculate_kalshi_taker_fee


# ==============================================================================
# 2. RSA-PSS AUTHENTICATOR & SECURE ENV KEY LOADER
# ==============================================================================

def load_dotenv_safely(env_path: Optional[str] = None):
    """Safely loads .env file into os.environ if not already set."""
    try:
        from dotenv import load_dotenv
        path = env_path or os.path.join(os.getcwd(), ".env")
        if os.path.exists(path):
            load_dotenv(path, override=False)
            return
    except ImportError:
        pass

    # Simple manual fallback parser if python-dotenv is not installed
    path = env_path or os.path.join(os.getcwd(), ".env")
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k not in os.environ:
                            os.environ[k] = v
        except Exception:
            pass


class KalshiAuth:
    """
    Kalshi API V2 Cryptographic Authentication Handler.
    Signs requests using RSA-PSS with SHA-256 and PKCS#1 v2.1 MGF1 padding.
    Credentials are never hardcoded and loaded strictly from secure env/PEM.
    """

    def __init__(
        self,
        key_id: Optional[str] = None,
        private_key_pem: Optional[str] = None,
        private_key_path: Optional[str] = None,
    ):
        self.key_id = (key_id or "").strip()
        self._private_key = None

        if private_key_pem:
            self._load_from_pem(private_key_pem.encode("utf-8"))
        elif private_key_path and os.path.exists(private_key_path):
            with open(private_key_path, "rb") as f:
                self._load_from_pem(f.read())

    @classmethod
    def from_env(cls, env_path: Optional[str] = None) -> "KalshiAuth":
        """
        Securely load RSA-PSS credentials from environment variables or .env file.
        Checks:
          - KALSHI_API_KEY_ID or KALSHI_KEY_ID
          - KALSHI_PRIVATE_KEY_PEM (unescapes newlines like '\\n')
          - KALSHI_PRIVATE_KEY_PATH (path to private key file)
        """
        load_dotenv_safely(env_path)

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
        clean_path = path.split("?")[0]
        if not clean_path.startswith("/trade-api/v2"):
            clean_path = f"/trade-api/v2{clean_path}"
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
# 3. RATE LIMITING & AUDIT TRAIL LOGGING
# ==============================================================================

class TokenBucketRateLimiter:
    """
    Token Bucket Rate Limiter to guarantee client stays within Kalshi API quotas.
    Default: 10 requests/sec with burst capacity up to 20 requests.
    """

    def __init__(self, rate_per_second: float = 10.0, burst_capacity: float = 20.0):
        self.rate = rate_per_second
        self.capacity = burst_capacity
        self.tokens = burst_capacity
        self.last_update = time.time()
        self._lock = asyncio.Lock()

    async def acquire(self):
        """Asynchronously acquire a token, waiting if bucket is exhausted."""
        async with self._lock:
            now = time.time()
            elapsed = now - self.last_update
            self.last_update = now
            self.tokens = min(self.capacity, self.tokens + (elapsed * self.rate))

            if self.tokens < 1.0:
                wait_time = (1.0 - self.tokens) / self.rate
                await asyncio.sleep(wait_time)
                self.tokens = 0.0
                self.last_update = time.time()
            else:
                self.tokens -= 1.0


class KalshiRequestAuditLogger:
    """Structured audit logger for Kalshi API requests and responses."""

    @staticmethod
    def log_request(method: str, url: str, params: Optional[Dict], json_data: Optional[Dict]):
        sanitized_json = None
        if json_data:
            sanitized_json = dict(json_data)
        logger.debug(f"[API REQ] {method} {url} | params={params} | body={sanitized_json}")

    @staticmethod
    def log_response(method: str, url: str, status_code: int, latency_ms: float, error: Optional[str] = None):
        if status_code in (200, 201):
            logger.debug(f"[API RESP] {method} {url} -> {status_code} ({latency_ms:.1f}ms)")
        else:
            logger.warning(f"[API ERROR] {method} {url} -> {status_code} ({latency_ms:.1f}ms) | err={error}")


# ==============================================================================
# 4. GENERIC ASYNC KALSHI V2 REST CLIENT
# ==============================================================================

class KalshiClient:
    """
    Generic Multi-Series Asynchronous Kalshi REST API Client.
    Supports arbitrary series tickers, symbols, and parameters across Demo and Production.
    """

    def __init__(
        self,
        auth: Optional[KalshiAuth] = None,
        base_url: Optional[str] = None,
        env: str = "demo",
        dry_run: bool = False,
        timeout_seconds: float = 5.0,
        rate_limit_per_second: float = 10.0,
        max_retries: int = 3,
    ):
        self.auth = auth or KalshiAuth.from_env()
        self.dry_run = dry_run
        self.timeout = timeout_seconds
        self.max_retries = max_retries

        # Select environment base URL
        if base_url:
            self.base_url = base_url.rstrip("/")
        elif env.lower() in ("prod", "production"):
            self.base_url = DEFAULT_PROD_REST_URL
        else:
            self.base_url = DEFAULT_DEMO_REST_URL

        self.rate_limiter = TokenBucketRateLimiter(rate_per_second=rate_limit_per_second)
        self.audit_logger = KalshiRequestAuditLogger()
        self._http_client: Optional[httpx.AsyncClient] = None

    async def get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(timeout=self.timeout)
        return self._http_client

    async def close(self):
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
            self._http_client = None

    async def __aenter__(self) -> "KalshiClient":
        await self.get_http_client()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    @staticmethod
    def calculate_fee(contracts: int, price: float) -> float:
        """Expose standard non-linear fee calculation."""
        return calculate_kalshi_taker_fee(contracts, price)

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        json_data: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """
        Dispatches an async HTTP request with rate limiting, signature headers,
        and automatic exponential backoff retry.
        """
        url = f"{self.base_url}{endpoint}"
        sign_path = f"/trade-api/v2{endpoint}" if not endpoint.startswith("/trade-api/v2") else endpoint

        last_error = ""
        last_status = 500

        for attempt in range(1, self.max_retries + 1):
            await self.rate_limiter.acquire()
            headers = self.auth.sign_request(method, sign_path) if self.auth and self.auth.is_configured else {"Content-Type": "application/json"}
            client = await self.get_http_client()

            self.audit_logger.log_request(method, url, params, json_data)
            start_ts = time.time()

            try:
                resp = await client.request(method, url, headers=headers, params=params, json=json_data)
                latency_ms = (time.time() - start_ts) * 1000.0

                if resp.status_code in (200, 201):
                    self.audit_logger.log_response(method, url, resp.status_code, latency_ms)
                    return resp.json()

                last_status = resp.status_code
                last_error = resp.text
                self.audit_logger.log_response(method, url, resp.status_code, latency_ms, error=resp.text)

                # Retry on 429 Too Many Requests or 5xx Server Errors
                if resp.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                    backoff = (2 ** (attempt - 1)) * 0.5 + random.uniform(0.1, 0.3)
                    logger.warning(f"[KalshiClient] HTTP {resp.status_code} on {endpoint}. Retrying in {backoff:.2f}s (Attempt {attempt}/{self.max_retries})...")
                    await asyncio.sleep(backoff)
                    continue

                return {"error": resp.text, "status_code": resp.status_code}

            except (httpx.TransportError, httpx.TimeoutException) as e:
                latency_ms = (time.time() - start_ts) * 1000.0
                last_error = str(e)
                self.audit_logger.log_response(method, url, 503, latency_ms, error=str(e))

                if attempt < self.max_retries:
                    backoff = (2 ** (attempt - 1)) * 0.5 + random.uniform(0.1, 0.3)
                    logger.warning(f"[KalshiClient] Network error on {endpoint}: {e}. Retrying in {backoff:.2f}s...")
                    await asyncio.sleep(backoff)
                else:
                    return {"error": str(e), "status_code": 503}

        return {"error": last_error, "status_code": last_status}

    # =========================================================================
    # MULTI-SERIES DISCOVERY & PUBLIC MARKET METHODS
    # =========================================================================

    async def get_series(self, series_ticker: str) -> Dict[str, Any]:
        """Fetch series specification for arbitrary series (e.g. KXBTC15M, KXETH15M)."""
        return await self._request("GET", f"/series/{series_ticker}")

    async def get_markets(
        self,
        series_ticker: Optional[str] = None,
        status: str = "open",
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List markets with optional series filtering and pagination."""
        params: Dict[str, Any] = {"status": status, "limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/markets", params=params)

    async def get_market(self, ticker: str) -> Dict[str, Any]:
        """Query market details (/markets/{ticker})."""
        return await self._request("GET", f"/markets/{ticker}")

    async def get_orderbook(self, ticker: str, depth: int = 10) -> Dict[str, Any]:
        """Query resting orderbook snapshot (/markets/{ticker}/orderbook)."""
        return await self._request("GET", f"/markets/{ticker}/orderbook", params={"depth": depth})

    # =========================================================================
    # PORTFOLIO & EXECUTION METHODS
    # =========================================================================

    async def get_balance(self) -> Dict[str, Any]:
        """Query portfolio balance (/portfolio/balance)."""
        if self.dry_run or not self.auth.is_configured or self.auth.key_id.startswith("test-"):
            return {"balance": 100000, "payout": 0}  # $1,000.00 mock demo balance
        return await self._request("GET", "/portfolio/balance")

    async def get_positions(self, ticker: Optional[str] = None, series_ticker: Optional[str] = None) -> List[Dict[str, Any]]:
        """Query open portfolio positions (/portfolio/positions)."""
        if self.dry_run or not self.auth.is_configured or self.auth.key_id.startswith("test-"):
            return []
        params = {}
        if ticker:
            params["ticker"] = ticker
        if series_ticker:
            params["series_ticker"] = series_ticker
        res = await self._request("GET", "/portfolio/positions", params=params or None)
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

    async def get_order(self, order_id: str) -> Dict[str, Any]:
        """Query status of a specific order via GET /portfolio/orders/{order_id}."""
        if self.dry_run or not self.auth.is_configured or self.auth.key_id.startswith("test-") or "mock_" in order_id:
            return {"order": {"status": "executed", "remaining_count": 0}}
        return await self._request("GET", f"/portfolio/orders/{order_id}")

    async def place_order(
        self,
        ticker: str,
        action: str,
        side: str,
        count: int,
        price_cents: int,
        order_type: str = "limit",
        client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Place an order via POST /portfolio/orders.
        - price_cents: price in integer cents [1 - 99]
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

    async def cancel_order(self, order_id: str, ticker: Optional[str] = None) -> bool:
        """Cancel an active order via DELETE /portfolio/events/orders/{order_id}."""
        if self.dry_run or not self.auth.is_configured or self.auth.key_id.startswith("test-") or "mock_" in order_id:
            return True
        params = {"exchange_index": 2}
        if ticker:
            params["market_ticker"] = ticker
        res = await self._request("DELETE", f"/portfolio/events/orders/{order_id}", params=params)
        return "error" not in res

    async def batch_cancel_orders(self, ticker: Optional[str] = None) -> Dict[str, Any]:
        """Cancel all resting orders across an asset or account."""
        if self.dry_run or not self.auth.is_configured:
            return {"cancelled": True, "count": 0}
        params = {"exchange_index": 2}
        if ticker:
            params["market_ticker"] = ticker
        return await self._request("DELETE", "/portfolio/events/orders", params=params)

    async def get_settlement(self, ticker: str) -> Optional[Dict[str, Any]]:
        """Fetch historical settlement details for a market."""
        res = await self._request("GET", f"/markets/{ticker}")
        m = res.get("market", {})
        if m.get("status") in ("finalized", "settled", "closed"):
            return {
                "ticker": ticker,
                "market_result": m.get("result"),
                "settlement_value": m.get("settlement_value"),
                "status": m.get("status")
            }
        return None


# ==============================================================================
# 5. L2 ORDER BOOK DATA STRUCTURE & VWAP SWEEPER
# ==============================================================================

class L2OrderBook:
    """
    In-memory Level-2 Order Book with O(1) level updates and VWAP sweep calculation.
    Prices are represented in integer cents [1 - 99].
    """

    def __init__(self, ticker: str):
        self.ticker = ticker
        self.bids: Dict[int, int] = {}  # YES bids: price_cents -> quantity
        self.asks: Dict[int, int] = {}  # YES asks (100 - NO bid): price_cents -> quantity
        self.last_update_ts: float = 0.0
        self.stale: bool = False
        self.last_seq: Optional[int] = None

    @staticmethod
    def parse_price_cents(raw_price: Any) -> int:
        if raw_price is None:
            return 0
        s = str(raw_price).strip()
        if "." in s:
            val = float(s)
            if val <= 1.0:
                return int(round(val * 100.0))
            return int(round(val))
        return int(s)

    def apply_snapshot(self, bids: List[List[int]], asks: List[List[int]], seq: Optional[int] = None):
        """Initialize book from full snapshot [[price_cents, qty], ...]"""
        new_bids: Dict[int, int] = {}
        for p, q in bids:
            pc, qc = int(p), int(q)
            if 1 <= pc <= 99 and qc > 0:
                new_bids[pc] = new_bids.get(pc, 0) + qc
        new_asks: Dict[int, int] = {}
        for p, q in asks:
            pc, qc = int(p), int(q)
            if 1 <= pc <= 99 and qc > 0:
                new_asks[pc] = new_asks.get(pc, 0) + qc
        self.bids = new_bids
        self.asks = new_asks
        self.last_update_ts = time.time()
        self.stale = False
        if seq is not None:
            self.last_seq = int(seq)

    def apply_delta(self, side: str, price_cents: int, delta_qty: int, seq: Optional[int] = None) -> bool:
        """Update price level from orderbook_delta stream."""
        if seq is not None:
            seq_i = int(seq)
            if self.last_seq is not None and seq_i != self.last_seq + 1:
                self.stale = True
                self.last_seq = seq_i
                return False
            self.last_seq = seq_i

        p = int(price_cents)
        s = side.lower()
        if s in ("no", "no_bid"):
            if 1 <= p <= 99:
                p = 100 - p
            else:
                return True
            book = self.asks
        elif s in ("yes_bid", "bid", "yes"):
            if not (1 <= p <= 99):
                return True
            book = self.bids
        else:
            if not (1 <= p <= 99):
                return True
            book = self.asks
        new_qty = book.get(p, 0) + int(delta_qty)
        if new_qty <= 0:
            book.pop(p, None)
        else:
            book[p] = new_qty
        self.last_update_ts = time.time()
        return True

    def validate_against_rest_quote(
        self,
        rest_yes_bid_dollars: Optional[float] = None,
        rest_yes_ask_dollars: Optional[float] = None,
        max_divergence_cents: int = 2,
    ) -> bool:
        best_bid, _ = self.get_best_bid()
        best_ask, _ = self.get_best_ask()
        if best_bid > 0 and best_ask < 100 and best_bid >= best_ask:
            self.stale = True
            return False
        if rest_yes_bid_dollars is not None and rest_yes_bid_dollars > 0 and best_bid > 0:
            rest_bid_c = int(round(float(rest_yes_bid_dollars) * 100.0)) if rest_yes_bid_dollars <= 1.0 else int(round(rest_yes_bid_dollars))
            if abs(best_bid - rest_bid_c) > max_divergence_cents:
                self.stale = True
                return False
        if rest_yes_ask_dollars is not None and rest_yes_ask_dollars > 0 and best_ask < 100:
            rest_ask_c = int(round(float(rest_yes_ask_dollars) * 100.0)) if rest_yes_ask_dollars <= 1.0 else int(round(rest_yes_ask_dollars))
            if abs(best_ask - rest_ask_c) > max_divergence_cents:
                self.stale = True
                return False
        return not self.stale

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

    def calculate_sweep_vwap(self, side: str, count: int, action: str = "BUY") -> Dict[str, Any]:
        """
        Simulate walking the orderbook to fill `count` contracts:
        - BUY YES: walks YES asks ascending at price `p`
        - BUY NO: walks YES bids descending at NO price `100 - p`
        - SELL YES: walks YES bids descending at YES price `p`
        - SELL NO: walks YES asks ascending at NO price `100 - p`
        """
        if count <= 0:
            return {"can_fill": False, "vwap_cents": 0.0, "filled_count": 0, "reason": "Invalid count"}

        s = side.upper()
        act = action.upper()
        if s in ("SELL_YES",):
            s, act = "YES", "SELL"
        elif s in ("SELL_NO",):
            s, act = "NO", "SELL"
        elif s in ("BUY_YES", "BUY"):
            s, act = "YES", "BUY"
        elif s in ("BUY_NO", "SELL"):
            s = "NO"

        if act == "BUY":
            is_yes = (s == "YES")
            book = self.asks if is_yes else self.bids
            sorted_prices = sorted(book.keys()) if is_yes else sorted(book.keys(), reverse=True)
            price_fn = (lambda p: p) if is_yes else (lambda p: 100 - p)
        else:
            is_yes = (s == "YES")
            book = self.bids if is_yes else self.asks
            sorted_prices = sorted(book.keys(), reverse=True) if is_yes else sorted(book.keys())
            price_fn = (lambda p: p) if is_yes else (lambda p: 100 - p)

        if not book:
            return {"can_fill": False, "vwap_cents": 0.0, "filled_count": 0, "reason": "Empty order book"}

        accumulated_qty = 0
        total_cost_cents = 0
        levels_swept = 0
        worst_price = 0

        for p in sorted_prices:
            available = book[p]
            if available <= 0:
                continue
            eff_price = price_fn(p)
            if not (1 <= eff_price <= 99):
                continue
            needed = count - accumulated_qty
            fill_at_level = min(available, needed)

            total_cost_cents += fill_at_level * eff_price
            accumulated_qty += fill_at_level
            levels_swept += 1
            worst_price = eff_price

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
            "levels_swept": levels_swept,
            "total_book_depth": int(sum(book.values())),
        }


# ==============================================================================
# 6. STALE-TICK WATCHDOG & HEARTBEAT MONITOR
# ==============================================================================

class StaleTickWatchdog:
    """
    Stale-Tick Latency & Heartbeat Watchdog.
    Halts trading if market data feed delta exceeds 1500ms.
    """

    def __init__(
        self,
        max_allowed_delta_ms: float = 1500.0,
        recovery_threshold_ms: float = 800.0,
        recovery_ticks: int = 3,
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
            "time_since_last_tick_ms": round((time.time() - self.last_tick_time) * 1000.0, 1),
        }


# ==============================================================================
# 7. MULTI-SERIES WEBSOCKET SUBSCRIPTION MANAGER (ASYNC ORDER MANAGER)
# ==============================================================================

class AsyncOrderManager:
    """
    Multi-Series WebSocket Subscription Manager & Real-Time OMS:
    - Maintains concurrent subscriptions across multiple crypto series (KXBTC15M, KXETH15M, KXSOL15M, KXXRP15M).
    - Auto-reconnect with exponential backoff and jitter.
    - Active WebSocket ping/pong heartbeat keepalive.
    - Thread-safe L2 order book management.
    """

    def __init__(
        self,
        client: Optional[KalshiClient] = None,
        ws_url: Optional[str] = None,
        watchdog: Optional[StaleTickWatchdog] = None,
        dry_run: bool = False,
        env: str = "demo",
    ):
        self.client = client or KalshiClient(dry_run=dry_run, env=env)
        if ws_url:
            self.ws_url = ws_url
        elif env.lower() in ("prod", "production"):
            self.ws_url = DEFAULT_PROD_WS_URL
        else:
            self.ws_url = DEFAULT_DEMO_WS_URL

        self.watchdog = watchdog or StaleTickWatchdog()
        self.dry_run = dry_run

        self.books: Dict[str, L2OrderBook] = {}
        self.subscribed_tickers: set = set()
        self.subscribed_series: set = set()
        self.is_running: bool = False
        self._ws = None
        self._listener_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
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

    @property
    def is_ws_connected(self) -> bool:
        if self._ws is None:
            return False
        if hasattr(self._ws, "state"):
            return getattr(self._ws.state, "name", "") == "OPEN"
        return getattr(self._ws, "open", not getattr(self._ws, "closed", True))

    def start_background_listener(self):
        """Spawns WebSocket listener and heartbeat tasks."""
        if self._listener_task is None or self._listener_task.done():
            self.is_running = True
            self._listener_task = asyncio.create_task(self.connect_and_listen())

    async def subscribe(self, tickers: Union[List[str], str]):
        """Subscribe to arbitrary list of market tickers or series."""
        ticker_list = [tickers] if isinstance(tickers, str) else list(tickers)
        for t in ticker_list:
            self.subscribed_tickers.add(t)
            self.get_or_create_book(t)

        if self.is_ws_connected:
            await self._send_subscribe(ticker_list)

    async def unsubscribe(self, tickers: Union[List[str], str]):
        """Unsubscribe from market tickers."""
        ticker_list = [tickers] if isinstance(tickers, str) else list(tickers)
        for t in ticker_list:
            self.subscribed_tickers.discard(t)
        if self.is_ws_connected and ticker_list:
            msg = {
                "id": int(time.time() * 1000),
                "cmd": "unsubscribe",
                "params": {"channels": ["orderbook_delta", "ticker"], "market_tickers": ticker_list}
            }
            await self._ws.send(json.dumps(msg))

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

    async def _heartbeat_loop(self):
        """Sends periodic ping frames to maintain active socket connection."""
        while self.is_running:
            try:
                await asyncio.sleep(15.0)
                if self.is_ws_connected:
                    if hasattr(self._ws, "ping"):
                        await self._ws.ping()
            except asyncio.CancelledError:
                break
            except Exception:
                pass

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

                    if self._heartbeat_task is None or self._heartbeat_task.done():
                        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

                    if self.subscribed_tickers:
                        await self._send_subscribe(list(self.subscribed_tickers))

                    async for message in ws:
                        await self._handle_ws_message(message)

            except asyncio.CancelledError:
                logger.info("[AsyncOrderManager] WebSocket listener task cancelled.")
                break
            except Exception as e:
                jitter = random.uniform(0.1, 0.5)
                wait_time = self._reconnect_delay + jitter
                logger.warning(f"[AsyncOrderManager] WebSocket disconnected ({e}). Reconnecting in {wait_time:.1f}s...")
                await asyncio.sleep(wait_time)
                self._reconnect_delay = min(self._max_reconnect_delay, self._reconnect_delay * 1.8)

    async def _handle_ws_message(self, raw_msg: str):
        try:
            data = json.loads(raw_msg)
            msg_type = data.get("type")
            msg_data = data.get("msg", {})
            seq = data.get("seq")

            if msg_type == "orderbook_snapshot":
                ticker = msg_data.get("market_ticker")
                if ticker:
                    book = self.get_or_create_book(ticker)
                    raw_yes = msg_data.get("yes_dollars_fp") or msg_data.get("yes_dollars") or msg_data.get("yes") or msg_data.get("bids") or []
                    raw_no = msg_data.get("no_dollars_fp") or msg_data.get("no_dollars") or msg_data.get("no") or []
                    raw_asks = msg_data.get("asks") if not raw_no else None

                    yes_levels = []
                    for item in raw_yes:
                        try:
                            p_cents = L2OrderBook.parse_price_cents(item[0])
                            qty = int(round(float(item[1])))
                            if 1 <= p_cents <= 99 and qty > 0:
                                yes_levels.append([p_cents, qty])
                        except Exception:
                            pass

                    asks_from_no = []
                    if raw_asks is not None:
                        for item in raw_asks:
                            try:
                                p_cents = L2OrderBook.parse_price_cents(item[0])
                                qty = int(round(float(item[1])))
                                if 1 <= p_cents <= 99 and qty > 0:
                                    asks_from_no.append([p_cents, qty])
                            except Exception:
                                pass
                    else:
                        for item in raw_no:
                            try:
                                p_cents = L2OrderBook.parse_price_cents(item[0])
                                qty = int(round(float(item[1])))
                                if 1 <= p_cents <= 99 and qty > 0:
                                    asks_from_no.append([100 - p_cents, qty])
                            except Exception:
                                pass

                    book.apply_snapshot(yes_levels, asks_from_no, seq=seq)
                    self._notify_book_update(ticker, book)

            elif msg_type == "orderbook_delta":
                ticker = msg_data.get("market_ticker")
                if ticker:
                    book = self.get_or_create_book(ticker)
                    raw_side = str(msg_data.get("side", "yes_ask")).lower()
                    raw_price = msg_data.get("price_dollars", msg_data.get("price", 0))
                    price = L2OrderBook.parse_price_cents(raw_price)
                    raw_delta = msg_data.get("delta_fp", msg_data.get("delta", 0))
                    delta = int(round(float(raw_delta)))
                    if raw_side in ("yes", "bid", "yes_bid"):
                        book.apply_delta("yes_bid", price, delta, seq=seq)
                    elif raw_side in ("no", "no_bid"):
                        book.apply_delta("no", price, delta, seq=seq)
                    else:
                        book.apply_delta("yes_ask", price, delta, seq=seq)
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
        price: Union[float, int],
        min_net_edge_pct: float = 2.5,
        model_prob: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Executes an entry trade with pre-trade checks:
        - Stale-Tick Watchdog check
        - L2 Book Depth VWAP sweep validation
        - Capped marketable limit order dispatch to avoid book-sweeping slippage
        """
        price_cents = int(round(price * 100)) if isinstance(price, float) and price <= 1.0 else int(round(price))

        if not self.watchdog.is_safe_to_trade():
            logger.error(f"[AsyncOrderManager] Trade blocked by Stale-Tick Watchdog! ({self.watchdog.halt_reason})")
            return {
                "status": "ABORTED_STALE_TICK",
                "reason": self.watchdog.halt_reason or "Market data latency exceeded safety threshold",
                "watchdog": self.watchdog.get_status()
            }

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

        if model_prob is not None:
            vwap_dollars = effective_vwap_cents / 100.0
            fee_per_contract = calculate_kalshi_taker_fee(1, vwap_dollars)
            net_edge = model_prob - (vwap_dollars + fee_per_contract)
            if (net_edge * 100.0) < min_net_edge_pct:
                logger.warning(f"[AsyncOrderManager] Aborting trade on {ticker}: Post-sweep net edge ({net_edge*100:.1f}%) < min {min_net_edge_pct}%.")
                return {
                    "status": "ABORTED_INSUFFICIENT_EDGE",
                    "reason": f"Post-sweep net edge {net_edge*100:.1f}% below minimum {min_net_edge_pct}%"
                }

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
        chase_poll_ms: int = 800,
    ) -> Dict[str, Any]:
        """Smart limit-chasing stop loss execution."""
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
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()


# Aliases for unified naming convention
KalshiWebSocketSubscriptionManager = AsyncOrderManager
WebSocketSubscriptionManager = AsyncOrderManager
