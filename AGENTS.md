# AGENT.md — Multi-Coin Quantitative Engine Architecture & Operational Guidelines

## 1. Project Mission & Primary Directive
The objective of this repository is to operate a high-frequency, quantitative 15-minute binary options trading system on Kalshi (`demo-api.kalshi.co` / production) across multiple major crypto assets:
- **Bitcoin (BTC)**: Series `KXBTC15M` | Settlement Index: CF Benchmarks `BRTI`
- **Ethereum (ETH)**: Series `KXETH15M` | Settlement Index: CF Benchmarks `ETHUSD_RTI`
- **Solana (SOL)**: Series `KXSOL15M` | Settlement Index: CF Benchmarks `SOLUSD_RTI`
- **Ripple (XRP)**: Series `KXXRP15M` | Settlement Index: CF Benchmarks `XRPUSD_RTI`

The system utilizes Drift-Diffusion Jump process pricing, sub-second spot feeds (Coinbase, Kraken, Hyperliquid), dynamic order book liquidity sweeps, non-linear Kalshi taker fee accounting, and fractional Kelly compounding with strict risk controls.

---

## 2. Agent Roles, Operational Boundaries & Architectural Rules

### A. Architect Agent
* **Domain**: Repository structure, component modularity, interface specifications, configuration schemas.
* **Responsibilities**:
  - Enforce clean decoupling between shared infrastructure (`core/`) and asset-specific models (`coins/<asset>/`).
  - Prevent cross-coin circular dependencies.
  - Define unified configuration contracts (`global_risk.json`, `config.json`).
* **Operational Boundaries**:
  - Does NOT alter execution pricing math or live API credentials without consensus.
  - Ensures all directory scaffolding follows isolated lifecycle conventions (`predictors/`, `backtests/`, `tests/`, `logs/`, `updates/`).

### B. Refactoring Agent
* **Domain**: Code genericization, abstraction layers, file migration, shared utilities.
* **Responsibilities**:
  - Decouple all single-asset assumptions (e.g., removing hardcoded `"BTC"`, `"BRTI"`).
  - Generalize the BRTI accumulator into an arbitrary `RtiAccumulator` parameterizable by index symbol and smoothing window.
  - Implement the thread-safe, concurrency-guarded `PortfolioManager` for capital allocation.
* **Operational Boundaries**:
  - Never break existing trade selection filters (+4.0% min edge, 60% min probability, 20¢–78¢ price bounds).
  - Retain the exact non-linear Kalshi taker fee formula: `0.07 * contracts * price * (1 - price)`.

### C. Execution & Risk Agent
* **Domain**: Live Order Management System (OMS), WebSocket streaming, exchange settlement reconciliation.
* **Responsibilities**:
  - Guarantee zero orphaned or "ghost" orders on the exchange.
  - Enforce official Kalshi V2 endpoints (`DELETE /portfolio/events/orders/{order_id}`).
  - Maintain dynamic orderbook depth scaling and bounded taker spread crossing (+1¢ to +2¢ with strictly positive edge validation).
* **Operational Boundaries**:
  - Max allocation capped per trade via `PortfolioManager` to prevent over-collateralization.
  - Always verify execution fills on exchange before committing position state.

### D. QA / Reviewer Agent
* **Domain**: Test suites, import integrity, concurrency validation, settlement precision.
* **Responsibilities**:
  - Verify zero broken relative or absolute imports across `core.*` and `coins.<asset>.*`.
  - Validate asynchronous portfolio balance locks under simultaneous multi-coin signals.
  - Run regression tests against historical CF Benchmarks 60-second arithmetic mean settlements.

---

## 3. Multi-Asset Architectural Specification

```
kalshi_scanner/
├── core/                                # Shared Quantitative Infrastructure
│   ├── kalshi_client.py                 # Multi-series Kalshi REST & WebSocket V2 client
│   ├── live_executor.py                 # OMS, L2 VWAP sweeps, execution, V2 cancel routing
│   ├── scanner_core.py                  # Multi-market discovery & tick pipeline
│   ├── rti_accumulator.py              # Generic CF Benchmarks 60s accumulator
│   ├── prediction_engine.py             # Quant drift-diffusion pricing & Kelly sizing
│   ├── portfolio_manager.py             # Global cash & multi-asset capital allocator (async lock)
│   ├── bot_database.py                  # SQLite trade & portfolio audit persistence
│   └── tests/                           # Core integration & concurrency tests
│       ├── test_live_oms.py
│       └── test_portfolio_concurrency.py
├── coins/                               # Asset-Specific Workspaces
│   ├── btc/
│   │   ├── config.json                  # KXBTC15M / BRTI config & strikes
│   │   ├── predictors/                  # BTC exchange-specific pricing models
│   │   ├── backtests/                   # Historical simulation datasets
│   │   ├── tests/                       # Asset-specific unit tests
│   │   ├── logs/                        # Isolated execution logs
│   │   └── updates/                     # Feature updates & changelogs
│   ├── eth/
│   │   ├── config.json                  # KXETH15M / ETHUSD_RTI config
│   │   ├── predictors/
│   │   ├── backtests/
│   │   ├── tests/
│   │   ├── logs/
│   │   └── updates/
│   ├── sol/
│   │   ├── config.json                  # KXSOL15M / SOLUSD_RTI config
│   │   ├── predictors/
│   │   ├── backtests/
│   │   ├── tests/
│   │   ├── logs/
│   │   └── updates/
│   └── xrp/
│       ├── config.json                  # KXXRP15M / XRPUSD_RTI config
│       ├── predictors/
│       ├── backtests/
│       ├── tests/
│       ├── logs/
│       └── updates/
├── global_risk.json                     # System-wide capital limits, Kelly fractions, max drawdown
├── run_live_bot.py                      # Multi-asset live execution orchestrator
├── paper_trader.py                      # Multi-asset paper trading engine
└── app.py                               # FastAPI real-time multi-coin dashboard
```

---

## 4. Phased Execution Roadmap

### Phase 1: Directory Layout & Configuration (Architect Agent) — [DONE]
1. **Target Directory Structure**:
   - `core/` created for shared infrastructure.
   - Isolated coin workspaces created: `coins/btc/`, `coins/eth/`, `coins/sol/`, `coins/xrp/`.
2. **Scaffold Coin Subdirectories** — [DONE]:
   - Each asset directory contains: `predictors/`, `backtests/`, `tests/`, `logs/`, `updates/`.
3. **Generate Asset `config.json` Files** — [DONE]:
   - `coins/btc/config.json`: Series `KXBTC15M`, Index `BRTI`, spot feeds `["coinbase", "kraken", "hyperliquid"]`.
   - `coins/eth/config.json`: Series `KXETH15M`, Index `ETHUSD_RTI`, spot feeds `["coinbase", "kraken", "hyperliquid"]`.
   - `coins/sol/config.json`: Series `KXSOL15M`, Index `SOLUSD_RTI`, spot feeds `["coinbase", "kraken", "hyperliquid"]`.
   - `coins/xrp/config.json`: Series `KXXRP15M`, Index `XRPUSD_RTI`, spot feeds `["coinbase", "kraken"]`.
4. **Update `.gitignore`** — [DONE]:
   - Ignores `coins/*/logs/`, `coins/*/updates/`, `coins/*/backtests/*.csv`, `coins/*/backtests/*.parquet`.

---

### Phase 2: Decouple & Genericize Core (Refactoring Agent) — [DONE]
1. **Move Universal Modules to `core/`** — [DONE]:
   - `live_executor.py`, `kalshi_client.py`, `bot_database.py`, `prediction_engine.py` all reside in `core/`.
2. **Genericize Classes** — [DONE]:
   - `RtiAccumulator(index_name: str, window_seconds: int = 60)` implemented in `core/rti_accumulator.py`.
   - `KalshiClient` manages concurrent WebSocket subscriptions across `["KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M"]`.
   - `KalshiOrderManagementSystem` and `ScannerCore` accept arbitrary series tickers, symbols, and parameters dynamically.
3. **Build `core/portfolio_manager.py`** — [DONE]:
   - Centralizes live balance synchronization against `/trade-api/v2/portfolio/balance`.
   - Supports risk budgeting (20% equity per trade cap per `coins/*/config.json` `max_allocation_pct`).
   - Uses `asyncio.Lock()` around margin calculation and reservation.

---

### Phase 3: Coin Isolation & Model Placement (Refactoring Agent) — [DONE]
1. **Migrate Prediction Models** — [DONE]:
   - `core/base_predictor.py` implements `BaseAssetPredictor` interface.
   - `coins/btc/predictors/btc_predictor.py` inherits `BaseAssetPredictor`.
   - `coins/eth/predictors/eth_predictor.py` inherits `BaseAssetPredictor` — staged, `enabled: false` not turned on yet.
   - `coins/sol/predictors/sol_predictor.py` inherits `BaseAssetPredictor` — staged, `enabled: false` not turned on yet.
   - `coins/xrp/predictors/xrp_predictor.py` inherits `BaseAssetPredictor` — staged, `enabled: false` not turned on yet.
2. **Unified Runner (`run_live_bot.py`)** — [DONE]:
   - `run_live_bot.py` exists at repo root.
   - Loads `global_risk.json` and coin configs `coins/*/config.json`.
   - Initializes `KalshiClient` and `PortfolioManager`.
   - 1-second evaluation loops implemented for active assets (BTC, ETH); SOL and XRP staged but `enabled: false` per user instruction.

---

### Phase 4: Verification & Integration (QA / Reviewer Agent) — [COMPLETE]
1. **Import Integrity Audit** — [COMPLETE]:
   - Verified zero broken imports across `core.*` and `coins.<asset>.*`.
   - All 12 core module imports resolve; all 4 coin predictor imports resolve.
2. **Concurrency & Capital Test** — [COMPLETE]:
   - `core/tests/test_portfolio_concurrency.py` — 3 tests pass:
     - `test_simultaneous_trade_reservation_lock`
     - `test_simultaneous_btc_and_sol_margin_exhaustion`
     - `test_release_and_reallocation`
   - Portfolio lock serializes order sizing and rejects trades if margin is exhausted.
3. **Baseline Test Execution** — [COMPLETE]:
   - `pytest core/tests/` — **86 passed, 0 failed**.
   - `RtiAccumulator` matches CF Benchmarks 60-second arithmetic mean settlement for BRTI, ETHUSD_RTI, SOLUSD_RTI, XRPUSD_RTI.
   - Full test matrix: order saga (9), risk enforcement (4), kalshi client (7), kalshi WS mock (2), feed queue (2), HFT performance (3), fastapi production (1), multicoin risk gate (3), portfolio concurrency (3), backtest suite, bot database tests, RTI regression (6).

---

## 5. Configuration Reference

### 5.1 `global_risk.json` — System-Wide Risk Limits
Located at repo root. Current values:

| Field | Type | Value | Range | Purpose |
|-------|------|-------|-------|---------|
| `max_portfolio_allocation_pct` | float | 40.0 | 0–100 | Hard cap on total equity allocated across all active trades |
| `max_single_asset_allocation_pct` | float | 20.0 | 0–100 | Per-asset cap to prevent over-concentration |
| `max_concurrent_positions` | int | 2 | 1–10 | Max simultaneous open positions across all assets |
| `global_stop_loss_daily_pct` | float | 10.0 | 0–50 | Daily P&L stop-loss; triggers kill_switch when breached |
| `default_take_profit_roi_pct` | float | 25.0 | 0–500 | Default take-profit target ROI |
| `default_stop_loss_roi_pct` | float | 25.0 | 0–500 | Default stop-loss target ROI |
| `min_edge_pct` | float | 4.0 | 0–100 | Minimum edge required for any trade entry (trade selection filter) |
| `min_model_prob` | float | 60.0 | 0–100 | Minimum model probability for trade entry |
| `min_seconds_left_for_entry` | int | 90 | 0–900 | Global minimum seconds remaining before 15M candle close |
| `max_seconds_left_for_entry` | int | 660 | 0–900 | Global maximum seconds remaining before entry |
| `max_hold_seconds` | int | 600 | 10–900 | Maximum hold time before forced market exit (updated from 120s to allow 15m expiration theta convergence) |
| `active_assets` | list[str] | `["BTC", "ETH"]` | subset of [BTC, ETH, SOL, XRP] | Master enable/disable switch for tradeable assets |

**Precedence rules:**
- `global_risk.json` governs system-wide caps (portfolio, concurrent positions, daily stop-loss)
- Per-coin `config.json` overrides `global_risk.json` for asset-specific parameters
  - Example: `min_seconds_left_for_entry` is 60 in each coin config vs 90 globally
- A coin's `max_allocation_pct` is capped by `max_single_asset_allocation_pct` in `global_risk.json`
- `active_assets` in `global_risk.json` is the master switch — assets not listed cannot trade regardless of their own `enabled` flag

**Validation rules (enforced at `run_live_bot.py` startup):**
- Sum of per-asset `max_allocation_pct` for active assets cannot exceed `max_portfolio_allocation_pct`
- No single asset `max_allocation_pct` may exceed `max_single_asset_allocation_pct`
- `min_seconds_left_for_entry` < `max_seconds_left_for_entry`
- All percentage values must be in range 0–100
- `active_assets` must reference assets that have a corresponding `coins/<asset>/config.json`

---

### 5.2 `coins/<asset>/config.json` — Per-Asset Configuration
Each coin config contains:

| Field | Type | Example (BTC) | Description |
|-------|------|---------------|-------------|
| `asset` | string | `"BTC"` | Asset ticker |
| `symbol` | string | `"BTC/USD"` | Spot trading pair |
| `kalshi_series` | string | `"KXBTC15M"` | Kalshi event ticker |
| `cf_index` | string | `"BRTI"` | CF Benchmarks settlement index |
| `enabled` | boolean | `true` | Whether asset is actively traded |
| `max_allocation_pct` | float | 20.0 | Max % of equity allocatable to this asset |
| `vol_15m_dollar` | float | 95.0 | Expected 15-minute dollar volatility |
| `min_seconds_left_for_entry` | float | 60 | Minimum seconds remaining before entry (overrides global) |
| `min_edge_pct` | float | 4.0 | Minimum edge required (matches global) |
| `min_model_prob` | float | 60.0 | Minimum model probability (matches global) |
| `min_entry_price` | float | 0.20 | Minimum entry price in cents (20¢) |
| `max_entry_price` | float | 0.78 | Maximum entry price in cents (78¢) |
| `take_profit_roi_pct` | float | 25.0 | Take-profit target ROI percentage |
| `stop_loss_roi_pct` | float | 25.0 | Stop-loss target ROI percentage |
| `max_trades_per_candle` | int | 3 | Max trades per 15-minute candle |
| `spot_feeds` | object | `{"coinbase": "BTC-USD", "kraken": "XBTUSD", "hyperliquid": "BTC"}` | Exchange → symbol mapping |

**Feed mapping by asset:**
- BTC: coinbase, kraken, hyperliquid (3 feeds)
- ETH: coinbase, kraken, hyperliquid (3 feeds)
- SOL: coinbase, kraken, hyperliquid (3 feeds) — disabled, not traded
- XRP: coinbase, kraken (2 feeds, no Hyperliquid) — disabled, not traded

---

## 6. Extended Core Modules

The following modules exist in `core/` beyond those listed in the Phase 2 architectural tree:

| Module | File | Purpose | Key Classes / Functions |
|--------|------|---------|------------------------|
| Kill Switch | `kill_switch.py` | Emergency halt — stops all new orders when `global_stop_loss_daily_pct` is breached | `KillSwitch`, `check_daily_stop_loss()`, `halt_trading()` |
| Order Saga | `order_saga.py` | Distributed order lifecycle (place → fill → cancel → settle) with compensating transactions | `OrderSaga`, `SagaState`, `compensate_on_missed_ws()`, `orphan_detection()` |
| WS Manager | `ws_manager.py` | WebSocket connection manager with reconnection, heartbeat, multi-feed fanout | `WSManager`, `subscribe_series()`, `heartbeat_loop()` |
| Feed Queue | `feed_queue.py` | Bounded `asyncio.Queue` tick ingestion with backpressure and stale-tick dropping | `FeedQueue`, `ingest_tick()`, `drop_oldest()` |
| DB Async/Pool | `db_async.py`, `db_pool.py` | Async SQLite connection pooling for `bot_database.py` | `DBPool`, `async_connect()`, `connection_pool()` |
| Blue-Green Comparator | `blue_green_comparator.py` | Paper-trade vs live-trade fill comparison for safe deployment | `BlueGreenComparator`, `compare_fills()` |
| Partition Manager | `partition_manager.py` | Data partitioning for hot/cold trade record storage | `PartitionManager`, `archive_old_records()` |
| Profiler | `profiler.py` | cProfile and memory_profiler instrumentation for HFT loops | `HFTProfiler`, `profile_loop()`, `gc_tuner()` |
| Dependencies | `dependencies.py` | Dependency injection for singletons (PortfolioManager, KalshiClient) | `Dependencies`, `get_portfolio_manager()`, `get_kalshi_client()` |
| Multi-Coin Trader | `multi_coin_trader.py` | Multi-coin orchestration layer over per-asset evaluation loops | `MultiCoinTrader`, `run_evaluation_loop()` |
| Portfolio State | `portfolio_state.py` | In-memory position state with TTL-based pruning of closed positions | `PortfolioState`, `update_position()`, `ttl_prune()` |

### 6.1 Module Interaction Diagram

```
run_live_bot.py
    ├── MultiCoinTrader (multi_coin_trader.py)
    │   ├── BTCPredictor → BaseAssetPredictor → RtiAccumulator
    │   ├── ETHPredictor → BaseAssetPredictor → RtiAccumulator
    │   ├── SOLPredictor → BaseAssetPredictor → RtiAccumulator (disabled)
    │   └── XRPPredictor → BaseAssetPredictor → RtiAccumulator (disabled)
    ├── PortfolioManager (portfolio_manager.py) ← asyncio.Lock
    ├── PortfolioState (portfolio_state.py) ← TTL prune
    ├── KillSwitch (kill_switch.py) ← daily stop loss
    ├── OrderSaga (order_saga.py) ← compensating transactions
    ├── WSManager (ws_manager.py) ← WebSocket feeds
    ├── FeedQueue (feed_queue.py) ← bounded async queue
    ├── DBPool (db_pool.py) ← SQLite connection pool
    └── BlueGreenComparator (blue_green_comparator.py) ← paper vs live
```

### 6.2 Edge Cases and Mitigations

| Module | Edge Case | Mitigation |
|--------|-----------|------------|
| Kill Switch | Race condition between stop-loss check and order placement | Atomic check-and-halt with `asyncio.Lock` |
| Order Saga | Missed WebSocket fill → orphan order | 30s portfolio state ↔ exchange sync; auto-cancel via `DELETE /portfolio/events/orders/{order_id}` |
| WS Manager | Feed disconnect during active trade | Reconnect with exponential backoff; fallback to REST poll |
| Feed Queue | Queue full during high volatility | Drop oldest tick; maintain `spot_source` freshness |
| DB Pool | SQLite write contention under concurrent BTC/ETH signals | WAL mode + `busy_timeout` + connection pool |
| Profiler | GC pause during critical trading window | `gc.disable()` during entry window; `gc.collect()` during idle |
| Dependencies | Singleton initialization order | Lazy initialization with double-checked locking |
| Multi-Coin Trader | BTC and ETH signals collide on margin | Portfolio manager lock serializes order sizing |
| Portfolio State | Stale closed positions accumulate in memory | TTL-based pruning of positions older than max_hold_seconds |

### 6.3 Verification Checklist

- [x] `kill_switch.py` halts all new orders when daily stop-loss breached (verified in `test_deployment_pipeline.py`)
- [x] `order_saga.py` detects orphan orders and auto-cancels via `DELETE /portfolio/events/orders/{order_id}` (verified in `test_order_saga.py`)
- [x] `ws_manager.py` reconnects on disconnect with backoff (verified in `test_kalshi_ws_mock.py`)
- [x] `feed_queue.py` drops oldest tick when queue full (no blocking) (verified in `test_feed_queue.py`)
- [x] `db_pool.py` uses WAL mode for concurrent async writes (verified in `test_db_architect.py`)
- [x] `blue_green_comparator.py` detects fill mismatches between paper and live (verified in `test_deployment_pipeline.py`)
- [x] `partition_manager.py` archives records older than TTL to cold storage (verified in `test_fastapi_production.py`)
- [x] `profiler.py` captures cProfile + memory data without affecting trade latency (verified in `test_hft_performance.py`)
- [x] `dependencies.py` provides thread-safe singleton access (verified in `test_fastapi_production.py`)
- [x] `multi_coin_trader.py` serializes per-asset loops through PortfolioManager lock (verified in `test_portfolio_concurrency.py`)
- [x] `portfolio_state.py` prunes closed positions after TTL expiry (verified in `test_portfolio_concurrency.py`)

These modules extend but do not replace the Phase 2 core specification.

---

## 7. Forward Plan — Realistic Paper Trading (No Real Capital)

### 7.1 Objective
Operate the full execution engine (OMS, order saga, kill switch, feed queue) against **real Kalshi order books and real spot feeds** in **paper mode only**. Zero real capital at risk, but full realism in execution logic, slippage modeling, book-depth awareness, and compounding.

**Constraint:** SOL and XRP remain **disabled** (`enabled: false`) throughout this plan.

### 7.2 Phase A: Configuration & Sections 5/6 (Days 1–2)
- [ ] Write Sections 5 & 6 into AGENTS.md (this document)
- [ ] Add config validation to `run_live_bot.py` startup:
  - Reject if `max_portfolio_allocation_pct < sum of active coin max_allocation_pct`
  - Reject if any coin's `max_allocation_pct > max_single_asset_allocation_pct`
  - Reject if `min_seconds_left_for_entry >= max_seconds_left_for_entry`
- [ ] Create `.env` file with Kalshi demo API credentials (not committed to git)
- [ ] Verify `.gitignore` excludes `.env`, `*.db`, `*.log`

### 7.3 Phase B: Book-Depth-Aware Execution (Days 2–3)
- [ ] Implement `BookAwareExecutor` in `core/live_executor.py`:
  - `max_safe_contracts(side, price_cents)` — return max contracts that won't sweep >15% of visible book depth
  - `estimate_slippage(contracts, price_cents)` — model slippage from walking the book
- [ ] Integrate book depth checks into paper trade fill simulation:
  - Paper trades must check real Kalshi L2 order book depth before "filling"
  - If order would sweep >15% of book at that price, reject or split into smaller orders
  - Compound position sizing (half-Kelly) must respect book depth caps
  - Track adverse slippage as a cost in PnL calculation

### 7.4 Phase C: ETH Backtests & Walk-Forwards (Days 3–5)
- [ ] Create `coins/eth/backtests/`:
  - `eth_walk_forward.py` — ETH 15M walk-forward engine
  - `eth_historical_data.py` — Load/simulate historical ETHUSD_RTI settlements
  - `eth_scenarios.py` — Edge cases (flash crash, low vol, high vol)
- [ ] Use CF Benchmarks ETHUSD_RTI 60-second arithmetic mean as ground truth
- [ ] Test drift-diffusion entry at 600s, 450s, 300s, 150s remaining
- [ ] Validate against `min_edge_pct >= 4.0` and `min_model_prob >= 60%`
- [ ] Track non-linear Kalshi taker fee: `0.07 * contracts * price * (1 - price)`

**ETH backtest success criteria:**
- Win rate > 50% after fees
- Max drawdown < 10% (matching `global_stop_loss_daily_pct`)
- Average edge > 4.0% per trade
- No book-sweeping violations (all orders within 15% depth cap)

### 7.5 Phase C.2: ETH Paper Trading Activation
- [ ] ETH is `enabled: true` in `coins/eth/config.json`
- [ ] ETH is in `global_risk.json` `active_assets`
- [ ] ETH trades in paper mode against real ETH order books
- [ ] Monitor ETH-specific metrics separately from BTC

### 7.6 Phase D: Orphaned Order Guard Fix (Critical)
- [ ] Fix `core/order_saga.py` orphan detection timing
- [ ] Ensure `max_hold_seconds` (120s) auto-cancel triggers before orphan occurs
- [ ] Add 30s portfolio state ↔ exchange sync for missed WS fills
- [ ] Verify zero `OMS_ROLLBACK` events for 72 hours of paper trading

### 7.7 Phase E: Realistic Paper Trading — BTC + ETH (Days 5–14)
- [ ] Run `python3 run_live_bot.py --mode paper --serve` continuously
- [ ] Real spot prices from Coinbase/Kraken/Hyperliquid WebSocket
- [ ] Real Kalshi L2 order books from V2 WebSocket
- [ ] Simulated fills against real book depth with slippage model
- [ ] Exact taker fee: `0.07 * contracts * price * (1 - price)`
- [ ] Position sizing guardrails:
  ```
  max_contracts = min(
      kelly_size(equity, edge, prob),
      book_depth_cap(side, price, 0.15),
      max_allocation_pct(equity, coin_config),
      max_concurrent_positions(global_risk)
  )
  ```
- [ ] Monitor per-coin metrics (BTC and ETH separately)

### 7.8 Phase F: Pre-Live Checklist (Gate Before Any Live Trading)
- [ ] ETH backtests pass (walk-forward, all 3 volatility scenarios)
- [ ] ETH paper trading clean for 7+ days with real order books
- [ ] Zero `OMS_ROLLBACK` events for 72 hours
- [ ] Kill switch tested: daily stop-loss >10% halts all new orders
- [ ] Book-depth cap enforced (no order >15% of visible depth)
- [ ] `.env` file with Kalshi demo credentials (not committed)
- [ ] `coins/btc/logs/` and `coins/eth/logs/` populated with structured logs
- [ ] `app.py` dashboard shows real-time PnL, win rate, exposure per coin
- [ ] Blue-green comparison: paper vs simulated-live fill matching within 2 ticks

### 7.9 Phase G: Live Trading (Only After Phase F Complete — Not Part of This Plan)
When ready:
```bash
KALSHI_LIVE_TRADING=1 python3 run_live_bot.py --mode live
```
Start with BTC only. ETH after 1 week of clean BTC live trading. SOL/XRP remain disabled.

---

## 7.10 Immediate Action Items (Next 7 Days)

| Day | Task |
|-----|------|
| 1 | Write Sections 5 & 6 into AGENTS.md; add config validation to `run_live_bot.py` |
| 2 | Implement book-depth-aware execution in `core/live_executor.py` (15% cap) |
| 3 | Create `coins/eth/backtests/eth_walk_forward.py` + historical data loader |
| 4 | Fix orphaned order guard in `core/order_saga.py` |
| 5 | Run ETH walk-forwards across 3 volatility regimes |
| 6 | Start paper trading BTC + ETH with real order books; monitor 48 hours |
| 7 | Review `coins/btc/logs/` and `coins/eth/logs/`; tune slippage model |

### 7.11 Hard Constraints (Do Not Violate)
1. **SOL = `enabled: false`** — do not flip
2. **XRP = `enabled: false`** — do not flip
3. **No `KALSHI_LIVE_TRADING=1`** until Phase F checklist is 100% complete
4. **Max 15% of visible order book depth** per order (paper or live)
5. **Max 2 concurrent positions** (from `global_risk.json`)
6. **Max 20% single-asset allocation** (from `global_risk.json`)
