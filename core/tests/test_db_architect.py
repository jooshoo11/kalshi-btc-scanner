"""
core/tests/test_db_architect.py — Verification of HFT SQLite Architecture, Pooling & Partitioning.

Tests:
1. WAL Mode & Busy Timeout configuration verification.
2. Concurrent write contention test (simulating simultaneous BTC/ETH/SOL/XRP signals).
3. Index verification & Query Plan analysis (EXPLAIN QUERY PLAN).
4. Connection pool reuse, checkout/checkin, and recovery.
5. Partitioning & Cold Storage Archival: Hot (<24h) in SQLite, cold (>24h) in compressed Parquet.
"""

import concurrent.futures
import os
import shutil
import sqlite3
import tempfile
import time
import pytest
import pandas as pd

from core.db_pool import SQLiteConnectionPool, get_db_pool, close_all_pools
from core.bot_database import AuditDatabase, TradeEventType
from core.multi_coin_trader import MultiCoinAuditDatabase
from core.partition_manager import PartitionArchiveManager


@pytest.fixture
def temp_db_dir():
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    from core.db_pool import _POOLS, _REGISTRY_LOCK
    with _REGISTRY_LOCK:
        keys_to_close = [k for k in list(_POOLS.keys()) if k.startswith(temp_dir)]
        for k in keys_to_close:
            _POOLS[k].close_all()
            _POOLS.pop(k, None)
    shutil.rmtree(temp_dir, ignore_errors=True)


def test_wal_mode_and_hft_pragmas(temp_db_dir):
    """Verify that SQLite connection pool configures WAL mode, busy_timeout, and NORMAL sync."""
    db_path = os.path.join(temp_db_dir, "test_pragmas.db")
    pool = SQLiteConnectionPool(db_path=db_path, max_connections=3, busy_timeout_ms=5000)

    with pool.connection() as conn:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode;")
        journal_mode = cursor.fetchone()[0]
        assert journal_mode.lower() == "wal", f"Expected WAL mode, got {journal_mode}"

        cursor.execute("PRAGMA busy_timeout;")
        busy_timeout = cursor.fetchone()[0]
        assert busy_timeout == 5000, f"Expected 5000ms busy_timeout, got {busy_timeout}"

        cursor.execute("PRAGMA synchronous;")
        sync_mode = cursor.fetchone()[0]
        # In SQLite, NORMAL synchronous is represented by integer 1
        assert sync_mode == 1, f"Expected synchronous=NORMAL (1), got {sync_mode}"

    pool.close_all()


def test_concurrent_multicoin_writes_no_lock_error(temp_db_dir):
    """
    Simulate high-frequency concurrent writes from multiple coin threads (BTC, ETH, SOL, XRP).
    Verifies that WAL mode and pooled connections prevent 'database is locked' errors.
    """
    db_path = os.path.join(temp_db_dir, "concurrent_multicoin.db")
    audit_db = AuditDatabase(db_path=db_path, pool_size=10)

    coins = ["BTC", "ETH", "SOL", "XRP"]
    num_writes_per_coin = 30

    def worker_trade_open_and_event(coin_name: str, worker_id: int):
        for i in range(num_writes_per_coin):
            ticker = f"KX{coin_name}15M-{worker_id}-{i}"
            # 1. Append immutable event
            audit_db.append_event(
                aggregate_id=ticker,
                event_type=TradeEventType.ORDER_PLACED,
                payload={"coin": coin_name, "price": 0.50, "contracts": 5}
            )
            # 2. Write trade open projection
            trade_id = audit_db.record_trade_open(
                ticker=ticker,
                series=f"KX{coin_name}15M",
                side="YES",
                entry_price=0.52,
                contracts=5,
                cost_dollars=2.60,
                fee_dollars=0.08,
                order_group_id=f"grp_{coin_name}_{i}"
            )
            # 3. Write close projection for a subset
            if i % 2 == 0:
                audit_db.record_trade_close(
                    trade_id=trade_id,
                    exit_price=1.00,
                    exit_reason="EXPIRED_IN_MONEY",
                    pnl_dollars=2.40,
                    pnl_pct=92.3
                )

    # Launch 8 concurrent threads simultaneously across all coins
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = []
        for worker_id in range(8):
            coin = coins[worker_id % len(coins)]
            futures.append(executor.submit(worker_trade_open_and_event, coin, worker_id))
        concurrent.futures.wait(futures)

        # Check for any exceptions
        for f in futures:
            assert f.exception() is None, f"Concurrent write threw exception: {f.exception()}"

    # Verify counts
    total_trades = audit_db.get_recent_trades(limit=1000)
    expected_trades = 8 * num_writes_per_coin
    assert len(total_trades) == expected_trades, f"Expected {expected_trades} trades, found {len(total_trades)}"


def test_database_indexes_and_explain_query_plan(temp_db_dir):
    """
    Verify all required indexes exist on trades and that EXPLAIN QUERY PLAN uses index scans.
    """
    db_path = os.path.join(temp_db_dir, "test_indexes.db")
    audit_db = AuditDatabase(db_path=db_path)

    with audit_db.pool.connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='trades'")
        index_names = {row[0] for row in cursor.fetchall()}

        required_indexes = {
            "idx_trades_ticker",
            "idx_trades_series",
            "idx_trades_timestamp",
            "idx_trades_status",
            "idx_trades_order_group",
            "idx_trades_order_id",
            "idx_trades_series_ticker",
            "idx_trades_status_series",
            "idx_trades_series_ts",
        }
        for idx in required_indexes:
            assert idx in index_names, f"Missing index {idx} in trades table"

        # Verify EXPLAIN QUERY PLAN leverages the index for ticker lookups
        cursor.execute("EXPLAIN QUERY PLAN SELECT * FROM trades WHERE ticker = 'KXBTC15M-SAMPLE'")
        plan = " ".join([str(col) for row in cursor.fetchall() for col in row])
        assert "USING INDEX idx_trades_ticker" in plan, f"Query plan did not use idx_trades_ticker: {plan}"

        # Verify EXPLAIN QUERY PLAN leverages the index for order_id lookups
        cursor.execute("EXPLAIN QUERY PLAN SELECT * FROM trades WHERE order_id = 'test_order_123'")
        plan_oid = " ".join([str(col) for row in cursor.fetchall() for col in row])
        assert "USING INDEX idx_trades_order_id" in plan_oid, f"Query plan did not use idx_trades_order_id: {plan_oid}"

        # Verify EXPLAIN QUERY PLAN leverages the composite series_ticker index
        cursor.execute("EXPLAIN QUERY PLAN SELECT * FROM trades WHERE series = 'KXETH15M' AND ticker = 'KXETH15M-SAMPLE'")
        plan_st = " ".join([str(col) for row in cursor.fetchall() for col in row])
        assert ("USING INDEX idx_trades_series_ticker" in plan_st or "USING INDEX idx_trades_ticker" in plan_st), \
            f"Query plan did not use series_ticker index: {plan_st}"

        # Verify EXPLAIN QUERY PLAN leverages the index for series and timestamp lookups
        cursor.execute("EXPLAIN QUERY PLAN SELECT * FROM trades WHERE series = 'KXETH15M' AND timestamp > 1000.0")
        plan_series = " ".join([str(col) for row in cursor.fetchall() for col in row])
        assert ("USING INDEX idx_trades_series" in plan_series or "USING INDEX idx_trades_series_ts" in plan_series), \
            f"Query plan did not use series index: {plan_series}"


def test_connection_pool_lifecycle_and_reuse(temp_db_dir):
    """Verify that connection pool reuses connections and handles limits gracefully."""
    db_path = os.path.join(temp_db_dir, "test_pool.db")
    pool = SQLiteConnectionPool(db_path=db_path, max_connections=2, timeout=1.0)

    # 1. Borrow connection and return it
    with pool.connection() as conn1:
        cursor = conn1.cursor()
        cursor.execute("CREATE TABLE test (val TEXT)")
        cursor.execute("INSERT INTO test VALUES ('A')")
        conn1.commit()

    # 2. Checkout 2 connections (pool capacity)
    conn_a = pool.get_connection()
    conn_b = pool.get_connection()
    assert pool._created_count == 2

    # 3. Third checkout should timeout
    with pytest.raises(TimeoutError):
        pool.get_connection(timeout=0.1)

    # 4. Return one and check out again successfully
    conn_a.close()
    conn_c = pool.get_connection(timeout=0.5)
    assert conn_c is not None

    conn_b.close()
    conn_c.close()
    pool.close_all()


def test_partitioning_hot_sqlite_and_cold_parquet_archival(temp_db_dir):
    """
    Test partitioning:
    - Last 24h data is hot in SQLite.
    - Data older than 24h is archived to partitioned compressed Parquet files.
    - Pruning deletes cold rows from SQLite.
    - Unified query returns combined results across both hot and cold storage.
    """
    db_path = os.path.join(temp_db_dir, "partition_test.db")
    archive_dir = os.path.join(temp_db_dir, "archive")
    audit_db = AuditDatabase(db_path=db_path)

    now = time.time()
    one_day_sec = 86400.0

    # Insert 10 cold trades (3 days ago and 2 days ago)
    for i in range(10):
        past_ts = now - (2.5 * one_day_sec) + (i * 3600.0)
        audit_db.record_trade_open(
            timestamp=past_ts,
            ticker=f"KXBTC15M-COLD-{i}",
            series="KXBTC15M",
            side="YES",
            entry_price=0.45,
            contracts=10,
            cost_dollars=4.50,
            fee_dollars=0.15,
            order_group_id=f"cold_order_{i}"
        )

    # Insert 5 hot trades (within the last 2 hours)
    for i in range(5):
        hot_ts = now - (i * 600.0)
        audit_db.record_trade_open(
            timestamp=hot_ts,
            ticker=f"KXBTC15M-HOT-{i}",
            series="KXBTC15M",
            side="NO",
            entry_price=0.55,
            contracts=4,
            cost_dollars=2.20,
            fee_dollars=0.07,
            order_group_id=f"hot_order_{i}"
        )

    # 1. Archive cold trades (> 24 hours old) with pruning enabled
    partition_mgr = PartitionArchiveManager(archive_base_dir=archive_dir)
    res = partition_mgr.archive_cold_trades(db_path=db_path, cutoff_hours=24, prune_hot=True)

    assert res["rows_archived"] == 10
    assert res["pruned"] is True
    assert len(res["files_created"]) > 0

    # Verify Parquet files were generated on disk
    assert len(res["files_created"]) > 0
    total_archived_rows = sum(len(pd.read_parquet(p)) for p in res["files_created"])
    assert total_archived_rows == 10
    has_cold_ticker = any("KXBTC15M-COLD-0" in pd.read_parquet(p)["ticker"].values for p in res["files_created"])
    assert has_cold_ticker is True

    # 2. Verify hot trades in SQLite: only 5 hot trades remain
    hot_trades = partition_mgr.query_hot_trades(db_path=db_path, cutoff_hours=24)
    assert len(hot_trades) == 5

    # 3. Test unified query: merges hot (5) and cold (10) to return all 15 trades
    unified = partition_mgr.query_unified_trades(db_path=db_path, archive_dir=archive_dir, limit=100)
    assert len(unified) == 15
    assert unified[0]["ticker"].startswith("KXBTC15M-HOT")  # Most recent first

    # 4. Test AuditDatabase helper methods
    audit_hot = audit_db.get_hot_trades(cutoff_hours=24)
    assert len(audit_hot) == 5
    audit_unified = audit_db.get_unified_trades(archive_dir=archive_dir, limit=100)
    assert len(audit_unified) == 15


def test_multicoin_database_indexes_and_explain_plan(temp_db_dir):
    """Verify that MultiCoinAuditDatabase has all required indexes on trades table."""
    db_path = os.path.join(temp_db_dir, "test_mc_indexes.db")
    mc_db = MultiCoinAuditDatabase(db_path=db_path)

    with mc_db.pool.connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='trades'")
        index_names = {row[0] for row in cursor.fetchall()}

        required_indexes = {
            "idx_mc_trades_ticker",
            "idx_mc_trades_series",
            "idx_mc_trades_coin",
            "idx_mc_trades_timestamp",
            "idx_mc_trades_status",
            "idx_mc_trades_order_group",
            "idx_mc_trades_order_id",
            "idx_mc_trades_series_ticker",
            "idx_mc_trades_coin_status",
            "idx_mc_trades_status_series_ts",
        }
        for idx in required_indexes:
            assert idx in index_names, f"Missing index {idx} in MultiCoin trades table"

        # Verify EXPLAIN QUERY PLAN leverages the index for order_id
        cursor.execute("EXPLAIN QUERY PLAN SELECT * FROM trades WHERE order_id = 'mc_order_123'")
        plan_oid = " ".join([str(col) for row in cursor.fetchall() for col in row])
        assert "USING INDEX idx_mc_trades_order_id" in plan_oid, f"Query plan did not use idx_mc_trades_order_id: {plan_oid}"

        # Verify EXPLAIN QUERY PLAN leverages the series_ticker index
        cursor.execute("EXPLAIN QUERY PLAN SELECT * FROM trades WHERE series = 'KXSOL15M' AND ticker = 'KXSOL15M-SAMPLE'")
        plan_st = " ".join([str(col) for row in cursor.fetchall() for col in row])
        assert ("USING INDEX idx_mc_trades_series_ticker" in plan_st or "USING INDEX idx_mc_trades_ticker" in plan_st), \
            f"Query plan did not use series_ticker index: {plan_st}"

    mc_db.close()


def test_hot_memory_partition_and_multidb_archive(temp_db_dir):
    """
    Test in-memory hot partition:
    - Load trades into HotMemoryPartition
    - Query and lookup by order_id in RAM (<10 microseconds)
    - Prune cold records from memory
    - Test archive_all_databases across kalshi_audit.db, multi_coin_paper.db, kalshi_live_demo.db
    """
    from core.partition_manager import HotMemoryPartition, PartitionArchiveManager

    hot_mem = HotMemoryPartition(cutoff_hours=24)
    now = time.time()

    # 1. Add in-memory hot trades
    sample_trade = {
        "id": 101,
        "order_id": "ord_fast_mem_1",
        "ticker": "KXBTC15M-MEM-1",
        "series": "KXBTC15M",
        "timestamp": now - 300,
        "side": "YES",
        "entry_price": 0.50,
        "contracts": 10
    }
    hot_mem.put_trade(sample_trade)

    # 2. Instant lookup by order_id
    rec = hot_mem.get_by_order_id("ord_fast_mem_1")
    assert rec is not None
    assert rec["ticker"] == "KXBTC15M-MEM-1"

    # 3. In-memory query
    q_res = hot_mem.query(series="KXBTC15M")
    assert len(q_res) == 1

    # 4. Multi-database archive
    db1 = os.path.join(temp_db_dir, "kalshi_audit.db")
    db2 = os.path.join(temp_db_dir, "multi_coin_paper.db")
    db3 = os.path.join(temp_db_dir, "kalshi_live_demo.db")

    a1 = AuditDatabase(db_path=db1)
    a2 = MultiCoinAuditDatabase(db_path=db2)
    a3 = AuditDatabase(db_path=db3)

    # Insert a cold trade in each
    past_ts = now - (48 * 3600.0)
    a1.record_trade_open(ticker="KXBTC15M-P1", series="KXBTC15M", side="YES", entry_price=0.5, contracts=1, cost_dollars=0.5, fee_dollars=0.01, timestamp=past_ts, order_id="ord_a1")
    a2.record_trade_open(coin="ETH", ticker="KXETH15M-P2", series="KXETH15M", side="YES", entry_price=0.5, contracts=1, cost_dollars=0.5, fee_dollars=0.01, timestamp=past_ts, order_id="ord_a2")
    a3.record_trade_open(ticker="KXSOL15M-P3", series="KXSOL15M", side="YES", entry_price=0.5, contracts=1, cost_dollars=0.5, fee_dollars=0.01, timestamp=past_ts, order_id="ord_a3")

    archive_dir = os.path.join(temp_db_dir, "multi_archive")
    mgr = PartitionArchiveManager(archive_base_dir=archive_dir)
    res = mgr.archive_all_databases(db_paths=[db1, db2, db3], cutoff_hours=24, prune_hot=True)

    assert db1 in res
    assert db2 in res
    assert db3 in res
    assert res[db1]["trades"]["rows_archived"] == 1
    assert res[db2]["trades"]["rows_archived"] == 1
    assert res[db3]["trades"]["rows_archived"] == 1

    a1.close()
    a2.close()
    a3.close()
