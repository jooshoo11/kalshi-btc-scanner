# Section 2: Quantitative Methodology, Pricing Math & Engineering

## 1. Drift-Diffusion Jump Process Pricing
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

## 2. CF Benchmarks 60-Second Settlement Index Accumulator
Kalshi binary contracts do not settle on the instantaneous spot price at the 15:00 mark. Instead, settlement is governed by CF Benchmarks (BRTI for BTC, ETHUSD_RTI for ETH), which computes an **arithmetic mean of 1-second price snapshots over the final 60 seconds** ($T - 60\text{s}$ to $T$):
$$S_{\text{settle}} = \frac{1}{60} \sum_{i=1}^{60} S_{T - 60 + i}$$

The system implements `core/rti_accumulator.py` to continuously record second-by-second spot ticks. When pricing contracts in the final 60 seconds:
1. Historical observed seconds are fixed.
2. Only remaining unobserved seconds are modeled probabilistically.
3. As $t \to T$, variance collapses to zero, enabling risk-free arbitrage if market prices diverge from the mathematical certainty of the accumulated mean.

---

## 3. Trade Selection Filters & Sweet-Spot Calibration
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

## 4. Book-Depth-Aware Execution & Slippage Modeling
In real markets, placing large orders into thin binary option order books causes severe market impact. The `BookAwareExecutor` enforces:
1. **$15\%$ Visible Depth Cap**:
   $$\text{Max Contracts} = \min\left(\text{KellySize}, \lfloor 0.15 \times \text{Depth}_{\text{visible}}\rfloor\right)$$
2. **Order Book Walking & VWAP**: Fill prices are modeled by walking the real Kalshi L2 ask/bid book level by level, ensuring that simulated fills accurately reflect live liquidity constraints.
3. **Non-Linear Kalshi Taker Fee**:
   $$\text{Fee} = 0.07 \times N \times P \times (1 - P)$$
   Notice that fees are lowest at extreme prices ($P \to 0$ or $P \to 1$) and peak at $P = 0.50$ (where fee is $1.75¢$ per contract). This is accounted for before any order is submitted.

---

## 5. Event Sourcing, CQRS & Distributed Order Sagas
- **Order Saga (`core/order_saga.py`)**: Distributed state machine tracking every order through:
  $$\text{PENDING} \longrightarrow \text{SUBMITTED} \longrightarrow \text{PARTIAL\_FILL} \longrightarrow \text{FILLED} \longrightarrow \text{SETTLED}$$
  Automatic compensating transactions cancel unconfirmed orders after timeouts to prevent "ghost" or orphaned orders.
- **Event-Sourced Persistence (`core/bot_database.py`)**: All state transitions are recorded as immutable, append-only events. Database triggers prevent any `UPDATE` or `DELETE` operations on trade records.
- **SQLite Performance Tuning**: Configured with Write-Ahead Logging (`PRAGMA journal_mode=WAL`), `busy_timeout=5000ms`, a 64MB memory cache, and hot-to-cold Parquet data partitioning.
