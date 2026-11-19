"""
core/tests/test_feed_queue.py — Unit tests for Bounded Ingestion Queues & Backpressure.
"""

import asyncio
import pytest
from core.feed_queue import LatestTickQueue, FeedIngestionManager

@pytest.mark.asyncio
async def test_latest_tick_queue_drop_oldest():
    """Verify that when the queue is full, oldest ticks are evicted to keep data fresh."""
    queue = LatestTickQueue[int](maxsize=3, name="test_queue")

    # Push 3 items: [1, 2, 3]
    for i in range(1, 4):
        assert queue.put_nowait_drop_oldest(i)

    assert queue.qsize == 3
    assert queue.total_dropped == 0

    # Push 4th item: should drop 1, queue contains [2, 3, 4]
    assert queue.put_nowait_drop_oldest(4)
    assert queue.qsize == 3
    assert queue.total_dropped == 1

    # Push 5th item: should drop 2, queue contains [3, 4, 5]
    assert queue.put_nowait_drop_oldest(5)
    assert queue.qsize == 3
    assert queue.total_dropped == 2

    # Verify order of items dequeued
    item1 = await queue.get()
    queue.task_done()
    item2 = await queue.get()
    queue.task_done()
    item3 = await queue.get()
    queue.task_done()

    assert item1 == 3
    assert item2 == 4
    assert item3 == 5
    assert queue.empty


@pytest.mark.asyncio
async def test_feed_ingestion_manager_routing_and_consumption():
    """Verify routing and consumer execution for Hyperliquid, Kraken, Coinbase."""
    manager = FeedIngestionManager(maxsize_per_exchange=10)
    received = {"hyperliquid": [], "kraken": [], "coinbase": []}

    async def hl_handler(tick):
        received["hyperliquid"].append(tick)

    async def kraken_handler(tick):
        received["kraken"].append(tick)

    async def cb_handler(tick):
        received["coinbase"].append(tick)

    manager.start_consumer("hyperliquid", hl_handler)
    manager.start_consumer("kraken", kraken_handler)
    manager.start_consumer("coinbase", cb_handler)

    # Ingest ticks
    manager.ingest_tick("hyperliquid", {"price": 95000.0, "source": "hl"})
    manager.ingest_tick("kraken", {"price": 95005.0, "source": "kr"})
    manager.ingest_tick("coinbase", {"price": 95010.0, "source": "cb"})

    # Let event loop dispatch
    await asyncio.sleep(0.05)

    assert len(received["hyperliquid"]) == 1
    assert received["hyperliquid"][0]["price"] == 95000.0
    assert len(received["kraken"]) == 1
    assert len(received["coinbase"]) == 1

    manager.stop_consumers()
