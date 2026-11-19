"""
core/feed_queue.py — Bounded Ingestion Queues with Backpressure for Multi-Exchange Feeds.

Provides low-latency, bounded queueing with drop-oldest (coalescing) backpressure semantics
for WebSocket tick ingestion from Hyperliquid, Kraken, and Coinbase.
Prevents unbounded memory growth and ensures high-frequency option evaluation loops always
read the freshest market state without tripping stale-tick watchdogs.
"""

import time
import asyncio
import logging
from typing import Generic, TypeVar, Optional, Dict, Any, Callable, Coroutine

logger = logging.getLogger("FeedQueue")

T = TypeVar("T")


class LatestTickQueue(Generic[T]):
    """
    Bounded queue with Drop-Oldest (LIFO eviction) backpressure support.
    
    When high-frequency WebSocket bursts exceed the consumer's throughput,
    the oldest ticks in the buffer are evicted immediately. This guarantees that:
    1. Memory usage remains strictly bounded.
    2. Consumers never process stale quotes (critical for sub-second binary option pricing).
    """

    def __init__(self, maxsize: int = 50, name: str = "feed"):
        self.maxsize = maxsize
        self.name = name
        self._queue: asyncio.Queue[T] = asyncio.Queue(maxsize=maxsize)
        self.total_enqueued: int = 0
        self.total_dropped: int = 0
        self.total_dequeued: int = 0

    def put_nowait_drop_oldest(self, item: T) -> bool:
        """
        Enqueues an item non-blocking. If queue is full, evicts the oldest item
        before enqueuing the newest item.
        """
        self.total_enqueued += 1
        if self._queue.full():
            try:
                # Evict oldest tick
                self._queue.get_nowait()
                self._queue.task_done()
                self.total_dropped += 1
            except (asyncio.QueueEmpty, ValueError):
                pass

        try:
            self._queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            # Fallback if queue full under contention
            self.total_dropped += 1
            return False

    async def put(self, item: T) -> None:
        """
        Standard async put that suspends the caller when full (classic TCP-style backpressure).
        """
        self.total_enqueued += 1
        await self._queue.put(item)

    async def get(self) -> T:
        """Await and retrieve the next item."""
        item = await self._queue.get()
        self.total_dequeued += 1
        return item

    def task_done(self) -> None:
        """Acknowledge completion of item processing."""
        self._queue.task_done()

    async def join(self) -> None:
        """Wait until all items in the queue have been processed."""
        await self._queue.join()

    @property
    def qsize(self) -> int:
        return self._queue.qsize()

    @property
    def empty(self) -> bool:
        return self._queue.empty()

    @property
    def full(self) -> bool:
        return self._queue.full()

    def get_stats(self) -> Dict[str, Any]:
        """Return operational telemetry."""
        return {
            "name": self.name,
            "qsize": self.qsize,
            "maxsize": self.maxsize,
            "total_enqueued": self.total_enqueued,
            "total_dropped": self.total_dropped,
            "total_dequeued": self.total_dequeued,
            "drop_rate_pct": round(
                (self.total_dropped / max(1, self.total_enqueued)) * 100.0, 2
            ),
        }


class FeedIngestionManager:
    """
    Manages bounded ingestion queues and consumers across multiple exchange feeds:
    Hyperliquid, Kraken, and Coinbase.
    """

    SUPPORTED_EXCHANGES = ("hyperliquid", "kraken", "coinbase")

    def __init__(self, maxsize_per_exchange: int = 50):
        self.maxsize = maxsize_per_exchange
        self.queues: Dict[str, LatestTickQueue[Dict[str, Any]]] = {
            exchange: LatestTickQueue(maxsize=maxsize_per_exchange, name=exchange)
            for exchange in self.SUPPORTED_EXCHANGES
        }
        self._consumers: Dict[str, asyncio.Task] = {}
        self._is_running: bool = False

    def ingest_tick(self, exchange: str, tick: Dict[str, Any]) -> bool:
        """
        Ingest a tick from an exchange WebSocket feed using drop-oldest backpressure.
        """
        ex = exchange.lower()
        if ex not in self.queues:
            self.queues[ex] = LatestTickQueue(maxsize=self.maxsize, name=ex)

        if "ingest_ts" not in tick:
            tick["ingest_ts"] = time.time()

        return self.queues[ex].put_nowait_drop_oldest(tick)

    def start_consumer(
        self,
        exchange: str,
        handler: Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]
    ) -> asyncio.Task:
        """
        Spawns a dedicated consumer task for an exchange queue.
        """
        ex = exchange.lower()
        queue = self.queues.get(ex)
        if not queue:
            queue = LatestTickQueue(maxsize=self.maxsize, name=ex)
            self.queues[ex] = queue

        async def _worker():
            while True:
                tick = await queue.get()
                try:
                    await handler(tick)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"[{ex}] Error in tick handler: {e}")
                finally:
                    queue.task_done()

        task = asyncio.create_task(_worker(), name=f"feed_consumer_{ex}")
        self._consumers[ex] = task
        return task

    def stop_consumers(self) -> None:
        """Cancels all active feed consumer tasks."""
        for name, task in self._consumers.items():
            if not task.done():
                task.cancel()
        self._consumers.clear()

    def get_all_stats(self) -> Dict[str, Any]:
        """Telemetry snapshot across all queues."""
        return {ex: q.get_stats() for ex, q in self.queues.items()}
