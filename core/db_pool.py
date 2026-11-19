"""
core/db_pool.py — Thread-Safe SQLite Connection Pool & High-Frequency Optimization.

Features:
1. Reusable connection pool with check-out/check-in queue to eliminate per-trade connection churn.
2. Production HFT SQLite PRAGMAs:
   - PRAGMA journal_mode = WAL; (Concurrent readers + writer)
   - PRAGMA busy_timeout = 5000; (5000ms busy wait before SQLITE_BUSY error)
   - PRAGMA synchronous = NORMAL; (Fast safe commit in WAL mode)
   - PRAGMA cache_size = -64000; (64MB memory page cache)
   - PRAGMA temp_store = MEMORY; (In-memory temporary tables and sorts)
   - PRAGMA mmap_size = 268435456; (256MB memory mapped I/O)
3. PooledConnection wrapper:
   - Supports `with conn:` transaction management and automatic check-in on exit or close().
   - Transparently delegates all methods (cursor, execute, commit, rollback) to sqlite3.Connection.
4. Global pool registry for singleton access across core, coins, and tests.
"""

import logging
import os
import queue
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

logger = logging.getLogger("SQLiteConnectionPool")


class PooledConnection:
    """
    Transparent proxy wrapping sqlite3.Connection.
    Automatically returns the connection to its parent pool on context exit or close().
    """

    def __init__(self, raw_conn: sqlite3.Connection, pool: "SQLiteConnectionPool"):
        self._raw_conn = raw_conn
        self._pool = pool
        self._returned = False

    def __enter__(self):
        self._raw_conn.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            self._raw_conn.__exit__(exc_type, exc_val, exc_tb)
        finally:
            self.close()

    def close(self):
        """Return connection to the pool rather than closing the underlying file handle."""
        if not self._returned:
            self._returned = True
            if self._pool is not None:
                self._pool.return_connection(self._raw_conn)

    def cursor(self, *args, **kwargs):
        return self._raw_conn.cursor(*args, **kwargs)

    def execute(self, *args, **kwargs):
        return self._raw_conn.execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        return self._raw_conn.executemany(*args, **kwargs)

    def executescript(self, *args, **kwargs):
        return self._raw_conn.executescript(*args, **kwargs)

    def commit(self):
        return self._raw_conn.commit()

    def rollback(self):
        return self._raw_conn.rollback()

    @property
    def total_changes(self):
        return self._raw_conn.total_changes

    @property
    def in_transaction(self):
        return self._raw_conn.in_transaction

    @property
    def row_factory(self):
        return self._raw_conn.row_factory

    @row_factory.setter
    def row_factory(self, val):
        self._raw_conn.row_factory = val

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw_conn, name)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class SQLiteConnectionPool:
    """
    Thread-safe connection pool for SQLite databases tuned for HFT write throughput.
    """

    def __init__(
        self,
        db_path: str,
        max_connections: int = 10,
        timeout: float = 30.0,
        busy_timeout_ms: int = 5000,
    ):
        self.db_path = db_path
        self.max_connections = max(1, max_connections)
        self.timeout = timeout
        self.busy_timeout_ms = busy_timeout_ms

        self._pool: queue.Queue[sqlite3.Connection] = queue.Queue(maxsize=self.max_connections)
        self._all_connections: List[sqlite3.Connection] = []
        self._lock = threading.Lock()
        self._closed = False
        self._created_count = 0

        # Ensure directory exists if path contains directories
        dir_name = os.path.dirname(self.db_path)
        if dir_name and not os.path.exists(dir_name):
            os.makedirs(dir_name, exist_ok=True)

        # Pre-seed at least 1 connection to initialize DB & configure WAL immediately
        conn = self._create_connection()
        self._all_connections.append(conn)
        self._pool.put(conn)

    def _create_connection(self) -> sqlite3.Connection:
        """Create a new SQLite connection with optimized PRAGMAs."""
        conn = sqlite3.connect(
            self.db_path,
            timeout=self.timeout,
            check_same_thread=False
        )
        conn.row_factory = sqlite3.Row

        # Execute high-throughput HFT PRAGMAs for persistent disk databases
        if self.db_path != ":memory:":
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode = WAL;")
            cursor.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms};")
            cursor.execute("PRAGMA synchronous = NORMAL;")
            cursor.execute("PRAGMA cache_size = -64000;")
            cursor.execute("PRAGMA temp_store = MEMORY;")
            try:
                cursor.execute("PRAGMA mmap_size = 268435456;")
            except sqlite3.OperationalError:
                pass  # Some restricted platforms or memory limits
            cursor.close()
        self._created_count += 1
        return conn

    def get_connection(self, timeout: Optional[float] = None) -> PooledConnection:
        """
        Check out a connection from the pool, wrapped in a PooledConnection.
        """
        if self._closed:
            raise RuntimeError(f"Connection pool for {self.db_path} is closed")

        wait_time = timeout if timeout is not None else self.timeout
        raw_conn: Optional[sqlite3.Connection] = None

        try:
            # Try non-blocking get first
            raw_conn = self._pool.get_nowait()
        except queue.Empty:
            # Check if we can create a new connection up to max_connections
            with self._lock:
                if self._created_count < self.max_connections:
                    raw_conn = self._create_connection()
                    self._all_connections.append(raw_conn)

            if raw_conn is None:
                # Pool at capacity, block until one is returned
                try:
                    raw_conn = self._pool.get(block=True, timeout=wait_time)
                except queue.Empty:
                    raise TimeoutError(
                        f"Timed out after {wait_time}s waiting for a SQLite connection from pool ({self.db_path})"
                    )

        return PooledConnection(raw_conn, self)

    def return_connection(self, raw_conn: sqlite3.Connection):
        """Return a checked-out raw connection back to the pool queue."""
        if self._closed:
            try:
                raw_conn.close()
            except Exception:
                pass
            return

        try:
            if raw_conn.in_transaction:
                raw_conn.rollback()
        except Exception as e:
            logger.warning(f"Error rolling back connection on return: {e}")

        try:
            self._pool.put_nowait(raw_conn)
        except queue.Full:
            try:
                raw_conn.close()
            except Exception:
                pass

    @contextmanager
    def connection(self, timeout: Optional[float] = None):
        """
        Context manager to acquire a connection, auto-commit on success,
        auto-rollback on exception, and return it safely to the pool.
        """
        conn = self.get_connection(timeout=timeout)
        try:
            yield conn
        finally:
            conn.close()

    def checkpoint(self, mode: str = "PASSIVE") -> tuple[int, int, int]:
        """
        Manually run WAL checkpoint (PASSIVE, FULL, RESTART, or TRUNCATE).
        Returns (busy, log, checkpointed) tuple.
        """
        with self.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"PRAGMA wal_checkpoint({mode});")
            res = cursor.fetchone()
            cursor.close()
            return tuple(res) if res else (0, 0, 0)

    def close_all(self):
        """Close all connections in the pool."""
        with self._lock:
            self._closed = True
            while not self._pool.empty():
                try:
                    self._pool.get_nowait()
                except queue.Empty:
                    break

            for conn in self._all_connections:
                try:
                    conn.close()
                except Exception:
                    pass
            self._all_connections.clear()
            self._created_count = 0


# =============================================================================
# Global Connection Pool Registry (Singleton per db_path)
# =============================================================================

_POOLS: Dict[str, SQLiteConnectionPool] = {}
_REGISTRY_LOCK = threading.Lock()


def get_db_pool(
    db_path: str = "kalshi_audit.db",
    max_connections: int = 10,
    timeout: float = 30.0,
    busy_timeout_ms: int = 5000,
) -> SQLiteConnectionPool:
    """
    Get or create a singleton SQLiteConnectionPool for the given db_path.
    In-memory databases (':memory:') are not cached to allow isolated test fixtures.
    """
    if db_path == ":memory:":
        return SQLiteConnectionPool(
            db_path=db_path,
            max_connections=max_connections,
            timeout=timeout,
            busy_timeout_ms=busy_timeout_ms,
        )

    norm_path = os.path.abspath(db_path)
    with _REGISTRY_LOCK:
        if norm_path not in _POOLS or _POOLS[norm_path]._closed:
            _POOLS[norm_path] = SQLiteConnectionPool(
                db_path=db_path,
                max_connections=max_connections,
                timeout=timeout,
                busy_timeout_ms=busy_timeout_ms,
            )
        return _POOLS[norm_path]


def close_pool(db_path: str):
    """Close and remove a specific database pool from the registry."""
    norm_path = os.path.abspath(db_path) if db_path != ":memory:" else db_path
    with _REGISTRY_LOCK:
        if norm_path in _POOLS:
            _POOLS[norm_path].close_all()
            del _POOLS[norm_path]



def close_all_pools():
    """Close all open connection pools (useful for tests and shutdown)."""
    with _REGISTRY_LOCK:
        for pool in _POOLS.values():
            pool.close_all()
        _POOLS.clear()
