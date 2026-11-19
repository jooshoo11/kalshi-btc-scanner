# Deployment Checklist: Activating New Crypto Assets (SOL & XRP)

This operational checklist outlines the step-by-step verification protocol required to safely promote a newly configured asset (such as **Solana (SOL)** or **Ripple (XRP)**) from disabled status to live automated trading on Kalshi (`demo-api.kalshi.co` staging and `api.elections.kalshi.com` production).

---

## Pre-Requisites & Repository State

- [ ] Core CI/CD pipeline passes completely:
  ```bash
  ./scripts/pre_deploy_check.sh
  ```
- [ ] Emergency Kill-Switch is verified **ARMED** with zero active `KILL` locks:
  ```bash
  python3 -m core.kill_switch --status
  ```
- [ ] Multi-coin SQLite databases (`multi_coin_paper.db`, `kalshi_audit.db`) are healthy with WAL mode enabled.
- [ ] Kalshi API V2 RSA-PSS credentials configured in `.env` (`KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH` or `KALSHI_PRIVATE_KEY_PEM`).

---

## Phase 1: Configuration & Series Ticker Verification

- [ ] **Inspect Asset Configuration**:
  - For SOL: Inspect `coins/sol/config.json`.
    - Series: `KXSOL15M`
    - Settlement Index: `SOLUSD_RTI` (CF Benchmarks Solana RTI)
    - Spot Feeds: Coinbase (`SOL-USD`), Kraken (`SOLUSD`), Hyperliquid (`SOL`)
    - 15-Minute Dollar Volatility: `vol_15m_dollar: 2.0`
  - For XRP: Inspect `coins/xrp/config.json`.
    - Series: `KXXRP15M`
    - Settlement Index: `XRPUSD_RTI` (CF Benchmarks Ripple RTI)
    - Spot Feeds: Coinbase (`XRP-USD`), Kraken (`XRPUSD`)
    - 15-Minute Dollar Volatility: `vol_15m_dollar: 0.05`
- [ ] **Verify Exchange Market Availability**:
  - Query Kalshi active 15-minute event tickers for the series:
    ```bash
    curl -s "https://demo-api.kalshi.co/trade-api/v2/events?series_ticker=KXSOL15M&status=open"
    curl -s "https://demo-api.kalshi.co/trade-api/v2/events?series_ticker=KXXRP15M&status=open"
    ```
  - Confirm active contract strikes, minimum tick size (1¢), and contract unit size (1 contract = $1.00 notional payout).

---

## Phase 2: Spot Feed Latency & RTI Settlement Calibration

- [ ] **Live Feed Connectivity**:
  - Test WebSocket subscriptions for the underlying spot venues.
  - Verify sub-second tick arrival (< 250ms latency) without feed dropouts.
- [ ] **Outlier Filtering & Composite Median**:
  - Confirm that stale ticks (> 3 seconds old) or divergent quotes (> 1.5% off median) are discarded.
- [ ] **CF Benchmarks RTI Accumulator Precision**:
  - Confirm that `RtiAccumulator` collects samples every second during the final 60 seconds (`seconds_left <= 60`).
  - Compare the computed arithmetic mean against the official published CF Benchmarks index value for 3 test candles. Variance must be $\le 0.01\%$.

---

## Phase 3: 24-Hour Paper Trading Burn-In (Blue Environment)

- [ ] **Enable Asset in Paper Mode**:
  - In `coins/<asset>/config.json`, set `"enabled": true`.
  - In `app.py` / `run_live_bot.py`, remove the asset from `disabled_coins`:
    ```python
    disabled_coins = set()  # Or remove SOL/XRP
    ```
- [ ] **Observe 24-Hour Execution**:
  - Let engine run paper trades through at least 96 consecutive 15-minute contract cycles (24 hours).
  - Verify zero unhandled exceptions or memory leaks.
  - Review trade logs in SQLite:
    ```bash
    sqlite3 multi_coin_paper.db "SELECT coin, count(*), sum(pnl_dollars) FROM trades WHERE coin IN ('SOL', 'XRP') GROUP BY coin;"
    ```
- [ ] **Filter Compliance Audit**:
  - Confirm all trades respected:
    - Min Edge: $\ge +4.0\%$
    - Model Probability: $\ge 60.0\%$
    - Contract Price Bounds: $20¢ \le \text{price} \le 78¢$
    - Max Concurrent Positions: $\le 2$

---

## Phase 4: Blue/Green Shadow Execution & SLA Validation

- [ ] **Activate Blue/Green Shadow Mode**:
  - Connect paper simulation (Blue) and live OMS (Green) in shadow mode.
  - As signals trigger, record comparative fill execution metrics:
    - Slippage Delta: $|\text{Live Price} - \text{Paper Price}| \le 2.0¢$
    - Latency Delta: $\text{Live Fill ACK Delay} \le 500\text{ ms}$
    - Taker Fee Delta: Exact match to $0.07 \times contracts \times price \times (1 - price)$
- [ ] **Check Blue/Green Dashboard Metrics**:
  - Query summary API:
    ```bash
    curl -s http://localhost:8088/api/blue-green/summary
    ```
  - Verify `sla_status: "HEALTHY"` and `recommendation: "PROCEED"`.

---

## Phase 5: Canary Live Deployment (Staging / Production)

- [ ] **Canary Sizing Constraints**:
  - In `coins/<asset>/config.json`, enforce conservative canary constraints:
    - Single-asset max allocation: `max_allocation_pct: 5.0` (reduced from 20%)
    - Max contracts per trade: `max_contracts: 1`
- [ ] **Arm Live Execution**:
  - Set `KALSHI_LIVE_TRADING=1` in the production environment.
- [ ] **Monitor First 5 Live Trades**:
  - Inspect live orderbook sweeps and confirmed exchange order IDs:
    ```bash
    curl -s http://localhost:8088/api/live/status
    ```
  - Confirm zero resting orders remain unfilled for $> 120$ seconds.

---

## Phase 6: Full Production Promotion & Rollback Runbook

- [ ] **Scale Capital Allocation**:
  - Once 20 consecutive live trades execute with positive expectancy and zero SLA breaches:
    - Restore `"max_allocation_pct": 20.0` in `coins/<asset>/config.json`.
- [ ] **Emergency Rollback Procedure**:
  - If any orphaned order (>120s) occurs or API returns repeated errors:
    1. The automated rollback will auto-cancel resting orders via `DELETE /portfolio/events/orders` and set `live_oms.dry_run = True`.
    2. To manually trigger emergency rollback at any time:
       ```bash
       curl -X POST http://localhost:8088/api/rollback -H "Content-Type: application/json" -d '{"reason": "Manual operator intervention"}'
       ```
    3. To halt the entire system with the emergency kill switch:
       ```bash
       python3 -m core.kill_switch --trip "Emergency operator halt"
       ```
