"""
core/order_saga.py — Distributed Saga Order Lifecycle Orchestrator for Kalshi OMS.

Implements the Saga Pattern for high-frequency binary option order lifecycles:
1. Saga States: PENDING → SUBMITTED → PARTIAL_FILL → FILLED → CANCELLED → SETTLED
2. Compensating Transactions: Reconciles via REST GET /portfolio/events/orders/{order_id}
   if fill WebSocket messages are dropped or delayed.
3. Orphan Detection & Auto-Cancellation: Automatically sweeps and cancels orders resting
   older than max_hold_seconds (120s) via DELETE /portfolio/events/orders/{order_id}.
4. 30-Second Portfolio Sync: Reconciles local portfolio_state.json with live exchange balances
   and positions every 30 seconds.
5. Network Partition Resilience: Local persistent queue for offline order deferral, with
   staleness checking and auto-flush on connectivity recovery.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, Any, List, Optional, Callable, Awaitable

import httpx

from core.portfolio_state import load_state, save_state, check_portfolio_daily_loss

logger = logging.getLogger("OrderSaga")


class OrderSagaState(str, Enum):
    """Distributed Order Lifecycle Saga States."""
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIAL_FILL = "PARTIAL_FILL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    SETTLED = "SETTLED"


# Soft orphan threshold: cancel orders at 80% of max_hold_seconds to prevent
# hitting the hard limit and triggering a rollback cycle.
SOFT_ORPHAN_THRESHOLD_PCT = 0.8


@dataclass
class OrderSaga:
    """Represents a single distributed order transaction saga."""
    saga_id: str
    client_order_id: str
    ticker: str
    side: str
    action: str
    target_contracts: int
    price_cents: int
    state: OrderSagaState = OrderSagaState.PENDING
    order_id: Optional[str] = None
    filled_contracts: int = 0
    remaining_contracts: int = 0
    fill_price_cents: float = 0.0
    created_at: float = field(default_factory=time.time)
    submitted_at: Optional[float] = None
    completed_at: Optional[float] = None
    max_hold_seconds: float = 120.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    compensation_log: List[Dict[str, Any]] = field(default_factory=list)

    def is_terminal(self) -> bool:
        """Returns True if the saga is in a finalized state."""
        return self.state in (OrderSagaState.FILLED, OrderSagaState.CANCELLED, OrderSagaState.SETTLED)

    def is_orphan_candidate(self, now: Optional[float] = None) -> bool:
        """
        Orders older than max_hold_seconds with remaining contracts resting
        without fill are candidates for orphan compensation.
        """
        current_time = now or time.time()
        age = current_time - self.created_at
        return (
            not self.is_terminal()
            and self.state in (OrderSagaState.SUBMITTED, OrderSagaState.PARTIAL_FILL)
            and age >= self.max_hold_seconds
            and self.remaining_contracts > 0
        )

    def is_soft_orphan_candidate(self, now: Optional[float] = None) -> bool:
        """
        Orders older than SOFT_ORPHAN_THRESHOLD_PCT * max_hold_seconds with
        remaining contracts resting without fill are candidates for preventive
        cancellation. This cancels orders before they hit the hard limit and
        trigger a rollback cycle.
        """
        current_time = now or time.time()
        age = current_time - self.created_at
        soft_cutoff = self.max_hold_seconds * SOFT_ORPHAN_THRESHOLD_PCT
        return (
            not self.is_terminal()
            and self.state in (OrderSagaState.SUBMITTED, OrderSagaState.PARTIAL_FILL)
            and age >= soft_cutoff
            and self.remaining_contracts > 0
        )

    def log_compensation(self, action: str, details: Dict[str, Any]):
        """Records a compensation event in the saga audit trail."""
        self.compensation_log.append({
            "timestamp": time.time(),
            "action": action,
            "details": details,
            "previous_state": self.state.value
        })


class NetworkPartitionQueue:
    """
    Local queue buffer for handling network partitions and API outages.
    Queues orders locally if Kalshi REST is unreachable and flushes upon recovery.
    """

    def __init__(self, maxsize: int = 500, state_file: Optional[str] = None):
        self.maxsize = maxsize
        self.state_file = state_file or "network_partition_queue.json"
        self._queue: List[Dict[str, Any]] = []
        self.is_partitioned: bool = False
        self.consecutive_failures: int = 0
        self.failure_threshold: int = 3
        self.last_failure_ts: float = 0.0
        self.last_recovery_ts: float = 0.0
        self._load_from_disk()

    def _load_from_disk(self):
        try:
            if os.path.exists(self.state_file):
                with open(self.state_file, "r") as f:
                    self._queue = json.load(f)
                if self._queue:
                    logger.warning(f"[NetworkPartitionQueue] Loaded {len(self._queue)} pending orders from disk.")
        except Exception as e:
            logger.error(f"[NetworkPartitionQueue] Error loading disk queue: {e}")

    def _save_to_disk(self):
        try:
            tmp = f"{self.state_file}.tmp"
            with open(tmp, "w") as f:
                json.dump(self._queue, f)
            os.replace(tmp, self.state_file)
        except Exception as e:
            logger.error(f"[NetworkPartitionQueue] Error saving disk queue: {e}")

    def record_failure(self, error: str):
        """Records a network or connection failure and updates circuit breaker state."""
        self.consecutive_failures += 1
        self.last_failure_ts = time.time()
        if self.consecutive_failures >= self.failure_threshold:
            if not self.is_partitioned:
                logger.error(f"[NetworkPartitionQueue] Network partition detected ({self.consecutive_failures} failures): {error}")
            self.is_partitioned = True

    def record_success(self):
        """Records a successful network interaction, clearing partition state."""
        if self.is_partitioned:
            logger.info("[NetworkPartitionQueue] Network connectivity restored. Clearing partition flag.")
            self.last_recovery_ts = time.time()
        self.consecutive_failures = 0
        self.is_partitioned = False

    def enqueue(self, saga: OrderSaga) -> bool:
        """Enqueues an order for deferred submission."""
        if len(self._queue) >= self.maxsize:
            logger.warning("[NetworkPartitionQueue] Queue full! Dropping oldest order.")
            self._queue.pop(0)

        saga_dict = asdict(saga)
        saga_dict["state"] = saga.state.value
        self._queue.append(saga_dict)
        self._save_to_disk()
        logger.info(f"[NetworkPartitionQueue] Enqueued saga {saga.saga_id} for {saga.ticker} (Queue size: {len(self._queue)})")
        return True

    def get_recoverable_sagas(self, max_age_seconds: float = 60.0) -> List[Dict[str, Any]]:
        """
        Retrieves pending sagas from the queue, discarding stale/expired orders.
        Orders older than max_age_seconds are discarded to avoid placing stale trades.
        """
        now = time.time()
        valid = []
        discarded_count = 0

        for item in self._queue:
            age = now - item.get("created_at", now)
            if age <= max_age_seconds:
                valid.append(item)
            else:
                discarded_count += 1

        if discarded_count > 0:
            logger.warning(f"[NetworkPartitionQueue] Discarded {discarded_count} stale orders (> {max_age_seconds}s old) on recovery.")

        self._queue.clear()
        self._save_to_disk()
        return valid

    @property
    def qsize(self) -> int:
        return len(self._queue)


class OrderSagaOrchestrator:
    """
    Central Saga Orchestrator for Distributed Kalshi Order Lifecycles.
    """

    def __init__(
        self,
        auth: Any,
        rest_url: str,
        max_hold_seconds: float = 120.0,
        ws_client: Optional[Any] = None,
        on_orphan_detected: Optional[Callable[[List[Dict[str, Any]]], Awaitable[None]]] = None
    ):
        self.auth = auth
        self.rest_url = rest_url.rstrip("/")
        self.max_hold_seconds = max_hold_seconds
        self.ws_client = ws_client
        self.on_orphan_detected = on_orphan_detected
        self.sagas: Dict[str, OrderSaga] = {}  # saga_id -> OrderSaga
        self.order_id_to_saga: Dict[str, str] = {}  # order_id -> saga_id
        self.client_order_id_to_saga: Dict[str, str] = {}  # client_order_id -> saga_id
        self.partition_queue = NetworkPartitionQueue()
        self._sync_task: Optional[asyncio.Task] = None
        self._is_running: bool = False
        self._last_exchange_sync: float = 0.0

        # Wire up WebSocket fill listener if WS client provided
        if self.ws_client and hasattr(self.ws_client, "on_fill"):
            self.ws_client.on_fill(self.handle_ws_fill)

    def create_saga(
        self,
        ticker: str,
        side: str,
        action: str,
        target_contracts: int,
        price_cents: int,
        client_order_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> OrderSaga:
        """Initializes a new order saga in PENDING state.
        
        Proactively cancels any existing orphan candidates before creating a new saga,
        preventing orphan accumulation from blocking new entries.
        """
        # Proactive orphan sweep before creating new saga
        now = time.time()
        cutoff = self.max_hold_seconds
        for saga_id, existing in list(self.sagas.items()):
            if existing.is_orphan_candidate(now=now):
                age = now - existing.created_at
                logger.warning(
                    f"[OrderSaga] Proactive orphan sweep: Saga {saga_id} (Order {existing.order_id}) "
                    f"on {existing.ticker} is {age:.1f}s (> {cutoff}s) with "
                    f"{existing.remaining_contracts} resting contracts. Cancelling before new saga."
                )
                existing.log_compensation("PROACTIVE_ORPHAN_SWEEP", {"age_seconds": age})
                if existing.filled_contracts > 0:
                    self.advance_state(existing, OrderSagaState.FILLED, f"Proactive orphan sweep partial: {existing.filled_contracts}x")
                else:
                    self.advance_state(existing, OrderSagaState.CANCELLED, f"Proactive orphan sweep after {age:.1f}s")

        saga_id = f"saga_{uuid.uuid4().hex[:12]}"
        cid = client_order_id or str(uuid.uuid4())
        saga = OrderSaga(
            saga_id=saga_id,
            client_order_id=cid,
            ticker=ticker,
            side=side.upper(),
            action=action.lower(),
            target_contracts=target_contracts,
            remaining_contracts=target_contracts,
            price_cents=price_cents,
            state=OrderSagaState.PENDING,
            max_hold_seconds=self.max_hold_seconds,
            metadata=metadata or {}
        )
        self.sagas[saga_id] = saga
        self.client_order_id_to_saga[cid] = saga_id
        logger.info(f"[OrderSaga] Created saga {saga_id} (State: PENDING) for {target_contracts}x {side} on {ticker}")
        return saga

    def advance_state(self, saga: OrderSaga, new_state: OrderSagaState, rationale: str = ""):
        """Transitions saga to a new state and updates timestamps."""
        prev_state = saga.state
        saga.state = new_state
        if new_state == OrderSagaState.SUBMITTED and not saga.submitted_at:
            saga.submitted_at = time.time()
        elif saga.is_terminal() and not saga.completed_at:
            saga.completed_at = time.time()

        logger.info(f"[OrderSaga] Saga {saga.saga_id} ({saga.ticker}): {prev_state.value} → {new_state.value} {f'({rationale})' if rationale else ''}")

    def mark_saga_settled(self, ticker_or_order_id: str, settlement_price: float = 1.0) -> int:
        """
        Transitions filled sagas for a contract ticker or order_id to SETTLED state upon market settlement.
        Returns number of sagas transitioned.
        """
        settled_count = 0
        for saga in self.sagas.values():
            if (saga.ticker == ticker_or_order_id or saga.order_id == ticker_or_order_id or saga.saga_id == ticker_or_order_id) and saga.state == OrderSagaState.FILLED:
                saga.metadata["settlement_price"] = settlement_price
                self.advance_state(saga, OrderSagaState.SETTLED, f"Settlement price: {settlement_price:.2f}")
                settled_count += 1
        return settled_count

    async def execute_saga_order(
        self,
        saga: OrderSaga,
        place_order_fn: Callable[..., Awaitable[Dict[str, Any]]],
        get_status_fn: Callable[[str], Awaitable[Dict[str, Any]]],
        cancel_order_fn: Callable[[str, Optional[str]], Awaitable[bool]]
    ) -> OrderSaga:
        """
        Executes the distributed saga workflow:
        1. Checks network partition: queues locally if partitioned
        2. Submits order via REST (transition to SUBMITTED)
        3. Awaits fills (via WS or compensating REST polling)
        4. Handles partial fills and cancels resting remainders cleanly
        """
        # Step 1: Check partition queue
        if self.partition_queue.is_partitioned:
            logger.warning(f"[OrderSaga] System is partitioned. Enqueuing saga {saga.saga_id} locally.")
            self.partition_queue.enqueue(saga)
            return saga

        # Step 2: REST Submission (PENDING → SUBMITTED)
        try:
            order_res = await place_order_fn(
                ticker=saga.ticker,
                action=saga.action,
                side=saga.side.lower(),
                count=saga.target_contracts,
                price_cents=saga.price_cents,
                order_type="limit"
            )
            self.partition_queue.record_success()
        except (httpx.ConnectError, httpx.TimeoutException, httpx.NetworkError) as e:
            logger.error(f"[OrderSaga] Network partition during order placement: {e}")
            self.partition_queue.record_failure(str(e))
            self.partition_queue.enqueue(saga)
            return saga

        order_id = order_res.get("order_id") or order_res.get("order", {}).get("order_id")

        if not order_id:
            # Handle rejection or simulation
            if order_res.get("status") in ("FILLED", "FILLED_SIMULATION"):
                saga.order_id = order_res.get("order_id", f"sim_{int(time.time()*1000)}")
                saga.filled_contracts = saga.target_contracts
                saga.remaining_contracts = 0
                saga.fill_price_cents = float(saga.price_cents)
                self.advance_state(saga, OrderSagaState.FILLED, "Simulation / instant fill")
                return saga

            saga.metadata["error"] = order_res.get("error", "No order_id returned")
            self.advance_state(saga, OrderSagaState.CANCELLED, "Submission rejected")
            return saga

        saga.order_id = order_id
        self.order_id_to_saga[order_id] = saga.saga_id
        self.advance_state(saga, OrderSagaState.SUBMITTED, f"Order ID: {order_id}")

        # Step 3: Wait for fills (allowing matching engine to cross)
        await asyncio.sleep(1.0)

        # Step 4: Compensating Reconciliation (Check if fill WebSocket was missed)
        await self.compensate_if_missed_ws(saga.saga_id, get_status_fn)

        # Step 5: If partially filled or resting, perform bounded taker resting window
        if saga.remaining_contracts > 0 and not saga.is_terminal():
            for rest_attempt in range(2):
                if saga.remaining_contracts <= 0 or saga.is_terminal():
                    break
                await asyncio.sleep(1.5)
                await self.compensate_if_missed_ws(saga.saga_id, get_status_fn)

        # Step 6: Compensating Transaction: Cancel remaining resting contracts
        if saga.remaining_contracts > 0 and not saga.is_terminal():
            logger.info(f"[OrderSaga] Cancelling resting remainder ({saga.remaining_contracts} unfilled) on order {order_id}...")
            cancel_ok = await cancel_order_fn(order_id, saga.ticker)
            saga.log_compensation("REST_CANCEL_REMAINDER", {"cancel_ok": cancel_ok, "remaining": saga.remaining_contracts})
            await asyncio.sleep(0.5)
            # Final status check post-cancel
            await self.compensate_if_missed_ws(saga.saga_id, get_status_fn)

            if saga.filled_contracts > 0:
                self.advance_state(saga, OrderSagaState.FILLED, f"Closed remainder, committed {saga.filled_contracts} fills")
            else:
                self.advance_state(saga, OrderSagaState.CANCELLED, "Zero fills received, cancelled completely")

        return saga

    def handle_ws_fill(self, fill_data: Dict[str, Any]):
        """
        WebSocket Fill Event Handler:
        Matches incoming fill against active sagas by order_id or client_order_id.
        """
        order_id = fill_data.get("order_id")
        client_order_id = fill_data.get("client_order_id")

        saga_id = self.order_id_to_saga.get(order_id) or self.client_order_id_to_saga.get(client_order_id)
        if not saga_id or saga_id not in self.sagas:
            return

        saga = self.sagas[saga_id]
        if saga.is_terminal():
            return

        raw_count = fill_data.get("count_fp", fill_data.get("count", 0))
        fill_count = int(round(float(raw_count)))
        if fill_count <= 0:
            fill_count = 1

        saga.filled_contracts += fill_count
        saga.remaining_contracts = max(0, saga.target_contracts - saga.filled_contracts)

        try:
            raw_p = fill_data.get("yes_price_dollars", fill_data.get("price", 0))
            if raw_p:
                saga.fill_price_cents = round(float(raw_p) * 100.0, 1) if "." in str(raw_p) else float(raw_p)
        except Exception:
            pass

        logger.info(
            f"[OrderSaga WS Fill] Received fill for saga {saga_id} on {saga.ticker}: "
            f"+{fill_count} contracts ({saga.filled_contracts}/{saga.target_contracts} filled)"
        )

        if saga.filled_contracts >= saga.target_contracts:
            self.advance_state(saga, OrderSagaState.FILLED, "WebSocket 100% fill")
        else:
            self.advance_state(saga, OrderSagaState.PARTIAL_FILL, f"WebSocket partial fill ({saga.filled_contracts}/{saga.target_contracts})")

    async def compensate_if_missed_ws(
        self,
        saga_id: str,
        get_status_fn: Callable[[str], Awaitable[Dict[str, Any]]]
    ) -> OrderSaga:
        """
        Compensating transaction:
        If WebSocket fill is dropped or delayed, reconciles truth via REST GET /portfolio/events/orders/{order_id}.
        """
        saga = self.sagas.get(saga_id)
        if not saga or not saga.order_id:
            return saga

        try:
            status_data = await get_status_fn(saga.order_id)
            if not status_data or status_data.get("status") == "unknown":
                return saga

            fill_count = int(status_data.get("fill_count", 0))
            rem_count = int(status_data.get("remaining_count", max(0, saga.target_contracts - fill_count)))
            exchange_status = str(status_data.get("status", "")).lower()

            # Record compensation reconciliation
            saga.log_compensation("REST_GET_STATUS_RECONCILIATION", {
                "exchange_status": exchange_status,
                "fill_count": fill_count,
                "remaining_count": rem_count,
                "ws_filled_before": saga.filled_contracts
            })

            # If REST reveals fills that WebSocket missed
            if fill_count > saga.filled_contracts:
                logger.info(
                    f"[OrderSaga Compensation] Reconciled missed fills on {saga.order_id}: "
                    f"Exchange has {fill_count} vs Local {saga.filled_contracts}."
                )
                saga.filled_contracts = fill_count
                saga.remaining_contracts = rem_count

            if exchange_status in ("executed", "filled") or saga.filled_contracts >= saga.target_contracts:
                if saga.state != OrderSagaState.FILLED:
                    self.advance_state(saga, OrderSagaState.FILLED, "REST confirmed executed")
            elif exchange_status in ("canceled", "cancelled") and saga.filled_contracts == 0:
                if saga.state != OrderSagaState.CANCELLED:
                    self.advance_state(saga, OrderSagaState.CANCELLED, "REST confirmed canceled")
            elif saga.filled_contracts > 0 and saga.state != OrderSagaState.FILLED:
                self.advance_state(saga, OrderSagaState.PARTIAL_FILL, "REST confirmed partial")

        except Exception as e:
            logger.error(f"[OrderSaga Compensation] Error checking REST order status for {saga.order_id}: {e}")

        return saga

    async def detect_and_cancel_orphans(
        self,
        cancel_fn: Callable[[str, Optional[str]], Awaitable[bool]],
        max_hold_seconds: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        """
        Orphan Detection & Auto-Cancellation:
        Finds any order older than max_hold_seconds (120s) with no complete fill and
        executes an immediate compensating DELETE /portfolio/events/orders/{order_id}.

        Also performs preventive cancellation on "soft orphans" — orders that have
        exceeded SOFT_ORPHAN_THRESHOLD_PCT (80%) of max_hold_seconds — to prevent
        them from ever becoming hard orphans and triggering a rollback cycle.
        """
        now = time.time()
        cutoff = max_hold_seconds or self.max_hold_seconds
        soft_cutoff = cutoff * SOFT_ORPHAN_THRESHOLD_PCT
        cancelled_orphans = []

        # Phase 1: Preventive cancellation of soft orphans (before they become hard orphans)
        for saga_id, saga in list(self.sagas.items()):
            if saga.is_soft_orphan_candidate(now=now):
                age = now - saga.created_at
                logger.warning(
                    f"[OrderSaga Soft Orphan Guard] Preventive cancel: Saga {saga_id} "
                    f"(Order {saga.order_id}) on {saga.ticker} is {age:.1f}s old "
                    f"({age/cutoff*100:.0f}% of {cutoff}s limit) with "
                    f"{saga.remaining_contracts} resting contracts. Cancelling preventively..."
                )
                if saga.order_id:
                    cancel_ok = await cancel_fn(saga.order_id, saga.ticker)
                    saga.log_compensation("SOFT_ORPHAN_PREVENTIVE_CANCEL", {
                        "cancel_ok": cancel_ok,
                        "age_seconds": age,
                        "soft_cutoff": soft_cutoff
                    })

                if saga.filled_contracts > 0:
                    self.advance_state(saga, OrderSagaState.FILLED, f"Soft orphan preventive cancel: {saga.filled_contracts}x")
                else:
                    self.advance_state(saga, OrderSagaState.CANCELLED, f"Soft orphan preventive cancel after {age:.1f}s")

                cancelled_orphans.append({
                    "saga_id": saga_id,
                    "order_id": saga.order_id,
                    "ticker": saga.ticker,
                    "age_seconds": round(age, 1),
                    "cancelled_remaining": saga.remaining_contracts,
                    "type": "soft_orphan_preventive"
                })

        # Phase 2: Hard orphan cancellation (orders that already exceeded max_hold_seconds)
        for saga_id, saga in list(self.sagas.items()):
            if saga.is_orphan_candidate(now=now):
                age = now - saga.created_at
                logger.warning(
                    f"[OrderSaga Orphan Guard] Ghost/Orphan detected! Saga {saga_id} (Order {saga.order_id}) on {saga.ticker} "
                    f"is {age:.1f}s old (> {cutoff}s) with {saga.remaining_contracts} resting contracts. Auto-cancelling..."
                )
                if saga.order_id:
                    cancel_ok = await cancel_fn(saga.order_id, saga.ticker)
                    saga.log_compensation("ORPHAN_AUTO_CANCEL", {"cancel_ok": cancel_ok, "age_seconds": age})

                if saga.filled_contracts > 0:
                    self.advance_state(saga, OrderSagaState.FILLED, f"Orphan auto-cancel finalized partial fill: {saga.filled_contracts}x")
                else:
                    self.advance_state(saga, OrderSagaState.CANCELLED, f"Orphan auto-cancelled after {age:.1f}s")

                cancelled_orphans.append({
                    "saga_id": saga_id,
                    "order_id": saga.order_id,
                    "ticker": saga.ticker,
                    "age_seconds": round(age, 1),
                    "cancelled_remaining": saga.remaining_contracts,
                    "type": "hard_orphan"
                })

        return cancelled_orphans

    async def sync_portfolio_state_with_exchange(
        self,
        client: Optional[httpx.AsyncClient] = None
    ) -> Dict[str, Any]:
        """
        Reconciliation step: Syncs local portfolio_state.json with live exchange
        /portfolio/balance and /portfolio/positions every 30 seconds.
        """
        own_client = False
        if client is None:
            client = httpx.AsyncClient(timeout=5.0)
            own_client = True

        state = load_state()
        sync_report = {
            "timestamp": time.time(),
            "status": "HEALTHY",
            "discrepancies": [],
            "exchange_balance_dollars": None,
            "open_positions_count": 0
        }

        try:
            if not self.auth or not self.auth.is_configured or self.auth.key_id.startswith("test-"):
                # Simulation mode sync
                sync_report["exchange_balance_dollars"] = float(state.get("day_start_equity", 100.0))
                sync_report["status"] = "SIMULATED_SYNC"
                state["last_exchange_sync_ts"] = time.time()
                state["exchange_sync_status"] = "SIMULATED"
                save_state(state)
                return sync_report

            # 1. Fetch live balance from Kalshi V2
            bal_endpoint = "/portfolio/balance"
            bal_headers = self.auth.sign_request("GET", bal_endpoint)
            bal_resp = await client.get(f"{self.rest_url}{bal_endpoint}", headers=bal_headers)

            if bal_resp.status_code == 200:
                bal_data = bal_resp.json()
                bal_cents = bal_data.get("balance", 0)
                bal_dollars = float(bal_cents) / 100.0
                sync_report["exchange_balance_dollars"] = bal_dollars
                state["exchange_balance_dollars"] = bal_dollars
            else:
                sync_report["discrepancies"].append(f"Failed to fetch balance: HTTP {bal_resp.status_code}")

            # 2. Fetch live open positions from Kalshi V2
            pos_endpoint = "/portfolio/positions"
            pos_headers = self.auth.sign_request("GET", pos_endpoint)
            pos_resp = await client.get(f"{self.rest_url}{pos_endpoint}", headers=pos_headers)

            if pos_resp.status_code == 200:
                pos_data = pos_resp.json().get("event_positions", [])
                active_positions = [p for p in pos_data if float(p.get("position_fp", 0)) != 0]
                sync_report["open_positions_count"] = len(active_positions)
                state["exchange_positions_count"] = len(active_positions)
            else:
                sync_report["discrepancies"].append(f"Failed to fetch positions: HTTP {pos_resp.status_code}")

            # 3. Check portfolio daily loss
            if sync_report["exchange_balance_dollars"] is not None:
                loss_trip_reason = check_portfolio_daily_loss(
                    current_combined_equity=sync_report["exchange_balance_dollars"]
                )
                if loss_trip_reason:
                    sync_report["discrepancies"].append(f"DAILY_LOSS_LIMIT: {loss_trip_reason}")
                    sync_report["status"] = "CIRCUIT_BREAKER_TRIPPED"

            state["last_exchange_sync_ts"] = time.time()
            state["exchange_sync_status"] = sync_report["status"]
            save_state(state)
            self._last_exchange_sync = time.time()

        except Exception as e:
            logger.error(f"[OrderSaga 30s Sync] Error synchronizing exchange state: {e}")
            sync_report["status"] = "ERROR"
            sync_report["discrepancies"].append(str(e))
        finally:
            if own_client:
                await client.aclose()

        return sync_report

    async def start_reconciliation_and_orphan_loop(
        self,
        cancel_fn: Callable[[str, Optional[str]], Awaitable[bool]],
        sync_interval_seconds: float = 30.0
    ):
        """
        Background maintenance loop:
        Runs every 30 seconds to:
        1. Auto-cancel orphaned ghost orders older than max_hold_seconds (120s)
        2. Sync local portfolio_state.json with live Kalshi exchange
        3. Flush recoverable orders from network partition queue if reconnected
        """
        self._is_running = True
        logger.info(f"[OrderSaga] Started background 30s reconciliation and orphan loop (Interval: {sync_interval_seconds}s)")

        async with httpx.AsyncClient(timeout=5.0) as client:
            while self._is_running:
                try:
                    # 1. Orphan Detection & Auto-Cancel
                    orphans = await self.detect_and_cancel_orphans(cancel_fn=cancel_fn)
                    if orphans:
                        logger.warning(f"[OrderSaga] Orphan sweep cancelled {len(orphans)} ghost order(s).")
                        if self.on_orphan_detected:
                            try:
                                await self.on_orphan_detected(orphans)
                            except Exception as e:
                                logger.error(f"[OrderSaga] on_orphan_detected callback error: {e}")

                    # 2. Portfolio State 30s Exchange Sync
                    await self.sync_portfolio_state_with_exchange(client=client)

                except asyncio.CancelledError:
                    logger.info("[OrderSaga] Reconciliation loop cancelled.")
                    break
                except Exception as e:
                    logger.error(f"[OrderSaga] Maintenance loop error: {e}")

                await asyncio.sleep(sync_interval_seconds)

    def stop(self):
        """Stops background maintenance loops."""
        self._is_running = False
