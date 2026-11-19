# Production Backtesting Implementation Playbook
## High-Frequency 15-Minute Binary Options (Kalshi Crypto & CF Benchmarks RTI)

This playbook establishes institutional-grade standards for backtesting quantitative binary options strategies across Bitcoin (`KXBTC15M`), Ethereum (`KXETH15M`), Solana (`KXSOL15M`), and Ripple (`KXXRP15M`) on Kalshi.

---

## 1. Core Architecture & Workflow

```
Market Tick Ingestion (1s) ───▶ Point-in-Time Event Loop ───▶ In-Sample Calibration (W_train)
         │                                   │                                 │
         ▼                                   ▼                                 ▼
CF Benchmarks 60s Settlement        Sequential State Machine           Freeze Best Hyperparameters
         │                                   │                                 │
         ▼                                   ▼                                 ▼
Expiration Arithmetic Mean  ───▶ Exact Kalshi Taker Fees  ───▶ Out-of-Sample Forward Test (W_test)
                                 (0.07 * count * p * (1-p))                    │
                                                                               ▼
                                                                  Institutional Risk Audit
                                                                  (VaR, CVaR, Sharpe, Sortino)
```

---

## 2. Eliminating Backtesting Biases

### A. Look-Ahead Bias (Future Leakage)
1. **RTI Settlement Averaging**:
   - **The Error**: Using the final CF Benchmarks 60-second arithmetic mean $\bar{P}_{RTI}$ or candle close before timestamp $T$.
   - **The Fix**: During seconds $0 \le t < 840$ of a 15-minute candle, the model has access *only* to ticks $\le t$. In the final 60-second window ($840 \le t \le 900$), the settlement accumulator strictly records ticks sequentially:
     $$\bar{P}_{RTI}(t) = \frac{1}{t - 840} \sum_{\tau=841}^t P(\tau)$$
2. **Causal Volatility & Drift Parameters**:
   - Rolling realized volatility $\sigma$ and drift $\mu$ must be computed using backward-looking windows (e.g. past 24 hours of 15m intervals) that end strictly at $t - 1$, never overlapping with the evaluation candle.
3. **Sequential Path Evaluation for Early Exits**:
   - High-water mark bias occurs when assuming a take-profit at $+25\%$ triggered if the candle high reached the threshold. In reality, stop-loss at $-25\%$ may have been triggered *first*.
   - All early exits must evaluate tick-by-tick sequentially over $t \in [1, 900]$.

### B. Survivorship & Selection Bias
1. **Strike Selection**:
   - Backtesting only contracts where the bid-ask spread was tight (e.g., 2¢) introduces survivorship bias.
   - The backtest universe must include illiquid contracts, wide markets (e.g., 10¢ bid / 90¢ ask), and delisted/voided events.
2. **Cherry-Picked Volatility Regimes**:
   - Strategies optimized solely during trending bull runs will collapse during low-volatility chop. Backtests must span diverse regimes: high volatility expansion, directional trending, and range-bound chop.

### C. Execution & Microstructure Realism
1. **Order Book Depth & Touch Fills**:
   - Never assume 200–500 contracts fill at the inside touch ask. On Kalshi, depth at the inside spread is typically 10–50 contracts. Orders exceeding touch depth must model VWAP book sweep slippage (+1¢ to +3¢).
2. **Symmetric Slippage Accounting**:
   - Paper trading engines frequently charge slippage on entry but assume zero slippage on early exits. Both entry and exit must deduct realistic market crossing penalties.

---

## 3. Transaction Cost Modeling: Exact Kalshi Taker Fee Schedule

Kalshi implements a non-linear fee structure calculated per contract and rounded **up to the nearest cent**:

$$\text{Fee}(\$) = \frac{1}{100} \left\lceil 100 \times 0.07 \times \text{Contracts} \times P \times (1 - P) \right\rceil$$

### Fee Characteristics Across Strike Prices
| Price ($P$) | Contracts | Theoretical Fee | Rounded Taker Fee | Drag per Contract |
|---|---|---|---|---|
| **$0.50** | 100 | $1.75 | **$1.75** | **1.75¢** (Maximum) |
| **$0.30 / $0.70** | 100 | $1.47 | **$1.47** | **1.47¢** |
| **$0.20 / $0.80** | 100 | $1.12 | **$1.12** | **1.12¢** |
| **$0.10 / $0.90** | 100 | $0.63 | **$0.63** | **0.63¢** |
| **$0.50** | 1 | $0.0175 | **$0.02** | **2.00¢** (Rounding penalty) |
| **Expiration ($0.00 / $1.00)** | Any | $0.00 | **$0.00** | **0.00¢** (No fee at settlement) |

> [!IMPORTANT]
> Because Kalshi charges **$0.00** at expiration settlement, holding an in-the-money binary to expiration saves taker fees, whereas exiting early incurs a second non-linear taker fee on the exit price.

---

## 4. Walk-Forward Optimization (WFO) Framework

Walk-forward optimization tests strategy robustness and parameter stability over time.

### Rolling Window Protocol
```
Window 1: [─── Train (200 candles) ───][─ Test (50) ─]
Window 2:       [─── Train (200) ───][─ Test (50) ─]
Window 3:             [─── Train (200) ───][─ Test (50) ─]
Window 4:                   [─── Train (200) ───][─ Test (50) ─]
```

1. **In-Sample Window ($W_{train}$)**:
   - Calibrate hyperparameters: `min_edge_pct`, `min_model_prob`, `take_profit_roi_pct`, `stop_loss_roi_pct`.
   - Objective function: Maximize **Annualized Sharpe Ratio** or **Sortino Ratio** subject to $\text{Max Drawdown} \le 20\%$.
2. **Out-of-Sample Window ($W_{test}$)**:
   - Freeze the best parameter combination identified in $W_{train}$.
   - Simulate out-of-sample forward trading without parameter modification.
   - Record equity, trade returns, and friction costs.
3. **Roll Forward**:
   - Shift the window forward by the length of $W_{test}$ and repeat until the historical series is exhausted.
4. **Walk-Forward Efficiency (WFE)**:
   $$\text{WFE} = \frac{\text{Annualized Return}_{OOS}}{\text{Annualized Return}_{IS}}$$
   - $\text{WFE} \ge 60\%$: Robust strategy with genuine edge.
   - $\text{WFE} < 40\%$: Strategy is overfit to historical noise.

---

## 5. Institutional Risk Metrics & Equations

### 1. Value at Risk (VaR)
- **Historical VaR (95%)**:
  $$\text{VaR}_{0.95} = -\text{Percentile}(R, 5)$$
- **Parametric VaR (Normal)**:
  $$\text{VaR}_{0.95} = -(\mu - 1.645 \cdot \sigma)$$

### 2. Conditional Value at Risk (CVaR / Expected Shortfall)
Measures the average loss in the worst 5% tail:
$$\text{CVaR}_{0.95} = -\mathbb{E}[R \mid R \le -\text{VaR}_{0.95}]$$

### 3. Sharpe & Sortino Ratios (Annualized for 24/7 Crypto)
Given $N = 35,040$ fifteen-minute periods per year ($96 \times 365$):
- **Sharpe Ratio**:
  $$\text{Sharpe} = \frac{\bar{R} - R_f}{\sigma_R} \times \sqrt{35,040}$$
- **Sortino Ratio**:
  $$\text{Sortino} = \frac{\bar{R} - R_f}{\sigma_{downside}} \times \sqrt{35,040}$$
  $$\sigma_{downside} = \sqrt{\frac{1}{M} \sum_{i=1}^M \min(0, R_i - R_f)^2}$$

### 4. Maximum Drawdown (MDD) & Calmar Ratio
- **Maximum Drawdown**:
  $$\text{MDD} = \max_{t} \left( \frac{\max_{\tau \le t} E(\tau) - E(t)}{\max_{\tau \le t} E(\tau)} \right)$$
- **Calmar Ratio**:
  $$\text{Calmar} = \frac{\text{CAGR}}{\text{MDD}}$$

---

## 6. Multi-Coin Production Verification Checklist

Before deploying a 15-minute binary options model to live capital:

- [ ] **Point-in-Time Data**: All predictors use strictly retrospective spot feeds.
- [ ] **CF Benchmarks Precision**: Settlement engine matches the 60-second arithmetic mean of BRTI, ETHUSD_RTI, SOLUSD_RTI, and XRPUSD_RTI.
- [ ] **Kalshi Fee Integration**: Every simulated trade applies $\lceil 0.07 \times \text{contracts} \times P \times (1 - P) \times 100 \rceil / 100$.
- [ ] **Bid-Ask Spread Crossing**: Entry prices assume taking the ask ($P_{ask} + \text{slippage}$); exits assume hitting the bid ($P_{bid} - \text{slippage}$).
- [ ] **Walk-Forward Efficiency**: WFE exceeds 50% across at least 4 rolling out-of-sample test windows.
- [ ] **Tail Risk Safeguards**: Historical 95% CVaR is constrained to $< 5.0\%$ of portfolio equity per 15-minute interval.
- [ ] **Automated Test Suite**: Run `pytest core/tests/test_backtest_suite.py` before any model parameter update.
