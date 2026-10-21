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

### Phase 1: Directory Layout & Configuration (Architect Agent)
1. **Target Directory Structure**:
   - Create `core/` for shared infrastructure.
   - Create isolated coin workspaces: `coins/btc/`, `coins/eth/`, `coins/sol/`, `coins/xrp/`.
2. **Scaffold Coin Subdirectories**:
   - Each asset directory must contain: `predictors/`, `backtests/`, `tests/`, `logs/`, `updates/`.
3. **Generate Asset `config.json` Files**:
   - `coins/btc/config.json`: Series `KXBTC15M`, Index `BRTI`, spot feeds `["coinbase", "kraken", "hyperliquid"]`.
   - `coins/eth/config.json`: Series `KXETH15M`, Index `ETHUSD_RTI`, spot feeds `["coinbase", "kraken", "hyperliquid"]`.
   - `coins/sol/config.json`: Series `KXSOL15M`, Index `SOLUSD_RTI`, spot feeds `["coinbase", "kraken", "hyperliquid"]`.
   - `coins/xrp/config.json`: Series `KXXRP15M`, Index `XRPUSD_RTI`, spot feeds `["coinbase", "kraken"]`.
4. **Update `.gitignore`**:
   - Ignore `coins/*/logs/`, `coins/*/updates/`, `coins/*/backtests/*.csv`, `coins/*/backtests/*.parquet`.

---

### Phase 2: Decouple & Genericize Core (Refactoring Agent)
1. **Move Universal Modules to `core/`**:
   - `live_executor.py`, `kalshi_client.py`, `bot_database.py`, `prediction_engine.py`.
2. **Genericize Classes**:
   - Replace single-asset BRTI accumulator with `RtiAccumulator(index_name: str, window_seconds: int = 60)`.
   - Refactor `KalshiClient` to manage concurrent WebSocket subscriptions across `["KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M"]`.
   - Enable `KalshiOrderManagementSystem` and `ScannerCore` to accept arbitrary series tickers, symbols, and parameters dynamically.
3. **Build `core/portfolio_manager.py`**:
   - Centralize live balance synchronization against `/trade-api/v2/portfolio/balance`.
   - Support risk budgeting (e.g. 15% equity per trade split across active opportunities).
   - Use an `asyncio.Lock()` around margin calculation and reservation to guarantee concurrent signals from BTC and SOL do not exceed available buying power.

---

### Phase 3: Coin Isolation & Model Placement (Refactoring Agent)
1. **Migrate Prediction Models**:
   - Migrate existing BTC models into `coins/btc/predictors/`.
   - Implement standard base predictor interface `BasePredictor` in `core/` and inherit it in `coins/eth/predictors/`, `coins/sol/predictors/`, and `coins/xrp/predictors/`.
2. **Unified Runner (`run_live_bot.py`)**:
   - Load `global_risk.json` and coin configs `coins/*/config.json`.
   - Initialize unified `KalshiClient` and `PortfolioManager`.
   - Concurrently spawn 1-second evaluation loops across all enabled coins, dispatching signals into the shared, lock-guarded execution engine.

---

### Phase 4: Verification & Integration (QA / Reviewer Agent)
1. **Import Integrity Audit**:
   - Enforce clean import paths (`core.*`, `coins.<asset>.*`).
   - Eliminate obsolete relative imports or orphaned references.
2. **Concurrency & Capital Test**:
   - Create `core/tests/test_portfolio_concurrency.py` simulating simultaneous trade triggers from BTC and SOL.
   - Confirm that the portfolio lock serializes order sizing and rejects trades if margin is exhausted.
3. **Baseline Test Execution**:
   - Run `pytest core/tests/` and verify that the generic `RtiAccumulator` matches CF Benchmarks 60-second arithmetic mean settlement logic.
