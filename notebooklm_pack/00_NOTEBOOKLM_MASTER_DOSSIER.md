# Master Project Dossier: Kalshi Multi-Coin Quantitative Options Engine
*Prepared for Google NotebookLM Analysis, Deep Dives, Audio Overviews & Study Guides*

---

## Guide for Google NotebookLM
> **System Context for NotebookLM**:
> You are the Chief Quantitative Architect and Trading Systems Historian for this repository.
> This dossier contains the comprehensive technical architecture, quantitative mathematical formulations, empirical trading results from Trial 1 and Trial 2, risk management protocols, and the full developmental conversation chronicle of an institutional 15-minute crypto binary options trading system on Kalshi.
>
> When answering questions or generating Audio Overviews:
> 1. Ground all explanations in the CF Benchmarks 60-second arithmetic mean settlement index and Drift-Diffusion Jump modeling.
> 2. Highlight how the system evolved from Trial 1 (+1,495% return) to Trial 2 (+96.46% return under strict burn-in controls).
> 3. Emphasize the crucial role of risk gates: 10% daily drawdown kill-switch, 15% order book depth sweep caps, and entry timing windows (240s–500s remaining).

---

# Table of Contents
1. Executive Summary & Mission
2. High-Frequency Quantitative Architecture
3. Quantitative Methodology & Pricing Mathematics
4. CF Benchmarks 60-Second Settlement Accumulator
5. Trade Selection Filters & Sweet-Spot Empirical Findings
6. Order Management System (OMS), Book Sweeps & Fee Accounting
7. Persistence & Event Sourcing Architecture
8. Trading Journal: Trial 1 Review & Key Breakthroughs
9. Trading Journal: Trial 2 Results & Asset Breakdown
10. Incident Investigation: Why Account Equity Held at $196.46
11. Network Resilience & Interface Handover Verification
12. Full Developmental Chronicle & Strategic Milestones
13. Operational Runbook & Production Roadmap

---

# 1. Executive Summary & Mission
The objective of this project is to architect, backtest, calibrate, and operate an institutional-grade, high-frequency quantitative algorithmic trading system tailored for **15-minute binary options on Kalshi** (`demo-api.kalshi.co` / `api.elections.kalshi.com`).

The system trades event contracts settled against real-time CF Benchmarks cryptocurrency reference indices:
- **Bitcoin (BTC)**: Series `KXBTC15M` | Settlement Index: CF Benchmarks `BRTI` (arithmetic mean over final 60 seconds)
- **Ethereum (ETH)**: Series `KXETH15M` | Settlement Index: CF Benchmarks `ETHUSD_RTI`
- **Solana (SOL)**: Series `KXSOL15M` | Settlement Index: CF Benchmarks `SOLUSD_RTI` (currently staged, `enabled: false`)
- **Ripple (XRP)**: Series `KXXRP15M` | Settlement Index: CF Benchmarks `XRPUSD_RTI` (currently staged, `enabled: false`)

The system operates with zero real capital at risk in strict paper/shadow mode (`KALSHI_LIVE_TRADING=0`), running directly against real live order books streamed from Kalshi's V2 WebSocket and sub-second spot feeds from Coinbase, Kraken, and Hyperliquid.

---

# 2. High-Frequency Quantitative Architecture
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

# 3. Quantitative Methodology & Pricing Mathematics
Kalshi 15-minute crypto contracts settle at expiration ($T$) as a binary outcome based on whether the CF Benchmarks Reference Rate ($S_T$) satisfies:
$$\text{Payout} = \begin{cases} \$1.00 & \text{if } S_T \ge K \text{ (for Yes contracts)} \\ \$0.00 & \text{if } S_T < K \end{cases}$$

To price the probability of in-the-money expiration $P(S_T \ge K)$, the system employs a continuous Drift-Diffusion Jump model:
$$dS_t = \mu S_t dt + \sigma S_t dW_t + J_t dN_t$$
where:
- $\mu$: Short-term drift rate estimated from order flow imbalance and momentum across Coinbase, Kraken, and Hyperliquid.
- $\sigma$: Intraday volatility calibrated to 15-minute dollar moves ($95.00 for BTC, asset-calibrated for ETH).
- $W_t$: Standard Brownian motion.
- $N_t$: Poisson jump process accounting for sudden volatility shocks.
- $J_t$: Jump magnitude distribution.

The analytical probability model calculates:
$$d_2 = \frac{\ln(S_t / K) + (\mu - \frac{1}{2}\sigma^2)(T - t)}{\sigma \sqrt{T - t}}$$
$$P(\text{Yes}) = \Phi(d_2)$$

---

# 4. CF Benchmarks 60-Second Settlement Accumulator
Kalshi binary contracts do not settle on the instantaneous spot price at the 15:00 mark. Instead, settlement is governed by CF Benchmarks (BRTI for BTC, ETHUSD_RTI for ETH), which computes an **arithmetic mean of 1-second price snapshots over the final 60 seconds** ($T - 60\text{s}$ to $T$):
$$S_{\text{settle}} = \frac{1}{60} \sum_{i=1}^{60} S_{T - 60 + i}$$

The system implements `core/rti_accumulator.py` to continuously record second-by-second spot ticks. When pricing contracts in the final 60 seconds:
1. Historical observed seconds are fixed.
2. Only remaining unobserved seconds are modeled probabilistically.
3. As $t \to T$, variance collapses to zero, enabling risk-free arbitrage if market prices diverge from the mathematical certainty of the accumulated mean.

---

# 5. Trade Selection Filters & Sweet-Spot Empirical Findings
Empirical testing across hundreds of trades revealed that profitability is concentrated in specific entry price and timing windows:

| Metric | Guardrail Limit | Empirical Rationale |
| :--- | :--- | :--- |
| **Minimum Edge** | $\ge +4.0\%$ | Model probability must exceed market implied probability by $\ge 4\%$ net of fees |
| **Minimum Model Prob** | $\ge 60.0\%$ | High-conviction threshold |
| **Sweet Spot Entry Price** | **$0.20$ to $0.52$** | Avoids overpaying for high-probability contracts ($> 58¢$) that have unfavorable risk/reward |
| **Entry Timing Window** | **$240\text{s}$ to $500\text{s}$** remaining | Avoids extreme gamma flips and slippage in the final 4 minutes of the 15-minute candle |
| **Max Hold Duration** | $600\text{s}$ (10 min) | Allows options theta convergence without holding into chaotic expiration noise |
| **Take-Profit Target** | $+25.0\%$ ROI | Systematic early exit when market moves favorably |
| **Stop-Loss Target** | $-25.0\%$ ROI | Strict loss containment preventing full 100% loss of premium |

---

# 6. Order Management System (OMS), Book Sweeps & Fee Accounting
In real markets, placing large orders into thin binary option order books causes severe market impact. The `BookAwareExecutor` enforces:
1. **$15\%$ Visible Depth Cap**:
   $$\text{Max Contracts} = \min\left(\text{KellySize}, \lfloor 0.15 \times \text{Depth}_{\text{visible}}\rfloor\right)$$
2. **Order Book Walking & VWAP**: Fill prices are modeled by walking the real Kalshi L2 ask/bid book level by level, ensuring that simulated fills accurately reflect live liquidity constraints.
3. **Non-Linear Kalshi Taker Fee**:
   $$\text{Fee} = 0.07 \times N \times P \times (1 - P)$$
   Notice that fees are lowest at extreme prices ($P \to 0$ or $P \to 1$) and peak at $P = 0.50$ (where fee is $1.75¢$ per contract). This is accounted for before any order is submitted.

---

# 7. Persistence & Event Sourcing Architecture
- **Order Saga (`core/order_saga.py`)**: Distributed state machine tracking every order through:
  $$\text{PENDING} \longrightarrow \text{SUBMITTED} \longrightarrow \text{PARTIAL\_FILL} \longrightarrow \text{FILLED} \longrightarrow \text{SETTLED}$$
  Automatic compensating transactions cancel unconfirmed orders after timeouts to prevent "ghost" or orphaned orders.
- **Event-Sourced Persistence (`core/bot_database.py`)**: All state transitions are recorded as immutable, append-only events. Database triggers prevent any `UPDATE` or `DELETE` operations on trade records.
- **SQLite Performance Tuning**: Configured with Write-Ahead Logging (`PRAGMA journal_mode=WAL`), `busy_timeout=5000ms`, a 64MB memory cache, and hot-to-cold Parquet data partitioning.

---

# 8. Trading Journal: Trial 1 Review & Key Breakthroughs
In Trial 1, the engine scaled aggressively using fractional Kelly compounding:
- **Initial Capital**: $100.00
- **Final Equity**: $1,595.48 (+1,495.48% return)
- **Total Closed Trades**: 254 | **Win Rate**: 69.7% | **Profit Factor**: 2.50
- **Key Empirical Breakthroughs**:
  1. **Candle Expiration Timing**:
     - Trades entered with $> 300\text{s}$ remaining achieved **69%–74% win rates**, generating over 95% of total profits.
     - Trades entered late in the candle ($< 240\text{s}$) suffered high-gamma chop and sudden spread-widening, causing sharp drawdowns.
  2. **Entry Price Compression**:
     - Contracts bought above 58¢ suffered poor risk/reward: winning 1 contract yielded 42¢ while losing cost 58¢.
     - Contracts bought between 20¢ and 52¢ yielded superior expectancy, providing a massive buffer for 25% take-profit limits.
- **Action Taken**: Trial 1 state was archived to `archive/trial1/`, and parameters were tightened for a clean-slate Trial 2.

---

# 9. Trading Journal: Trial 2 Results & Asset Breakdown
Trial 2 was launched with fresh $100.00 capital, enforcing:
- `min_seconds_left_for_entry: 240s`
- `max_seconds_left_for_entry: 500s`
- Sweet-spot ceiling: $\le 52¢$
- SOL and XRP strictly disabled.

### Aggregate Performance Table:
| Asset | Ticker | Series | Trades | Record (W / L) | Win Rate | Net PnL | Capital Allocation | Status |
| :--- | :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **Bitcoin** | BTC | `KXBTC15M` | 85 | 56W / 29L | **65.9%** | **+$41.86** | $98.23 (50%) | Active |
| **Ethereum** | ETH | `KXETH15M` | 71 | 48W / 23L | **67.6%** | **+$54.60** | $98.23 (50%) | Active |
| **Solana** | SOL | `KXSOL15M` | 0 | 0W / 0L | — | $0.00 | $0.00 (0%) | **Disabled** (`enabled: false`) |
| **Ripple** | XRP | `KXXRP15M` | 0 | 0W / 0L | — | $0.00 | $0.00 (0%) | **Disabled** (`enabled: false`) |
| **Total** | — | — | **156** | **104W / 52L** | **66.7%** | **+$96.46** | **$196.46** | **+96.46% ROI** |

---

# 10. Incident Investigation: Why Account Equity Held at $196.46
During the Trial 2 session:
1. Cumulative equity climbed steadily from $100.00 to an intraday high of **$221.40**.
2. Two consecutive trade losses occurred:
   - Trade #155 (ETH): $-\$8.42$
   - Trade #156 (BTC): $-\$16.52$
3. Total equity dropped from $\$221.40$ to **$\$196.46$** (a pullback of $11.26\%$).
4. In `global_risk.json`, `global_stop_loss_daily_pct` is set to **$10.0\%$**.
5. The `KillSwitch` daemon detected the breach and generated the `KILL` flag file.
6. The engine immediately entered protective sleep mode:
   ```
   [app.py] KILL file detected, sleeping 10s...
   ```
7. **Outcome**: The circuit breaker performed its exact intended risk function: halting new risk exposure and preserving **+$96.46 (+96.46%)** net profit from the original $100 baseline.

---

# 11. Network Resilience & Interface Handover Verification
During live paper execution, the physical Ethernet cable (`enp0s31f6`) was unplugged to test system resilience:
- System seamlessly shifted traffic to Wi-Fi interface `wlp2s0` (`10.0.0.19`).
- Auto-reconnect handlers in `core/kalshi_client.py` and `core/live_executor.py` restored WebSocket subscriptions with zero lost state.
- Background daemons (`run_live_bot.py` on port 8088) remained uninterrupted.

---

# 12. Full Developmental Chronicle & Strategic Milestones
1. **Multi-Asset Decoupling**: Genericized `RtiAccumulator` and built concurrency-safe `PortfolioManager`.
2. **Trial 1 Calibration Run**: Proved high-expectancy viability with 254 trades and +1,495% ROI.
3. **Trial 2 Parameter Burn-In**: Confirmed 66.7% win rate under strict 20¢–52¢ price caps and 240s–500s timing rules.
4. **Automated 15-Minute Monitoring System**: Deployed lightweight `bot_monitor` subagent on cron schedule with persistent memory logging.
5. **Physical Hardware Disconnect Test**: Validated uninterrupted execution during interface transitions.
6. **Risk Management Safeguard Validation**: Verified 10% daily drawdown circuit breaker trip at $196.46.

---

# 13. Operational Runbook & Production Roadmap
1. **Pre-Deployment Checklist**: Verify linting, unit tests, RTI regression, and paper smoke test via `scripts/pre_deploy_check.sh`.
2. **Circuit Breaker Reset**: When beginning a new trading day, call `POST /api/kill-switch/reset` to re-initialize baseline equity.
3. **Live Trading Promotion Gate**:
   - Maintain $\ge 60\%$ win rate over 500+ paper trades.
   - Zero unhandled exceptions or orphan orders over 7 continuous days.
   - Toggle `KALSHI_LIVE_TRADING=1` only with explicit consensus.
