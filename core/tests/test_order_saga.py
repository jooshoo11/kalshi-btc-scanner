"""
core/tests/test_order_saga.py — Unit Tests for Distributed Order Lifecycle Saga Orchestrator.

Verifies:
1. Saga state transitions: PENDING → SUBMITTED → PARTIAL_FILL → FILLED → CANCELLED → SETTLED
2. Compensating transactions: missed WebSocket fills reconciled via REST GET /portfolio/events/orders/{order_id}
3. Orphan detection: orders older than max_hold_seconds (120s) auto-cancelled via DELETE /portfolio/events/orders/{order_id}
4. 30-second portfolio state synchronization with exchange balance & positions
5. Network partition handling: local queueing and safe recovery flushing
"""

import asyncio
import time
from unittest.mock import MagicMock, AsyncMock, patch
import pytest

from core.order_saga import (
    OrderSagaOrchestrator,
    OrderSagaState,
    OrderSaga,
    NetworkPartitionQueue
)
from core.live_executor import KalshiOrderManagementSystem, KalshiAuth


@pytest.fixture
def saga_orchestrator():
    auth_mock = MagicMock(spec=KalshiAuth)
    auth_mock.is_configured = True
    auth_mock.key_id = "test-key-id"
    auth_mock.sign_request.return_value = {"KALSHI-ACCESS-KEY": "test"}

    orchestrator = OrderSagaOrchestrator(
        auth=auth_mock,
        rest_url="https://demo-api.kalshi.co",
        max_hold_seconds=120.0
    )
    return orchestrator


def test_saga_state_transitions(saga_orchestrator):
    """Verify standard state progression: PENDING -> SUBMITTED -> PARTIAL_FILL -> FILLED -> SETTLED."""
    saga = saga_orchestrator.create_saga(
        ticker="KXBTC15M-TEST",
        side="YES",
        action="buy",
        target_contracts=10,
        price_cents=50
    )

    assert saga.state == OrderSagaState.PENDING
    assert saga.remaining_contracts == 10
    assert saga.filled_contracts == 0

    # Advance to SUBMITTED
    saga_orchestrator.advance_state(saga, OrderSagaState.SUBMITTED, "Placed on exchange")
    assert saga.state == OrderSagaState.SUBMITTED
    assert saga.submitted_at is not None

    # Partial fill via WebSocket
    saga_orchestrator.handle_ws_fill({
        "order_id": saga.order_id or "ord_123",
        "client_order_id": saga.client_order_id,
        "count_fp": 4,
        "yes_price_dollars": 0.50
    })
    assert saga.state == OrderSagaState.PARTIAL_FILL
    assert saga.filled_contracts == 4
    assert saga.remaining_contracts == 6

    # Complete fill via WebSocket
    saga_orchestrator.handle_ws_fill({
        "order_id": saga.order_id or "ord_123",
        "client_order_id": saga.client_order_id,
        "count_fp": 6,
        "yes_price_dollars": 0.50
    })
    assert saga.state == OrderSagaState.FILLED
    assert saga.filled_contracts == 10
    assert saga.remaining_contracts == 0
    assert saga.completed_at is not None

    # Settle
    saga_orchestrator.advance_state(saga, OrderSagaState.SETTLED, "Contract expired and settled")
    assert saga.state == OrderSagaState.SETTLED
    assert saga.is_terminal()


@pytest.mark.asyncio
async def test_compensating_transaction_on_missed_ws(saga_orchestrator):
    """Verify REST status reconciliation compensates when WebSocket fill message is dropped."""
    saga = saga_orchestrator.create_saga(
        ticker="KXETH15M-TEST",
        side="YES",
        action="buy",
        target_contracts=5,
        price_cents=45
    )
    saga.order_id = "ord_test_999"
    saga_orchestrator.order_id_to_saga["ord_test_999"] = saga.saga_id
    saga_orchestrator.advance_state(saga, OrderSagaState.SUBMITTED)

    # Simulate dropped WebSocket fill: local state still has 0 fills
    assert saga.filled_contracts == 0

    # REST status reports exchange already executed 5 contracts
    mock_get_status = AsyncMock(return_value={
        "status": "executed",
        "fill_count": 5,
        "remaining_count": 0
    })

    await saga_orchestrator.compensate_if_missed_ws(saga.saga_id, mock_get_status)

    # Verify compensating transaction reconciled the fill
    assert saga.state == OrderSagaState.FILLED
    assert saga.filled_contracts == 5
    assert saga.remaining_contracts == 0
    assert len(saga.compensation_log) >= 1
    assert saga.compensation_log[0]["action"] == "REST_GET_STATUS_RECONCILIATION"


@pytest.mark.asyncio
async def test_orphan_detection_and_auto_cancellation(saga_orchestrator):
    """Verify orders resting older than max_hold_seconds (120s) are auto-cancelled."""
    saga = saga_orchestrator.create_saga(
        ticker="KXSOL15M-TEST",
        side="NO",
        action="buy",
        target_contracts=20,
        price_cents=60
    )
    saga.order_id = "ord_orphan_1"
    saga_orchestrator.order_id_to_saga["ord_orphan_1"] = saga.saga_id
    saga_orchestrator.advance_state(saga, OrderSagaState.SUBMITTED)

    # Artificially age the order past 120s
    saga.created_at = time.time() - 130.0

    mock_cancel = AsyncMock(return_value=True)

    cancelled_orphans = await saga_orchestrator.detect_and_cancel_orphans(
        cancel_fn=mock_cancel,
        max_hold_seconds=120.0
    )

    assert len(cancelled_orphans) == 1
    assert cancelled_orphans[0]["order_id"] == "ord_orphan_1"
    assert mock_cancel.called
    assert saga.state == OrderSagaState.CANCELLED
    assert saga.is_terminal()


@pytest.mark.asyncio
async def test_30s_portfolio_state_exchange_sync(saga_orchestrator, tmp_path):
    """Verify reconciliation updates portfolio_state.json with live exchange data."""
    test_state_file = tmp_path / "portfolio_state.json"
    with patch("core.order_saga.load_state", return_value={"day_start_equity": 100.0}), \
         patch("core.order_saga.save_state") as mock_save:

        report = await saga_orchestrator.sync_portfolio_state_with_exchange()

        assert report["status"] in ("HEALTHY", "SIMULATED_SYNC")
        assert mock_save.called
        saved_state = mock_save.call_args[0][0]
        assert "last_exchange_sync_ts" in saved_state
        assert "exchange_sync_status" in saved_state


def test_network_partition_queue(tmp_path):
    """Verify local queuing during network partitions and staleness filtering on flush."""
    q_file = str(tmp_path / "partition_q.json")
    queue = NetworkPartitionQueue(maxsize=10, state_file=q_file)

    # Record consecutive network failures to trip circuit breaker
    queue.record_failure("ConnectTimeout")
    queue.record_failure("ConnectionRefused")
    queue.record_failure("HostUnreachable")
    assert queue.is_partitioned is True

    # Enqueue a fresh order
    saga1 = OrderSaga(
        saga_id="s1",
        client_order_id="c1",
        ticker="KXBTC15M-T1",
        side="YES",
        action="buy",
        target_contracts=5,
        price_cents=50,
        created_at=time.time()
    )
    # Enqueue a stale order (> 60s old)
    saga2 = OrderSaga(
        saga_id="s2",
        client_order_id="c2",
        ticker="KXBTC15M-T2",
        side="NO",
        action="buy",
        target_contracts=10,
        price_cents=45,
        created_at=time.time() - 90.0
    )

    queue.enqueue(saga1)
    queue.enqueue(saga2)
    assert queue.qsize == 2

    # Network recovers
    queue.record_success()
    assert queue.is_partitioned is False

    # Recoverable orders (max_age=60s) should return saga1 and discard stale saga2
    recoverable = queue.get_recoverable_sagas(max_age_seconds=60.0)
    assert len(recoverable) == 1
    assert recoverable[0]["saga_id"] == "s1"
    assert queue.qsize == 0


@pytest.mark.asyncio
async def test_oms_execute_order_with_saga():
    """Verify integration of execute_order_with_saga in KalshiOrderManagementSystem."""
    auth_mock = MagicMock(spec=KalshiAuth)
    auth_mock.is_configured = False  # Simulation mode

    oms = KalshiOrderManagementSystem(auth=auth_mock, dry_run=True)
    saga = await oms.execute_order_with_saga(
        ticker="KXBTC15M-TEST",
        direction="BUY_YES",
        target_contracts=5,
        price_cents=55
    )

    assert saga is not None
    assert saga.state == OrderSagaState.FILLED
    assert saga.filled_contracts == 5
    assert saga.ticker == "KXBTC15M-TEST"


def test_mark_saga_settled(saga_orchestrator):
    """Verify mark_saga_settled transitions FILLED sagas to SETTLED."""
    saga = saga_orchestrator.create_saga(
        ticker="KXSOL15M-EXP",
        side="YES",
        action="buy",
        target_contracts=10,
        price_cents=50
    )
    saga.order_id = "ord_settle_1"
    saga_orchestrator.order_id_to_saga["ord_settle_1"] = saga.saga_id
    saga_orchestrator.advance_state(saga, OrderSagaState.SUBMITTED)
    saga_orchestrator.advance_state(saga, OrderSagaState.FILLED)

    settled_count = saga_orchestrator.mark_saga_settled("KXSOL15M-EXP", settlement_price=1.0)
    assert settled_count == 1
    assert saga.state == OrderSagaState.SETTLED
    assert saga.metadata["settlement_price"] == 1.0
    assert saga.is_terminal()


@pytest.mark.asyncio
async def test_oms_execute_trade_registers_saga():
    """Verify execute_trade tracks orders in saga_orchestrator."""
    auth_mock = MagicMock(spec=KalshiAuth)
    auth_mock.is_configured = False  # Simulation mode

    oms = KalshiOrderManagementSystem(auth=auth_mock, dry_run=True)
    res = await oms.execute_trade(
        ticker="KXBTC15M-TEST",
        direction="BUY_YES",
        target_contracts=3,
        edge_threshold=0.04,
        model_prob=0.65
    )

    assert res["status"] == "FILLED"
    assert len(oms.saga_orchestrator.sagas) == 1
    saga = list(oms.saga_orchestrator.sagas.values())[0]
    assert saga.state == OrderSagaState.FILLED
    assert saga.target_contracts == 3
