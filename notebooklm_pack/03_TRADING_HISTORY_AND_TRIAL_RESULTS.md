# Section 3: Empirical Trading History, Trial Runs & Analysis

## 1. Overview of Experimental Runs
The trading engine underwent two major live-market paper trial runs against real Kalshi order books and live spot exchanges:

```
[Trial 1: Exploratory Calibration]
├── Initial Capital: $100.00
├── Total Closed Trades: 254
├── Final Equity: $1,595.48 (+1,495.48% return)
├── Overall Win Rate: 69.7%
└── Profit Factor: 2.50

[Trial 2: Strict Burn-In & Parameter Hardening]
├── Initial Capital: $100.00
├── Total Closed Trades: 156
├── Final Equity: $196.46 (+96.46% return)
├── Overall Win Rate: 66.7%
└── Profit Factor: 1.84 (Gains safely locked by 10% daily drawdown circuit breaker)
```

---

## 2. Trial 1 Deep-Dive & Quantitative Findings
In Trial 1, the engine scaled aggressively using fractional Kelly compounding:
- **Net Profit**: +$1,495.48 on a $100 stake.
- **Key Empirical Breakthroughs**:
  1. **Candle Expiration Timing**:
     - Trades entered with $> 300\text{s}$ remaining achieved **69%–74% win rates**, generating over 95% of total profits.
     - Trades entered late in the candle ($< 240\text{s}$) suffered high-gamma chop and sudden spread-widening, causing sharp drawdowns.
  2. **Entry Price Compression**:
     - Contracts bought above 58¢ suffered poor risk/reward: winning 1 contract yielded 42¢ while losing cost 58¢.
     - Contracts bought between 20¢ and 52¢ yielded superior expectancy, providing a massive buffer for 25% take-profit limits.
- **Action Taken**: Trial 1 state was archived to `archive/trial1/`, and parameters were tightened for a clean-slate Trial 2.

---

## 3. Trial 2 Results & Per-Asset Breakdown
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

## 4. The Circuit Breaker Event: Why Equity Held at $196.46
During the Trial 2 session:
1. Cumulative equity climbed steadily from $100.00 to an intraday high of **$221.40**.
2. Two consecutive trade losses occurred:
   - Trade #155 (ETH): $-\$8.42$
   - Trade #156 (BTC): $-\$16.52$
3. Total equity dropped from $\$221.40$ to **$\$196.46$** (a pullback of $11.26\%$).
4. In `global_risk.json`, `global_stop_loss_daily_pct` is set to **$10.0\%$**.
5. The `KillSwitch` daemon detected the breach and generated the [`KILL`](file:///home/jooshoo/kalshi-btc-scanner-clean/KILL) flag file.
6. The engine immediately entered protective sleep mode:
   ```
   [app.py] KILL file detected, sleeping 10s...
   ```
7. **Outcome**: The circuit breaker performed its exact intended risk function: halting new risk exposure and preserving **+$96.46 (+96.46%)** net profit from the original $100 baseline.

---

## 5. Network Failover Validation (Ethernet to Wi-Fi)
During live paper execution, the physical Ethernet cable (`enp0s31f6`) was unplugged to test system resilience:
- System seamlessly shifted traffic to Wi-Fi interface `wlp2s0` (`10.0.0.19`).
- Auto-reconnect handlers in `core/kalshi_client.py` and `core/live_executor.py` restored WebSocket subscriptions with zero lost state.
- Background daemons (`run_live_bot.py` on port 8088) remained uninterrupted.
