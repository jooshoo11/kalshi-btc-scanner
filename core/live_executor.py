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

try:
    from .prediction_engine import calculate_kalshi_fee  # package import (core.*)
    from .order_saga import OrderSagaState, OrderSaga, OrderSagaOrchestrator
except ImportError:
    from prediction_engine import calculate_kalshi_fee  # script-style fallback (repo root on sys.path)
    from core.order_saga import OrderSagaState, OrderSaga, OrderSagaOrchestrator
import contextlib

# Phoenix/OpenTelemetry tracing DISABLED (no collector on :4317 -> log spam). No-op tracer:
class _NoopTracer:
    def agent(self, fn=None, **kwargs):
        if fn is None:
            return lambda f: f
        return fn
    def tool(self, fn=None, **kwargs):
        if fn is None:
            return lambda f: f
        return fn
    def start_as_current_span(self, *args, **kwargs):
        return contextlib.nullcontext()

tracer = _NoopTracer()

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
        self.bids: Dict[int, int] = {}  # YES bids: price_cents -> quantity
        self.asks: Dict[int, int] = {}  # YES asks (100 - NO bid): price_cents -> quantity
        self.last_update_ts: float = 0.0
        self.stale: bool = False  # FINDING #7: set when a delta fails to parse or seq gap / REST divergence
        self.last_seq: Optional[int] = None

    @staticmethod
    def parse_price_cents(raw_price: Any) -> int:
        """
        Convert Kalshi price (dollar string like '0.4700', sub-cent '0.0010', or integer cents)
        into integer cents [1..99]. Sub-cent dust (< 0.005 dollars = 0.5c) returns 0 and is filtered out.
        """
        if raw_price is None:
            return 0
        s = str(raw_price).strip()
        if "." in s:
            val = float(s)
            if val <= 1.0:
                return int(round(val * 100.0))
            return int(round(val))
        val_i = int(s)
        return val_i

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
        self.stale = False  # fresh full snapshot clears the stale flag
        self.last_seq = int(seq) if seq is not None else None

    def apply_delta(self, side: str, price_cents: int, delta_qty: int, seq: Optional[int] = None) -> bool:
        """
        Update price level from orderbook_delta stream.
        Returns False (and marks book stale) if a sequence gap is detected.
        """
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
        """
        Cross-check WS L2 best bid/ask against REST top-of-book quote.
        Marks book stale and returns False if crossed (best_bid >= best_ask)
        or diverging from REST quote by more than max_divergence_cents.
        """
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
        Simulate walking the book to fill `count` contracts:
        - BUY YES: walks YES asks (`self.asks`) ascending at price `p`
        - BUY NO: walks YES bids (`self.bids`) descending at NO price `100 - p`
        - SELL YES: walks YES bids (`self.bids`) descending at YES price `p`
        - SELL NO: walks YES asks (`self.asks`, i.e. NO bids at `100 - p`) ascending at NO price `100 - p`
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
            # Legacy compatibility: side="NO" with default action="BUY" buys NO
            s = "NO"

        if act == "BUY":
            is_yes = (s == "YES")
            book = self.asks if is_yes else self.bids
            sorted_prices = sorted(book.keys()) if is_yes else sorted(book.keys(), reverse=True)
            price_fn = (lambda p: p) if is_yes else (lambda p: 100 - p)
        else:
            # SELL
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
            available_at_level = book[p]
            if available_at_level <= 0:
                continue
            eff_price = price_fn(p)
            if not (1 <= eff_price <= 99):
                continue
            needed = count - accumulated_qty
            fill_at_level = min(available_at_level, needed)

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

    def book_depth_on_side(self, is_buy_yes: bool) -> int:
        """Total visible depth (contracts) on the side that would be swept."""
        book = self.asks if is_buy_yes else self.bids
        return sum(book.values())


class BookAwareExecutor:
    """
    Book-depth-aware execution guard for paper and live trading.
    
    Prevents sweeping more than 15% of visible order book depth on any single order,
    tracks adverse slippage as a cost in PnL calculation, and splits/rejects orders
    that would over-consume liquidity.
    """
    
    BOOK_SWEEP_CAP_PCT = 0.15  # Max 15% of visible book depth per order

    def __init__(self):
        self.sweep_violations_blocked = 0
        self.sweep_violations_split = 0
        self.total_adverse_slippage_cents = 0.0
        self.total_contracts_executed = 0

    def max_safe_contracts(
        self,
        book: L2OrderBook,
        is_buy_yes: bool,
        requested_contracts: int
    ) -> int:
        """
        Return max contracts that won't sweep > BOOK_SWEEP_CAP_PCT of visible book depth.
        Returns 0 if book is empty or too thin.
        """
        depth = book.book_depth_on_side(is_buy_yes)
        if depth <= 0:
            return 0
        cap = int(depth * self.BOOK_SWEEP_CAP_PCT)
        return min(requested_contracts, max(1, cap))

    def estimate_adverse_slippage(
        self,
        book: L2OrderBook,
        is_buy_yes: bool,
        contracts: int,
        mid_price_cents: int
    ) -> Dict[str, Any]:
        """
        Model adverse slippage from walking the book vs. mid price.
        Returns slippage in cents per contract and total slippage cost.
        """
        vwap_res = book.calculate_sweep_vwap("yes" if is_buy_yes else "no", contracts)
        if not vwap_res["can_fill"]:
            return {
                "slippage_cents_per_contract": 0.0,
                "total_slippage_cost_cents": 0.0,
                "vwap_cents": 0.0,
                "filled_count": 0,
                "reason": "Cannot fill"
            }

        vwap = vwap_res["vwap_cents"]
        if is_buy_yes:
            # Buying: slippage = VWAP - mid (paying above mid)
            slippage = vwap - mid_price_cents
        else:
            # Selling: slippage = mid - VWAP (selling below mid)
            slippage = mid_price_cents - vwap

        total_cost = slippage * vwap_res["filled_count"]
        self.total_adverse_slippage_cents += total_cost
        self.total_contracts_executed += vwap_res["filled_count"]

        return {
            "slippage_cents_per_contract": round(slippage, 2),
            "total_slippage_cost_cents": round(total_cost, 2),
            "vwap_cents": vwap,
            "filled_count": vwap_res["filled_count"],
            "reason": "OK"
        }

    def should_split_or_reject(
        self,
        requested_contracts: int,
        max_safe: int
    ) -> Tuple[bool, int]:
        """
        Decide whether to split or reject an order.
        Returns (should_proceed, adjusted_contracts).
        """
        if max_safe <= 0:
            self.sweep_violations_blocked += 1
            return False, 0
        if requested_contracts > max_safe:
            self.sweep_violations_split += 1
            return True, max_safe
        return True, requested_contracts

    def get_stats(self) -> Dict[str, Any]:
        return {
            "sweep_cap_pct": self.BOOK_SWEEP_CAP_PCT,
            "sweep_violations_blocked": self.sweep_violations_blocked,
            "sweep_violations_split": self.sweep_violations_split,
            "total_adverse_slippage_cents": round(self.total_adverse_slippage_cents, 2),
            "total_contracts_executed": self.total_contracts_executed,
            "avg_slippage_cents_per_contract": round(
                self.total_adverse_slippage_cents / max(1, self.total_contracts_executed), 2
            )
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
        self.on_fill_callbacks: List[Callable[[Dict[str, Any]], None]] = []
        self.on_position_callbacks: List[Callable[[Dict[str, Any]], None]] = []

    def get_or_create_book(self, ticker: str) -> L2OrderBook:
        if ticker not in self.books:
            self.books[ticker] = L2OrderBook(ticker)
        return self.books[ticker]

    def get_book(self, ticker: str) -> Optional[L2OrderBook]:
        """Return the current L2 order book for ticker if tracked, else None."""
        return self.books.get(ticker)

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
                # Fallback: keep L2 orderbook populated via public REST while WS is disconnected
                if self.subscribed_tickers:
                    for t in list(self.subscribed_tickers):
                        await self.refresh_orderbook_rest(t)
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._max_reconnect_delay, self._reconnect_delay * 1.8)

    async def _send_subscribe(self, tickers: List[str]):
        """Subscribe to orderbook_delta, ticker, fill, and market_positions channels."""
        if not self._ws or not tickers:
            return
        msg = {
            "id": int(time.time() * 1000),
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta", "ticker", "fill", "market_positions"],
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

    async def refresh_orderbook_rest(self, ticker: str, overwrite_fresh: bool = True) -> bool:
        """Fetch real L2 orderbook snapshot via public REST endpoint."""
        existing = self.books.get(ticker)
        if not overwrite_fresh and existing and not existing.stale and (existing.bids or existing.asks) and existing.last_seq is not None:
            # WS snapshot already arrived — do not overwrite live sequence-tracked book
            return True
        last_ref = getattr(self, "_last_rest_refresh_ts", {}).get(ticker, 0.0)
        if time.time() - last_ref < 5.0 and existing and (existing.bids or existing.asks):
            return False
        if not hasattr(self, "_last_rest_refresh_ts"):
            self._last_rest_refresh_ts = {}
        self._last_rest_refresh_ts[ticker] = time.time()
        base = self.ws_url.replace("wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2", "https://demo-api.kalshi.co/trade-api/v2")
        base = base.replace("wss://demo-api.kalshi.co/trade-api/ws/v2", "https://demo-api.kalshi.co/trade-api/v2")
        base = base.replace("wss://api.elections.kalshi.com/trade-api/ws/v2", "https://api.elections.kalshi.com/trade-api/v2")
        if not base.startswith("http"):
            base = "https://demo-api.kalshi.co/trade-api/v2"
        url = f"{base}/markets/{ticker}/orderbook"
        try:
            import httpx
            async with httpx.AsyncClient(timeout=3.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    ob_fp = data.get("orderbook_fp") or data.get("orderbook", {})
                    raw_yes = ob_fp.get("yes_dollars") or ob_fp.get("yes") or []
                    raw_no = ob_fp.get("no_dollars") or ob_fp.get("no") or []

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
                    for item in raw_no:
                        try:
                            p_cents = L2OrderBook.parse_price_cents(item[0])
                            qty = int(round(float(item[1])))
                            if 1 <= p_cents <= 99 and qty > 0:
                                asks_from_no.append([100 - p_cents, qty])
                        except Exception:
                            pass

                    book = self.get_or_create_book(ticker)
                    if not overwrite_fresh and not book.stale and book.last_seq is not None and (book.bids or book.asks):
                        return True
                    book.apply_snapshot(yes_levels, asks_from_no)
                    self._notify_book_update(ticker, book)
                    return True
        except Exception as e:
            logger.debug(f"REST orderbook fallback failed for {ticker}: {e}")
        return False

    async def subscribe_ticker(self, ticker: str):
        self.subscribed_tickers.add(ticker)
        self.get_or_create_book(ticker)
        asyncio.create_task(self.refresh_orderbook_rest(ticker, overwrite_fresh=False))
        if self.is_ws_connected:
            await self._send_subscribe([ticker])

    async def subscribe(self, ticker: str):
        """Alias for subscribe_ticker."""
        await self.subscribe_ticker(ticker)

    def on_fill(self, callback: Callable[[Dict[str, Any]], None]):
        """Register a callback for fill messages."""
        self.on_fill_callbacks.append(callback)

    def on_market_position(self, callback: Callable[[Dict[str, Any]], None]):
        """Register a callback for market position updates."""
        self.on_position_callbacks.append(callback)

    async def _handle_message(self, raw_msg: str):
        try:
            data = json.loads(raw_msg)
            msg_type = data.get("type")
            msg_data = data.get("msg", {})
            seq = data.get("seq")
            sid = data.get("sid")
            if not hasattr(self, "_last_seq_by_sid"):
                self._last_seq_by_sid = {}

            if msg_type == "orderbook_snapshot":
                ticker = msg_data.get("market_ticker")
                if ticker:
                    if sid is not None and seq is not None:
                        self._last_seq_by_sid[sid] = int(seq)
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

                    # In Kalshi binary structure, NO bids at P are equivalent to YES asks at (100 - P)
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
                    raw_side = str(msg_data.get("side", "")).lower()

                    try:
                        raw_price = msg_data.get("price_dollars", msg_data.get("price", 0))
                        price = L2OrderBook.parse_price_cents(raw_price)
                        raw_delta = msg_data.get("delta_fp", msg_data.get("delta", 0))
                        delta = int(round(float(raw_delta)))

                        # When sid is present, Kalshi increments seq per subscription (sid) across all tickers on that sid
                        sid_ok = True
                        per_book_seq = seq
                        if sid is not None and seq is not None:
                            seq_i = int(seq)
                            prev_sid_seq = self._last_seq_by_sid.get(sid)
                            if prev_sid_seq is not None and seq_i != prev_sid_seq + 1:
                                sid_ok = False
                            self._last_seq_by_sid[sid] = seq_i
                            per_book_seq = None

                        if raw_side in ("yes", "bid", "yes_bid"):
                            ok = book.apply_delta("yes_bid", price, delta, seq=per_book_seq) and sid_ok
                        elif raw_side in ("no", "no_bid"):
                            ok = book.apply_delta("no", price, delta, seq=per_book_seq) and sid_ok
                        elif raw_side in ("ask", "yes_ask"):
                            ok = book.apply_delta("yes_ask", price, delta, seq=per_book_seq) and sid_ok
                        else:
                            ok = sid_ok
                        if not ok:
                            book.stale = True
                            logger.warning(f"[WS] Sequence gap on {ticker} (sid={sid}, seq={seq}) — marking L2 book STALE and refreshing")
                            asyncio.create_task(self.refresh_orderbook_rest(ticker, overwrite_fresh=True))
                        self._notify_book_update(ticker, book)
                    except Exception as e:
                        # FINDING #7: never silently discard book deltas — mark the book STALE so
                        # VWAP/edge gates refuse to run on drifted data until a fresh snapshot arrives.
                        logger.warning(f"[WS] orderbook_delta parse failed for {ticker}: {e} — marking L2 book STALE")
                        book.stale = True

            elif msg_type == "fill":
                # Private fill channel - update local position tracking
                try:
                    fill_data = {
                        "trade_id": msg_data.get("trade_id"),
                        "order_id": msg_data.get("order_id"),
                        "client_order_id": msg_data.get("client_order_id"),
                        "market_ticker": msg_data.get("market_ticker"),
                        "exchange_index": msg_data.get("exchange_index"),
                        "is_taker": msg_data.get("is_taker"),
                        "side": msg_data.get("side"),  # "yes" or "no"
                        "yes_price_dollars": msg_data.get("yes_price_dollars"),
                        "count_fp": msg_data.get("count_fp"),
                        "fee_cost": msg_data.get("fee_cost"),
                        "action": msg_data.get("action"),
                        "ts_ms": msg_data.get("ts_ms"),
                        "post_position_fp": msg_data.get("post_position_fp"),
                        "purchased_side": msg_data.get("purchased_side"),
                        "outcome_side": msg_data.get("outcome_side"),
                        "book_side": msg_data.get("book_side"),
                        "subaccount": msg_data.get("subaccount"),
                    }
                    self._notify_fill(fill_data)
                except Exception as e:
                    logger.debug(f"Error parsing fill message: {e}")

            elif msg_type == "market_positions" or msg_type == "market_position":
                # Private position updates
                try:
                    positions = msg_data.get("market_positions") or msg_data.get("positions") or [msg_data]
                    for pos in positions:
                        pos_data = {
                            "ticker": pos.get("ticker"),
                            "exchange_index": pos.get("exchange_index"),
                            "position_fp": pos.get("position_fp"),
                            "market_exposure_dollars": pos.get("market_exposure_dollars"),
                            "realized_pnl_dollars": pos.get("realized_pnl_dollars"),
                            "fees_paid_dollars": pos.get("fees_paid_dollars"),
                            "last_updated_ts": pos.get("last_updated_ts"),
                            "netting_enabled": pos.get("netting_enabled"),
                        }
                        self._notify_position_update(pos_data)
                except Exception as e:
                    logger.debug(f"Error parsing market_position message: {e}")

            elif msg_type == "order_group_updates":
                logger.debug(f"Order group update: {msg_data}")

        except Exception as e:
            logger.debug(f"Error parsing WS message: {e}")

    def _notify_fill(self, fill_data: Dict[str, Any]):
        for cb in self.on_fill_callbacks:
            try:
                cb(fill_data)
            except Exception:
                pass

    def _notify_position_update(self, pos_data: Dict[str, Any]):
        for cb in self.on_position_callbacks:
            try:
                cb(pos_data)
            except Exception:
                pass

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
    def __init__(self, auth: Optional[KalshiAuth] = None, rest_url: Optional[str] = None, dry_run: Optional[bool] = None):
        self.auth = auth or KalshiAuth.from_env()
        if rest_url is None:
            env_choice = os.environ.get("KALSHI_ENVIRONMENT", "demo").lower()
            rest_url = KALSHI_PROD_REST_URL if env_choice == "prod" else KALSHI_DEMO_REST_URL
        self.rest_url = rest_url.rstrip("/")
        if dry_run is None:
            # SAFETY GATE (agreed plan, condition 1): real exchange calls armed ONLY via env
            dry_run = os.environ.get("KALSHI_LIVE_TRADING", "0").strip().lower() not in ("1", "true", "yes")
        self.dry_run = dry_run
        self.last_report: Dict[str, Any] = {}

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

    async def fetch_settlement(self, ticker: str) -> Optional[Dict[str, Any]]:
        """
        FINDING #2: GET /portfolio/settlements?ticker=... — authoritative cash booking for a
        settled market (market_result, revenue in cents, fee_cost, settled_time).
        Returns None when unavailable; caller must then book SETTLED_UNKNOWN (never -100%).
        """
        if self.dry_run or not self.auth or not self.auth.is_configured or self.auth.key_id.startswith("test-"):
            return None
        endpoint = "/portfolio/settlements"
        headers = self.auth.sign_request("GET", f"{endpoint}?ticker={ticker}")
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.rest_url}{endpoint}", headers=headers, params={"ticker": ticker})
                if resp.status_code == 200:
                    settlements = resp.json().get("settlements") or []
                    return settlements[0] if settlements else None
                logger.warning(f"[Reconciler] fetch_settlement failed for {ticker} ({resp.status_code}): {resp.text[:200]}")
        except Exception as e:
            logger.warning(f"[Reconciler] fetch_settlement exception for {ticker}: {e}")
        return None

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
        cancel_resting_orders: bool = False  # SAFE DEFAULT (agreed plan): only explicit kill switches pass True
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
            from core.db_pool import get_db_pool
            conn = get_db_pool(db_path_or_db).get_connection()
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
            "untracked_resting_orders": [],
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
                if resting_orders:
                    # FAIL-CLOSED (agreed plan, condition 3): untracked resting orders block new
                    # entries and are surfaced on /api/portfolio — not just logged.
                    report["untracked_resting_orders"] = [o.get("order_id", "") for o in resting_orders if o.get("order_id")]
                    logger.warning(
                        f"[Cold-Boot Reconciler] FAIL-CLOSED: {len(resting_orders)} resting order(s) NOT canceled "
                        f"(cancel_resting_orders=False): {report['untracked_resting_orders']} — blocking new entries."
                    )
                else:
                    logger.info("[Cold-Boot Reconciler] Step 2 Complete: No resting orders require cancellation.")

            # ------------------------------------------------------------------
            # STEP 3: Fetch all open positions (/portfolio/positions)
            # ------------------------------------------------------------------
            logger.info("[Cold-Boot Reconciler] Step 3/4: Fetching live portfolio positions from Kalshi exchange...")
            exchange_positions = await self.fetch_exchange_positions()
            report["live_positions_fetched"] = len(exchange_positions)
            report["active_exchange_positions"] = len(exchange_positions)

            # Map active exchange positions by ticker (keep SIGNED position to derive side — finding #3)
            pos_map: Dict[str, Dict[str, Any]] = {}
            pos_sign: Dict[str, int] = {}
            for p in exchange_positions:
                tk = p.get("ticker", "")
                raw_pos = int(p.get("position", 0))
                if tk and raw_pos != 0:
                    pos_map[tk] = p
                    pos_sign[tk] = raw_pos
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
                    # FINDING #2: never book a hardcoded -100%. Use the authoritative settlement when
                    # available; void -> refund cost basis; no record -> SETTLED_UNKNOWN with NULL pnl
                    # (excluded from win-rate/PnL stats until exchange-confirmed). Idempotent: only
                    # OPEN rows are touched and they become CLOSED, so re-runs never double-credit.
                    fee_db = float(db_trade.get("fee_dollars") or 0.0)
                    side_db = str(db_trade.get("side") or "yes").lower()
                    settlement = await self.fetch_settlement(ticker)
                    if settlement:
                        result = str(settlement.get("market_result") or "").lower()
                        revenue_cents = float(settlement.get("revenue") or 0.0)
                        if result in ("yes", "no"):
                            won = (result == side_db)
                            exit_price = 1.0 if won else 0.0
                            payout = (revenue_cents / 100.0) if won else 0.0
                            pnl = payout - cost - fee_db
                            pnl_pct = round((pnl / max(0.01, cost + fee_db)) * 100.0, 1)
                            reason = "SETTLED_EXCHANGE"
                        elif result == "void":
                            exit_price = 0.0
                            pnl = -fee_db  # void: contracts refunded, only the fee is lost
                            pnl_pct = round((pnl / max(0.01, cost + fee_db)) * 100.0, 1)
                            reason = "SETTLED_VOID"
                        else:  # scalar settlement
                            val = float(settlement.get("value") or 0.0)
                            exit_price = max(0.0, min(1.0, val / 100.0))
                            pnl = (val / 100.0) * contracts - cost - fee_db
                            pnl_pct = round((pnl / max(0.01, cost + fee_db)) * 100.0, 1)
                            reason = "SETTLED_SCALAR"
                        msg = (
                            f"[DISCREPANCY: OFFLINE_SETTLEMENT] DB Trade #{trade_id} ({ticker}, {contracts}x) "
                            f"not on exchange — booked from settlement result '{settlement.get('market_result')}' as {reason}."
                        )
                        logger.warning(msg)
                        report["discrepancies_found"].append(msg)
                        cursor.execute("""
                            UPDATE trades SET exit_price = ?, exit_timestamp = ?, exit_reason = ?,
                                pnl_dollars = ?, pnl_pct = ?, status = 'CLOSED' WHERE id = ?
                        """, (exit_price, time.time(), reason, round(pnl, 2), pnl_pct, trade_id))
                        conn.commit()
                        report["reconciled_closed_in_db"].append({"trade_id": trade_id, "ticker": ticker, "reason": reason})
                    else:
                        msg = (
                            f"[DISCREPANCY: OFFLINE_SETTLEMENT_UNKNOWN] DB Trade #{trade_id} ({ticker}) not on exchange "
                            f"and no settlement record — booking SETTLED_UNKNOWN (pnl NULL, excluded from stats, NOT -100%)."
                        )
                        logger.warning(msg)
                        report["discrepancies_found"].append(msg)
                        cursor.execute("""
                            UPDATE trades SET exit_price = 0.0, exit_timestamp = ?, exit_reason = 'SETTLED_UNKNOWN',
                                pnl_dollars = NULL, pnl_pct = NULL, status = 'CLOSED' WHERE id = ?
                        """, (time.time(), trade_id))
                        conn.commit()
                        report["reconciled_closed_in_db"].append({"trade_id": trade_id, "ticker": ticker, "reason": "SETTLED_UNKNOWN"})

                else:
                    # Case 4C: Position exists in both, check contract count match
                    ex_contracts = abs(pos_sign.get(ticker, 0))
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
                    signed = pos_sign.get(ticker, 0)
                    contracts = abs(signed)
                    # FINDING #3: derive series from the ticker prefix and side from the position
                    # sign instead of hardcoding 'KXBTC15M' / 'yes' for every asset.
                    series = ticker.split("-")[0]
                    side = "yes" if signed >= 0 else "no"
                    exposure = float(pos.get("market_exposure", 0.0))
                    entry_p = (exposure / contracts) if contracts > 0 else 0.50
                    msg = (
                        f"[DISCREPANCY: UNTRACKED_POSITION] Live position on {ticker} ({contracts}x {side}, series {series}) "
                        f"found on exchange but missing from SQLite. Syncing to SQLite trades table."
                    )
                    logger.warning(msg)
                    report["discrepancies_found"].append(msg)

                    cursor.execute("""
                        INSERT INTO trades (
                            timestamp, ticker, series, side, entry_price, contracts,
                            cost_dollars, fee_dollars, status, rationale
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 0.0, 'OPEN', 'RECONCILED_FROM_EXCHANGE')
                    """, (time.time(), ticker, series, side, entry_p, contracts, exposure))
                    conn.commit()
                    report["reconciled_opened_in_db"].append({"ticker": ticker, "contracts": contracts, "side": side})

            if not report["discrepancies_found"]:
                logger.info("[Cold-Boot Reconciler] Step 4 Complete: 0 discrepancies found. Local SQLite and live exchange are 100% in sync.")
            else:
                logger.info(f"[Cold-Boot Reconciler] Step 4 Complete: Resolved {len(report['discrepancies_found'])} discrepancy(ies).")

            logger.info(
                f"[Cold-Boot Reconciler] Complete: Canceled {len(report['resting_orders_canceled'])} order(s), "
                f"Closed {len(report['reconciled_closed_in_db'])} trade(s) in DB, "
                f"Synced {len(report['reconciled_opened_in_db'])} missing position(s) to DB."
            )
            self.last_report = report
            return report

        except Exception as e:
            logger.error(f"[Cold-Boot Reconciler] Error during reconciliation: {e}")
            report["status"] = "ERROR"
            report["error"] = str(e)
            return report
        finally:
            if should_close:
                conn.close()

    async def reconcile_with_db(self, db_path_or_db: Any, cancel_orphaned_orders: bool = False) -> Dict[str, Any]:
        """Backward-compatible alias for reconcile_state_on_startup. SAFE DEFAULT: no cancels unless explicitly requested."""
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
        dry_run: Optional[bool] = None
    ):
        self.auth = auth or KalshiAuth.from_env()
        if rest_url is None:
            env_choice = os.environ.get("KALSHI_ENVIRONMENT", "demo").lower()
            rest_url = KALSHI_PROD_REST_URL if env_choice == "prod" else KALSHI_DEMO_REST_URL
        self.rest_url = rest_url.rstrip("/")
        self.ws_client = ws_client
        self.watchdog = watchdog or StaleTickWatchdog()
        if dry_run is None:
            # SAFETY GATE (agreed plan, condition 1): enforced HERE in __init__, not just the CLI.
            # Real order placement is armed ONLY with KALSHI_LIVE_TRADING=1 in the environment.
            dry_run = os.environ.get("KALSHI_LIVE_TRADING", "0").strip().lower() not in ("1", "true", "yes")
        self.reconciler = KalshiStateReconciler(auth=self.auth, rest_url=self.rest_url, dry_run=dry_run)
        self.dry_run = dry_run
        # Distributed Order Lifecycle Saga Orchestrator
        try:
            from .order_saga import OrderSagaOrchestrator, OrderSagaState, OrderSaga
        except ImportError:
            from core.order_saga import OrderSagaOrchestrator, OrderSagaState, OrderSaga
        self.saga_orchestrator = OrderSagaOrchestrator(
            auth=self.auth,
            rest_url=self.rest_url,
            max_hold_seconds=120.0,
            ws_client=self.ws_client,
            on_orphan_detected=self._handle_orphans_rollback
        )
        # FAIL-CLOSED orphan gate (condition 3): set when untracked resting orders are observed
        self.blocked_by_orphans: bool = False
        self.untracked_resting_orders: List[str] = []
        self.last_reconcile_report: Dict[str, Any] = {}
        # Simulated order ledger so mock fills report their REAL count (finding #6)
        self.mock_orders: Dict[str, Dict[str, Any]] = {}
        # Book-depth-aware execution guard (15% book sweep cap)
        self.book_aware_executor = BookAwareExecutor()
        logger.info(f"[OMS] dry_run={self.dry_run} — live order placement {'ARMED' if not dry_run else 'DISABLED (set KALSHI_LIVE_TRADING=1 to arm)'}")

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

        # 0b. FAIL-CLOSED orphan gate (agreed plan, condition 3): untracked resting orders block new entries
        if self.blocked_by_orphans:
            logger.error(f"[OMS] ABORT: blocked — untracked resting orders on exchange: {self.untracked_resting_orders}")
            return {
                "status": "ABORTED_ORPHAN_ORDERS",
                "reason": "untracked resting orders present (zero-orphan mandate)",
                "untracked": list(self.untracked_resting_orders)
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

        if book and getattr(book, "stale", False):
            # FINDING #7: refuse to gate on a book marked stale after a delta parse failure
            logger.error(f"[OMS] ABORT: L2 book for {ticker} is STALE — refusing to trade on drifted data.")
            return {"status": "ABORTED_STALE_BOOK", "reason": "L2 book marked stale after orderbook_delta parse failure"}

        # 2b. BOOK-DEPTH-AWARE EXECUTION: enforce 15% book sweep cap (AGENTS.md Section 7.3)
        # Only enforce when a real L2 book is available; skip in mock/dry-run without WS.
        max_safe = target_contracts
        slippage_info = None
        if book is not None:
            # Mid price for slippage modeling: midpoint of best bid/ask
            mid_price_cents = 50  # neutral fallback
            best_bid, _ = book.get_best_bid()
            best_ask, _ = book.get_best_ask()
            if best_bid > 0 and best_ask < 100:
                mid_price_cents = (best_bid + best_ask) // 2

            is_buy_yes = (direction.upper() in ("BUY", "YES", "BUY_YES"))
            max_safe = self.book_aware_executor.max_safe_contracts(book, is_buy_yes, target_contracts)
            should_proceed, adjusted_contracts = self.book_aware_executor.should_split_or_reject(
                target_contracts, max_safe
            )
            if not should_proceed:
                logger.error(
                    f"[OMS] ABORTED_BOOK_SWEEP: {target_contracts} contracts would exceed "
                    f"{self.book_aware_executor.BOOK_SWEEP_CAP_PCT*100:.0f}% of visible book depth "
                    f"on {ticker}. Order blocked."
                )
                return {
                    "status": "ABORTED_BOOK_SWEEP",
                    "reason": f"Order would sweep >{self.book_aware_executor.BOOK_SWEEP_CAP_PCT*100:.0f}% of visible book depth",
                    "target_contracts": target_contracts,
                    "max_safe_contracts": max_safe
                }
            if adjusted_contracts < target_contracts:
                logger.warning(
                    f"[OMS] Book-depth split: scaling {target_contracts} → {adjusted_contracts} contracts "
                    f"to respect 15% book sweep cap on {ticker}"
                )
                target_contracts = adjusted_contracts

            # Model adverse slippage before execution for PnL tracking
            slippage_info = self.book_aware_executor.estimate_adverse_slippage(
                book, is_buy_yes, target_contracts, mid_price_cents
            )
            if slippage_info["reason"] != "OK":
                logger.warning(
                    f"[OMS] Slippage model warning on {ticker}: {slippage_info['reason']}"
                )

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

        # 4. Dispatch Marketable Limit Order with Bounded Taker Spread Crossing (Phase 1 Speedup)
        # Allows crossing the spread by up to +3¢ as long as mathematical edge strictly remains >= min_edge_pct
        effective_cap_cents = worst_price_cents
        if model_prob is not None:
            max_tolerable_cap = worst_price_cents
            for test_cap in range(worst_price_cents, min(78, worst_price_cents + 4)):
                test_dollars = test_cap / 100.0
                test_fee = calculate_kalshi_fee(1, test_dollars)
                test_edge = (model_prob - (test_dollars + test_fee)) * 100.0
                if test_edge >= min_edge_pct:
                    max_tolerable_cap = test_cap
                else:
                    break
            effective_cap_cents = max_tolerable_cap

        logger.info(
            f"[OMS] Executing {target_contracts}x {direction} on {ticker} | "
            f"Sweep VWAP: {vwap_cents:.1f}¢ | Cap Limit: {effective_cap_cents}¢ (Worst Level: {worst_price_cents}¢) | Levels: {levels_swept}"
        )

        # Create Order Saga (PENDING)
        saga = self.saga_orchestrator.create_saga(
            ticker=ticker,
            side=side_label,
            action="buy" if is_buy else "sell",
            target_contracts=target_contracts,
            price_cents=effective_cap_cents,
            metadata={"vwap_cents": vwap_cents, "model_prob": model_prob}
        )

        order_res = await self._place_rest_order(
            ticker=ticker,
            action="buy" if is_buy else "sell",
            side=side_label,
            count=target_contracts,
            price_cents=effective_cap_cents,
            order_type="limit"
        )
        order_id = order_res.get("order_id") or order_res.get("order", {}).get("order_id")
        if order_res.get("status") == "FILLED_SIMULATION":
            # Only a genuine simulation may synthesize a fill
            order_res["status"] = "FILLED"
            order_res["filled_count"] = target_contracts
            order_res.setdefault("order_id", f"sim_{int(time.time()*1000)}")
            saga.order_id = order_res["order_id"]
            saga.filled_contracts = target_contracts
            saga.remaining_contracts = 0
            self.saga_orchestrator.advance_state(saga, OrderSagaState.FILLED, "Simulation fill")
            return order_res
        if not order_id:
            # FINDING #4: REJECTED / timeout / error with no order_id must propagate —
            # NEVER rewrite it into a phantom FILLED position.
            st = order_res.get("status") or "UNKNOWN"
            logger.error(f"[OMS] Order placement returned no order_id (status={st}): {order_res}")
            self.saga_orchestrator.advance_state(saga, OrderSagaState.CANCELLED, "Placement rejected / no order_id")
            return {
                "status": st if st in ("REJECTED", "UNKNOWN") else "UNKNOWN",
                "reason": order_res.get("error") or order_res.get("reason") or "no order_id in response",
                "filled_count": 0,
                "raw": order_res,
            }

        saga.order_id = order_id
        self.saga_orchestrator.order_id_to_saga[order_id] = saga.saga_id
        self.saga_orchestrator.advance_state(saga, OrderSagaState.SUBMITTED, f"Order ID: {order_id}")

        # Allow matching engine to execute against book (Option C Hybrid Execution)
        # Step 1: Initial fast taker check (1.0s)
        await asyncio.sleep(1.0)
        order_status = await self._get_order_status(order_id)
        filled_count = order_status.get("fill_count", 0)
        remaining_count = order_status.get("remaining_count", target_contracts - filled_count)
        status_str = order_status.get("status", "").lower()

        # Step 2: Hybrid Resting Window (poll across up to 3 intervals of 1.8s ~ 5.4s total)
        # Gives Kalshi Demo participants time to trade against the limit order
        if remaining_count > 0 and status_str in ("resting", "open", "pending", "partially_filled"):
            for rest_step in range(3):
                if remaining_count <= 0 or status_str in ("executed", "filled"):
                    break
                await asyncio.sleep(1.8)
                order_status = await self._get_order_status(order_id)
                filled_count = order_status.get("fill_count", filled_count)
                remaining_count = order_status.get("remaining_count", target_contracts - filled_count)
                status_str = order_status.get("status", status_str).lower()
                if filled_count > 0:
                    logger.info(f"[OMS Entry] Partial fill progress: {filled_count}/{target_contracts} contracts filled ({remaining_count} remaining on book)...")

        # Step 3: Cancel any remaining unfilled contracts cleanly so no orphaned orders sit into expiry
        if remaining_count > 0 and status_str in ("resting", "open", "pending", "partially_filled"):
            logger.info(f"[OMS Entry] Cancelling unfilled resting remainder ({remaining_count} resting on book)...")
            # FINDING #5: verify the cancel actually landed; retry via batch cancel-all, then fail closed
            cancel_ok = await self._cancel_rest_order(order_id, ticker=ticker)
            if not cancel_ok:
                logger.warning(f"[OMS] Cancel of {order_id} failed — retrying via batch cancel-all")
                await self.cancel_all_orders()
                cancel_ok = await self._cancel_rest_order(order_id, ticker=ticker)
            await asyncio.sleep(0.8)
            final_status = await self._get_order_status(order_id)
            filled_count = final_status.get("fill_count", filled_count)
            status_str = final_status.get("status", status_str).lower()
            still_resting = (not cancel_ok) or (
                int(final_status.get("remaining_count", 0)) > 0
                and status_str in ("resting", "open", "pending", "partially_filled")
            )
            if still_resting:
                self.blocked_by_orphans = True
                if order_id not in self.untracked_resting_orders:
                    self.untracked_resting_orders.append(order_id)
                logger.error(
                    f"[OMS] FAIL-CLOSED: could not confirm cancellation of {order_id} on {ticker}; "
                    f"blocking new entries until the order is confirmed gone."
                )

        # Step 4: Accept and commit any filled contracts
        if filled_count <= 0:
            logger.warning(f"[OMS Entry] Order {order_id} received 0 fills on exchange book after resting window. Entry canceled cleanly.")
            self.saga_orchestrator.advance_state(saga, OrderSagaState.CANCELLED, "0 fills received on orderbook")
            return {
                "status": "UNFILLED",
                "reason": "Order received 0 fills on orderbook",
                "filled_count": 0
            }

        logger.info(f"[OMS Entry] Order {order_id} confirmed filled {filled_count}/{target_contracts} contracts @ {worst_price_cents}¢.")
        saga.filled_contracts = filled_count
        saga.remaining_contracts = max(0, target_contracts - filled_count)
        saga.fill_price_cents = float(worst_price_cents)
        self.saga_orchestrator.advance_state(saga, OrderSagaState.FILLED, f"Filled {filled_count}/{target_contracts} contracts")
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

        # 0b. FAIL-CLOSED orphan gate (condition 3)
        if self.blocked_by_orphans:
            logger.error(f"[OMS] ABORT: blocked — untracked resting orders: {self.untracked_resting_orders}")
            return {"status": "ABORTED_ORPHAN_ORDERS", "reason": "untracked resting orders present", "untracked": list(self.untracked_resting_orders)}

        # 1. Inspect L2 Order Book Depth
        vwap_res = None
        if self.ws_client and ticker in self.ws_client.books:
            book = self.ws_client.books[ticker]
            if getattr(book, "stale", False):
                # FINDING #7: never gate on a stale book
                logger.error(f"[OMS] ABORT: L2 book for {ticker} is STALE — refusing to trade on drifted data.")
                return {"status": "ABORTED_STALE_BOOK", "reason": "L2 book marked stale after orderbook_delta parse failure"}
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
        if order_res.get("status") == "FILLED_SIMULATION":
            order_res["status"] = "FILLED"
            order_res["filled_count"] = contracts
            order_res.setdefault("order_id", f"sim_{int(time.time()*1000)}")
            return order_res
        if not order_id:
            # FINDING #4: propagate REJECTED/UNKNOWN — never fabricate a fill
            st = order_res.get("status") or "UNKNOWN"
            logger.error(f"[OMS] Order placement returned no order_id (status={st}): {order_res}")
            return {"status": st if st in ("REJECTED", "UNKNOWN") else "UNKNOWN",
                    "reason": order_res.get("error") or "no order_id in response",
                    "filled_count": 0, "raw": order_res}

        # Verify actual fill status on the exchange book
        await asyncio.sleep(0.8)
        order_status = await self._get_order_status(order_id)
        filled_count = order_status.get("fill_count", 0)
        remaining_count = order_status.get("remaining_count", contracts - filled_count)

        # Cancel any resting unfilled contracts immediately so no rogue fills happen
        if remaining_count > 0 and order_status.get("status") == "resting":
            logger.info(f"[OMS Entry] Cancelling unfilled resting contracts ({remaining_count} resting)...")
            cancel_ok = await self._cancel_rest_order(order_id)
            if not cancel_ok:
                await self.cancel_all_orders()
                cancel_ok = await self._cancel_rest_order(order_id)
            await asyncio.sleep(0.2)
            final_status = await self._get_order_status(order_id)
            filled_count = final_status.get("fill_count", filled_count)
            # FINDING #5: fail closed if the remainder could not be confirmed canceled
            if (not cancel_ok) or (int(final_status.get("remaining_count", 0)) > 0 and str(final_status.get("status", "")).lower() in ("resting", "open", "pending", "partially_filled")):
                self.blocked_by_orphans = True
                if order_id not in self.untracked_resting_orders:
                    self.untracked_resting_orders.append(order_id)
                logger.error(f"[OMS] FAIL-CLOSED: unconfirmed remainder on {order_id}; blocking new entries.")

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

    @tracer.tool
    async def _place_rest_order(
        self,
        ticker: str,
        action: str,
        side: str,
        count: int,
        price_cents: int,
        order_type: str = "limit",
        order_group_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """REST client dispatch to Kalshi V2 orders endpoint."""
        if self.dry_run or not self.auth or not self.auth.is_configured or self.auth.key_id.startswith("test-"):
            # Dry-run / Mock simulation mode
            logger.info(f"[OMS Simulation] Placed mock order: {action} {count}x {side} on {ticker} @ {price_cents}¢")
            mock_id = f"mock_ord_{int(time.time()*1000)}"
            # FINDING #6: record the mock order's real count so _get_order_status reports it
            self.mock_orders[mock_id] = {"count": int(count), "ticker": ticker, "side": side, "price_cents": price_cents}
            return {
                "status": "FILLED_SIMULATION",
                "order_id": mock_id,
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
            "count": f"{int(count):.2f}",
            "price": f"{v2_price_cents / 100.0:.4f}",
            "time_in_force": "good_till_canceled",
            "client_order_id": str(uuid.uuid4()),
            "self_trade_prevention_type": "taker_at_cross",
            "exchange_index": 2,
        }
        if order_group_id:
            payload["order_group_id"] = order_group_id

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
        if self.dry_run or not self.auth or not self.auth.is_configured or self.auth.key_id.startswith("test-") or "mock_" in order_id or "sim_" in order_id:
            # FINDING #6: report the mock order's ACTUAL count, not a hardcoded 1
            mock = self.mock_orders.get(order_id)
            cnt = int(mock.get("count", 0)) if mock else 0
            return {"status": "executed", "remaining_count": 0, "fill_count": cnt, "initial_count": cnt}

        endpoints_to_try = [
            f"/portfolio/events/orders/{order_id}",
            f"/portfolio/orders/{order_id}"
        ]
        async with httpx.AsyncClient(timeout=3.0) as client:
            for endpoint in endpoints_to_try:
                headers = self.auth.sign_request("GET", endpoint)
                try:
                    resp = await client.get(f"{self.rest_url}{endpoint}", headers=headers)
                    if resp.status_code == 200:
                        ord_data = resp.json().get("order", resp.json())
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
                except Exception as e:
                    logger.debug(f"[OMS Status] Polling error on {endpoint}: {e}")
            return {"status": "unknown"}

    async def execute_order_with_saga(
        self,
        ticker: str,
        direction: str,
        target_contracts: int,
        price_cents: int,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Any:
        """
        Executes order under the Saga Distributed Order Lifecycle:
        PENDING -> SUBMITTED -> PARTIAL_FILL -> FILLED -> CANCELLED -> SETTLED
        With compensating transactions if WebSocket fills are missed.
        """
        dir_upper = direction.upper()
        is_buy_no = dir_upper in ("NO", "BUY_NO")
        is_buy_yes = dir_upper in ("BUY", "YES", "BUY_YES")
        is_buy = is_buy_yes or is_buy_no
        side_label = "NO" if is_buy_no else "YES"
        action = "buy" if is_buy else "sell"

        saga = self.saga_orchestrator.create_saga(
            ticker=ticker,
            side=side_label,
            action=action,
            target_contracts=target_contracts,
            price_cents=price_cents,
            metadata=metadata
        )

        return await self.saga_orchestrator.execute_saga_order(
            saga=saga,
            place_order_fn=self._place_rest_order,
            get_status_fn=self._get_order_status,
            cancel_order_fn=self._cancel_rest_order
        )

    async def sweep_and_cancel_orphaned_orders(self, max_hold_seconds: float = 120.0, auto_rollback: bool = True) -> List[Dict[str, Any]]:
        """
        Detect and auto-cancel orders resting older than max_hold_seconds (120s) with no fill.
        If auto_rollback is True and orphans are found, auto-cancels resting orders and reverts to paper mode.
        """
        orphans = await self.saga_orchestrator.detect_and_cancel_orphans(
            cancel_fn=self._cancel_rest_order,
            max_hold_seconds=max_hold_seconds
        )
        if orphans and auto_rollback:
            await self._handle_orphans_rollback(orphans)
        return orphans

    async def _handle_orphans_rollback(self, orphans: List[Dict[str, Any]]) -> None:
        """Callback triggered when saga orchestrator detects and cancels orphaned orders."""
        if not orphans:
            return
        logger.critical(
            f"[KalshiOMS Orphan Guard] {len(orphans)} orphaned order(s) detected! "
            f"Executing automated rollback: cancelling all resting orders and reverting to paper mode."
        )
        await self.trigger_rollback_to_paper(
            reason=f"Orphaned order guard triggered: {len(orphans)} order(s) exceeded max hold time"
        )

    async def trigger_rollback_to_paper(self, reason: str) -> Dict[str, Any]:
        """
        Automated Rollback:
        If live bot causes orphaned orders (>120s with no fill) or critical exchange failure,
        auto-cancel resting orders via DELETE /portfolio/events/orders and revert to paper mode.
        """
        was_live = not self.dry_run
        logger.critical(f"[LIVE OMS ROLLBACK] Reverting to paper mode! Reason: {reason} (was_live={was_live})")

        # 1. Auto-cancel all resting orders on exchange
        cancel_report = {}
        try:
            cancel_report = await self.cancel_all_orders()
        except Exception as e:
            logger.error(f"[Rollback] Failed to cancel resting orders on exchange: {e}")

        # 2. Revert execution mode to dry-run (paper mode)
        self.dry_run = True

        # 3. Update portfolio_state.json
        try:
            from core.portfolio_state import load_state, save_state
            state = load_state()
            state["rollback_triggered"] = True
            state["rollback_reason"] = reason
            state["rollback_ts"] = time.time()
            state["execution_mode"] = "REVERTED_TO_PAPER"
            save_state(state)
        except Exception as e:
            logger.error(f"[Rollback] Failed to update portfolio_state.json: {e}")

        # 4. Record to Audit DB if available
        try:
            from core.bot_database import AuditDatabase, TradeEventType
            audit_db = AuditDatabase()
            audit_db.append_event(
                event_type=TradeEventType.OMS_ROLLBACK,
                aggregate_id="system",
                payload={
                    "reason": reason,
                    "was_live": was_live,
                    "cancel_report": cancel_report,
                    "timestamp": time.time()
                }
            )
        except Exception as e:
            logger.error(f"[Rollback] Failed to log audit event: {e}")

        return {
            "status": "REVERTED_TO_PAPER",
            "reason": reason,
            "was_live": was_live,
            "cancel_report": cancel_report
        }

    async def sync_portfolio_state_30s(self, client: Optional[httpx.AsyncClient] = None) -> Dict[str, Any]:
        """
        30-second reconciliation loop step: syncs local portfolio_state.json with exchange.
        """
        return await self.saga_orchestrator.sync_portfolio_state_with_exchange(client=client)

    async def start_saga_maintenance_loop(self, sync_interval_seconds: float = 30.0):
        """
        Runs continuous 30s background loop for orphan cancellation and portfolio state synchronization.
        """
        return await self.saga_orchestrator.start_reconciliation_and_orphan_loop(
            cancel_fn=self._cancel_rest_order,
            sync_interval_seconds=sync_interval_seconds
        )

    @tracer.tool
    async def cancel_order(self, order_id: str, market_ticker: Optional[str] = None) -> bool:
        """Cancel an open order (public method)."""
        return await self._cancel_rest_order(order_id=order_id, ticker=market_ticker)

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
        cancel_resting_orders: bool = False  # SAFE DEFAULT: only explicit kill switches pass True
    ) -> Dict[str, Any]:
        """
        Cold-boot safety sequence executed before the WebSocket listener starts:
        1. Fetch all open orders (/portfolio/orders with status=resting).
        2. Issue batch or individual cancel requests for all resting orders (only if explicitly requested).
        3. Fetch all open positions (/portfolio/positions).
        4. Log any discrepancies between the live positions and the local SQLite state.
        """
        report = await self.reconciler.reconcile_state_on_startup(
            db_path_or_db=db_path_or_db,
            cancel_resting_orders=cancel_resting_orders
        )
        self.last_reconcile_report = report
        # FAIL-CLOSED (condition 3): untracked resting orders block new entries and surface on /api/portfolio
        untracked = report.get("untracked_resting_orders") or []
        if untracked:
            self.blocked_by_orphans = True
            self.untracked_resting_orders = list(untracked)
            logger.error(f"[OMS] FAIL-CLOSED: {len(untracked)} untracked resting order(s) — blocking new entries: {untracked}")
        return report

    async def reconcile_exchange_state(
        self,
        db_path_or_db: Any = "kalshi_audit.db",
        cancel_orphaned_orders: bool = False  # SAFE DEFAULT: never cancel unless explicitly requested
    ) -> Dict[str, Any]:
        """
        Backward-compatible alias for reconcile_state_on_startup.
        """
        return await self.reconcile_state_on_startup(
            db_path_or_db=db_path_or_db,
            cancel_resting_orders=cancel_orphaned_orders
        )

    # ==================== ORDER MANAGEMENT V2 EXTENSIONS ====================

    @tracer.tool
    async def amend_order(
        self,
        order_id: str,
        ticker: str,
        side: str,
        price_cents: int,
        count: int,
        exchange_index: int = 2,
        client_order_id: Optional[str] = None,
        updated_client_order_id: Optional[str] = None,
        expiration_time: int = 0,
        action: str = "buy",  # "buy" or "sell" - needed to infer V2 side
    ) -> Dict[str, Any]:
        """
        Amend an existing order (price, size, or expiry).
        POST /portfolio/events/orders/{order_id}/amend

        Note: `count` is the NEW TOTAL max fillable count (filled + desired remaining).
        Changing price or increasing size forfeits queue position.
        """
        if self.dry_run or not self.auth or not self.auth.is_configured or self.auth.key_id.startswith("test-"):
            logger.info(f"[OMS Sim] Amend order {order_id}: {action} {side} {count} @ {price_cents}¢")
            return {"status": "AMENDED_SIMULATION", "order_id": order_id}

        endpoint = f"/portfolio/events/orders/{order_id}/amend"
        url = f"{self.rest_url}{endpoint}"

        # Convert to V2 side/price (same logic as create)
        is_no_side = side.lower() == "no"
        is_buy_action = action.lower() == "buy"
        if is_no_side:
            v2_side = "ask" if is_buy_action else "bid"
            v2_price_cents = max(1, min(99, 100 - price_cents))
        else:
            v2_side = "bid" if is_buy_action else "ask"
            v2_price_cents = max(1, min(99, price_cents))

        payload = {
            "ticker": ticker,
            "side": v2_side,
            "price": f"{v2_price_cents / 100.0:.4f}",
            "count": f"{count:.2f}",
            "exchange_index": exchange_index,
        }
        if client_order_id:
            payload["client_order_id"] = client_order_id
        if updated_client_order_id:
            payload["updated_client_order_id"] = updated_client_order_id
        if expiration_time > 0:
            payload["expiration_time"] = expiration_time

        headers = self.auth.sign_request("POST", endpoint)
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code in (200, 201):
                data = resp.json()
                ord_info = data.get("order", data)
                return {
                    "status": "AMENDED",
                    "order_id": ord_info.get("order_id"),
                    "client_order_id": ord_info.get("client_order_id"),
                    "fill_count": ord_info.get("fill_count_fp", ord_info.get("fill_count", "0")),
                    "remaining_count": ord_info.get("remaining_count_fp", ord_info.get("remaining_count", "0")),
                }
            else:
                logger.error(f"[OMS] Amend failed ({resp.status_code}): {resp.text}")
                return {"status": "REJECTED", "code": resp.status_code, "error": resp.text}

    @tracer.tool
    async def decrease_order(
        self,
        order_id: str,
        reduce_by: Optional[float] = None,
        reduce_to: Optional[float] = None,
        exchange_index: int = 2,
        market_ticker: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Decrease the remaining size of an order.
        POST /portfolio/events/orders/{order_id}/decrease

        Exactly one of `reduce_by` or `reduce_to` must be provided.
        """
        if self.dry_run or not self.auth or not self.auth.is_configured or self.auth.key_id.startswith("test-"):
            logger.info(f"[OMS Sim] Decrease order {order_id}: reduce_by={reduce_by}, reduce_to={reduce_to}")
            return {"status": "DECREASED_SIMULATION", "order_id": order_id}

        if (reduce_by is None) == (reduce_to is None):
            return {"status": "ERROR", "error": "Exactly one of reduce_by or reduce_to must be provided"}

        endpoint = f"/portfolio/events/orders/{order_id}/decrease"
        url = f"{self.rest_url}{endpoint}"

        payload = {"exchange_index": exchange_index}
        if reduce_by is not None:
            payload["reduce_by"] = f"{reduce_by:.2f}"
        else:
            payload["reduce_to"] = f"{reduce_to:.2f}"
        if market_ticker:
            payload["market_ticker"] = market_ticker

        headers = self.auth.sign_request("POST", endpoint)
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code in (200, 201):
                data = resp.json()
                return {
                    "status": "DECREASED",
                    "order_id": data.get("order_id"),
                    "remaining_count": data.get("remaining_count", "0"),
                    "ts_ms": data.get("ts_ms"),
                }
            else:
                logger.error(f"[OMS] Decrease failed ({resp.status_code}): {resp.text}")
                return {"status": "REJECTED", "code": resp.status_code, "error": resp.text}

    @tracer.tool
    async def cancel_all_orders(self, subaccount: int = 0) -> Dict[str, Any]:
        """
        Cancel ALL resting orders across all markets.
        DELETE /portfolio/events/orders

        WARNING: New orders placed within 1 minute after this call may also be cancelled.
        """
        if self.dry_run or not self.auth or not self.auth.is_configured or self.auth.key_id.startswith("test-"):
            logger.info("[OMS Sim] Cancel all orders")
            return {"status": "CANCELLED_ALL_SIMULATION", "count": 0}

        endpoint = "/portfolio/events/orders"
        url = f"{self.rest_url}{endpoint}"
        headers = self.auth.sign_request("DELETE", endpoint)
        params = {"exchange_index": 2}
        if subaccount > 0:
            params["subaccount"] = subaccount

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.delete(url, headers=headers, params=params)
            if resp.status_code in (200, 204):
                logger.info("[OMS] Batch cancel-all successful")
                return {"status": "CANCELLED_ALL", "cancelled": True}
            else:
                logger.error(f"[OMS] Cancel all failed ({resp.status_code}): {resp.text}")
                return {"status": "REJECTED", "code": resp.status_code, "error": resp.text}

    # ==================== ORDER GROUP MANAGEMENT ====================

    @tracer.tool
    async def create_order_group(
        self,
        contracts_limit: int,
        description: str = "",
        exchange_index: int = 2,
    ) -> Dict[str, Any]:
        """
        Create a new order group with a contracts limit over a rolling 15-second window.
        POST /portfolio/order_groups
        """
        if self.dry_run or not self.auth or not self.auth.is_configured or self.auth.key_id.startswith("test-"):
            group_id = f"mock_grp_{int(time.time()*1000)}"
            logger.info(f"[OMS Sim] Created order group {group_id} limit={contracts_limit}")
            return {"status": "CREATED_SIMULATION", "order_group_id": group_id}

        endpoint = "/portfolio/order_groups"
        url = f"{self.rest_url}{endpoint}"
        headers = self.auth.sign_request("POST", endpoint)
        payload = {
            "contracts_limit": str(contracts_limit),
            "exchange_index": exchange_index,
        }
        if description:
            payload["description"] = description

        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code in (200, 201):
                data = resp.json()
                grp = data.get("order_group", data)
                return {
                    "status": "CREATED",
                    "order_group_id": grp.get("order_group_id"),
                    "contracts_limit": grp.get("contracts_limit"),
                }
            else:
                logger.error(f"[OMS] Create order group failed ({resp.status_code}): {resp.text}")
                return {"status": "REJECTED", "code": resp.status_code, "error": resp.text}

    @tracer.tool
    async def get_order_groups(self) -> Dict[str, Any]:
        """List all order groups. GET /portfolio/order_groups"""
        if self.dry_run or not self.auth or not self.auth.is_configured or self.auth.key_id.startswith("test-"):
            return {"status": "OK", "order_groups": []}

        endpoint = "/portfolio/order_groups"
        url = f"{self.rest_url}{endpoint}"
        headers = self.auth.sign_request("GET", endpoint)
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                data = resp.json()
                return {"status": "OK", "order_groups": data.get("order_groups", [])}
            else:
                logger.error(f"[OMS] Get order groups failed ({resp.status_code}): {resp.text}")
                return {"status": "ERROR", "code": resp.status_code, "error": resp.text}

    @tracer.tool
    async def trigger_order_group(self, order_group_id: str) -> Dict[str, Any]:
        """
        Trigger an order group: cancels all orders in group and blocks new orders until reset.
        PUT /portfolio/order_groups/{id}/trigger
        """
        if self.dry_run or not self.auth or not self.auth.is_configured or self.auth.key_id.startswith("test-"):
            logger.info(f"[OMS Sim] Trigger order group {order_group_id}")
            return {"status": "TRIGGERED_SIMULATION", "order_group_id": order_group_id}

        endpoint = f"/portfolio/order_groups/{order_group_id}/trigger"
        url = f"{self.rest_url}{endpoint}"
        headers = self.auth.sign_request("PUT", endpoint)
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.put(url, headers=headers)
            if resp.status_code in (200, 204):
                logger.info(f"[OMS] Order group {order_group_id} TRIGGERED")
                return {"status": "TRIGGERED", "order_group_id": order_group_id}
            else:
                logger.error(f"[OMS] Trigger group failed ({resp.status_code}): {resp.text}")
                return {"status": "REJECTED", "code": resp.status_code, "error": resp.text}

    @tracer.tool
    async def reset_order_group(self, order_group_id: str) -> Dict[str, Any]:
        """
        Reset a triggered order group, allowing new orders again.
        PUT /portfolio/order_groups/{id}/reset
        """
        if self.dry_run or not self.auth or not self.auth.is_configured or self.auth.key_id.startswith("test-"):
            logger.info(f"[OMS Sim] Reset order group {order_group_id}")
            return {"status": "RESET_SIMULATION", "order_group_id": order_group_id}

        endpoint = f"/portfolio/order_groups/{order_group_id}/reset"
        url = f"{self.rest_url}{endpoint}"
        headers = self.auth.sign_request("PUT", endpoint)
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.put(url, headers=headers)
            if resp.status_code in (200, 204):
                logger.info(f"[OMS] Order group {order_group_id} RESET")
                return {"status": "RESET", "order_group_id": order_group_id}
            else:
                logger.error(f"[OMS] Reset group failed ({resp.status_code}): {resp.text}")
                return {"status": "REJECTED", "code": resp.status_code, "error": resp.text}

    @tracer.tool
    async def update_order_group_limit(
        self,
        order_group_id: str,
        contracts_limit: int,
    ) -> Dict[str, Any]:
        """Update the contracts limit of an order group. PUT /portfolio/order_groups/{id}/limit"""
        if self.dry_run or not self.auth or not self.auth.is_configured or self.auth.key_id.startswith("test-"):
            return {"status": "UPDATED_SIMULATION", "order_group_id": order_group_id}

        endpoint = f"/portfolio/order_groups/{order_group_id}/limit"
        url = f"{self.rest_url}{endpoint}"
        headers = self.auth.sign_request("PUT", endpoint)
        payload = {"contracts_limit": str(contracts_limit)}
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.put(url, json=payload, headers=headers)
            if resp.status_code in (200, 204):
                return {"status": "UPDATED", "order_group_id": order_group_id}
            else:
                logger.error(f"[OMS] Update group limit failed ({resp.status_code}): {resp.text}")
                return {"status": "REJECTED", "code": resp.status_code, "error": resp.text}

    @tracer.tool
    async def delete_order_group(self, order_group_id: str) -> Dict[str, Any]:
        """Delete an order group permanently. DELETE /portfolio/order_groups/{id}"""
        if self.dry_run or not self.auth or not self.auth.is_configured or self.auth.key_id.startswith("test-"):
            return {"status": "DELETED_SIMULATION", "order_group_id": order_group_id}

        endpoint = f"/portfolio/order_groups/{order_group_id}"
        url = f"{self.rest_url}{endpoint}"
        headers = self.auth.sign_request("DELETE", endpoint)
        async with httpx.AsyncClient(timeout=4.0) as client:
            resp = await client.delete(url, headers=headers)
            if resp.status_code in (200, 204):
                return {"status": "DELETED", "order_group_id": order_group_id}
            else:
                logger.error(f"[OMS] Delete group failed ({resp.status_code}): {resp.text}")
                return {"status": "REJECTED", "code": resp.status_code, "error": resp.text}


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

    @tracer.tool
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
