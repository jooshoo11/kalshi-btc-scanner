# Section 1: Project Mission & High-Frequency Quantitative Architecture

## 1. Executive Summary & Mission
The objective of this project is to architect, backtest, calibrate, and operate an institutional-grade, high-frequency quantitative algorithmic trading system tailored for **15-minute binary options on Kalshi** (`demo-api.kalshi.co` / `api.elections.kalshi.com`). 

The system trades event contracts settled against real-time CF Benchmarks cryptocurrency reference indices:
- **Bitcoin (BTC)**: Series `KXBTC15M` | Settlement Index: CF Benchmarks `BRTI` (arithmetic mean over final 60 seconds)
- **Ethereum (ETH)**: Series `KXETH15M` | Settlement Index: CF Benchmarks `ETHUSD_RTI`
- **Solana (SOL)**: Series `KXSOL15M` | Settlement Index: CF Benchmarks `SOLUSD_RTI` (currently staged, `enabled: false`)
- **Ripple (XRP)**: Series `KXXRP15M` | Settlement Index: CF Benchmarks `XRPUSD_RTI` (currently staged, `enabled: false`)

The system operates with zero real capital at risk in strict paper/shadow mode (`KALSHI_LIVE_TRADING=0`), running directly against real live order books streamed from Kalshi's V2 WebSocket and sub-second spot feeds from Coinbase, Kraken, and Hyperliquid.

---

## 2. Quantitative System Architecture

The repository enforces strict separation between shared quantitative infrastructure (`core/`) and isolated asset workspaces (`coins/<asset>/`):

```
kalshi_scanner/
├── core/                                # Shared Quantitative Infrastructure
│   ├── kalshi_client.py                 # Multi-series Kalshi REST & WebSocket V2 client (RSA-PSS)
│   ├── live_executor.py                 # OMS, L2 VWAP sweeps, execution, V2 cancel routing
│   ├── scanner_core.py                  # Multi-market discovery & tick pipeline
│   ├── rti_accumulator.py              # Generic CF Benchmarks 60s arithmetic mean accumulator
│   ├── prediction_engine.py             # Quant drift-diffusion pricing & Kelly sizing
│   ├── portfolio_manager.py             # Global cash & multi-asset capital allocator (async lock)
│   ├── bot_database.py                  # Event-sourced SQLite persistence (CQRS + WAL)
│   ├── order_saga.py                    # Distributed order lifecycle & compensating transactions
│   ├── kill_switch.py                   # 10% daily drawdown circuit breaker
│   ├── ws_manager.py                    # WebSocket reconnection, keepalive & multi-feed fanout
│   ├── feed_queue.py                    # Bounded async tick ingestion with backpressure
│   ├── db_pool.py                       # SQLite connection pooling (WAL, 64MB cache)
│   └── partition_manager.py             # Hot in-memory tier & cold Parquet archival
├── coins/                               # Asset-Specific Workspaces
│   ├── btc/                             # KXBTC15M / BRTI config, models, backtests, tests
│   ├── eth/                             # KXETH15M / ETHUSD_RTI config, models, backtests, tests
│   ├── sol/                             # KXSOL15M / SOLUSD_RTI (staged, disabled)
│   └── xrp/                             # KXXRP15M / XRPUSD_RTI (staged, disabled)
├── global_risk.json                     # System-wide capital limits, Kelly fractions, max drawdown
├── run_live_bot.py                      # Multi-asset live execution orchestrator & API server
└── app.py                               # FastAPI real-time multi-coin dashboard
```

---

## 3. Core Design Principles & Guardrails
1. **No Circular Dependencies**: `coins/<asset>/` can import from `core.*`, but `core/` never imports from `coins.*`.
2. **Strict Currency Isolation**: Assets not explicitly enabled in both `global_risk.json` (`active_assets`) and their respective `config.json` are hard-blocked from trading. Currently, SOL and XRP are permanently disabled (`enabled: false`, allocation weight: 0.0%).
3. **Execution Realism**: Paper trades check live Kalshi L2 depth before simulating fills. Any order sweeping $> 15\%$ of visible depth is rejected or split.
4. **Non-Linear Fee Modeling**: Kalshi's exact exchange fee formula is modeled on every tick:
   $$\text{Fee} = 0.07 \times \text{contracts} \times P \times (1 - P)$$
5. **Circuit Breakers**: A 10% daily drawdown threshold relative to intraday peak equity automatically trips the system kill-switch, locking in accumulated profits.
