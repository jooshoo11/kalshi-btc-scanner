# Quantitative Audit & Code Archaeology Report
## Deconstruction of the +2,000% Run & Demo Bot Alignment Audit

**Author:** Lead Quantitative Auditor & Code Archaeologist  
**Timestamp:** October 19, 2026 22:52 EDT (2026-10-20 02:52 UTC)  
**Database Audited:** `/home/jooshoo/kalshi_scanner/kalshi_audit.db` & `kalshi_live_demo.db`  
**Repository:** `/home/jooshoo/kalshi_scanner`

---

## Executive Summary

1. **The Winning Commit**:
   - The exact commit active during the famous **+2,000% return / ~84% win rate** run (Trades #105 to #116) was **`8763b865d746afec9849d27d06910888461d8115`** (`feat(institutional): implement Phase 2 live execution OMS and Phase 3 safety reconciliation with stale-tick watchdog`, committed at `2026-10-19 14:16:21 -0400`).
   - The +2,000% threshold was officially crossed at **19:25:31 EDT** on Trade #113 (Equity: **$1,063.99**, starting from **$50.00**, return **+2,028.0%**, win rate **84.1%**).
   - By Trade #116 at **19:38:25 EDT**, equity reached **$1,207.03** (**+2,314.1%**, **84.5% win rate** with 98 wins / 18 losses).

2. **The Core Engine of the +2,000% Run**:
   - **Uncapped 15% Equity Compounding**: Position size was `min(cash, equity * 15%)`. Without a dollar ceiling, each consecutive win scaled up position sizing from $7.04 (Trade #1) to $160.64 (Trade #116).
   - **Dynamic Profit Securing (+25% Take Profit)**: **86 out of 107 total wins (80.4%) were early take-profit exits**, held for an average duration of only **267 seconds (~4.5 minutes)**. The bot locked in profits the moment the contract bid jumped +25% rather than waiting for 15-minute binary expiry reversals.
   - **Intra-Candle Re-entry**: Allowed up to 3 trades per 15-minute candle with an 8-second cooldown, repeatedly scalping the same trending candle (e.g. Trades #105, #106, #107 on `KXBTC15M-26OCT191830-30` captured +$145.66 in one candle).
   - **Dual-Confirmation Stop Loss**: Hard stops at -25% were ONLY triggered if model probability simultaneously collapsed below 35% (`unrealized_pct <= -25.0 and curr_model_prob <= 35.0`). Positions were not shaken out by temporary bid blips.

3. **Distortions / Bloat Factors in the Paper Run**:
   - **Theoretical Fills at Touch Without Book Depth**: In paper trading, orders of 200–380 contracts filled instantly at the inside touch price. In real orderbooks, available depth at the touch is often 10–50 contracts.
   - **Asymmetric Exit Slippage**: Commit `8763b86` charged a 3¢ slippage penalty on entry, but **0¢ slippage on exit** (`_close_position` sold into `current_bid` at 100% fill price).
   - **Feed Basis Spread**: In commit `8763b86`, `spot_price` tracked Hyperliquid perpetual futures while `candle_open` tracked Kraken OHLC. Basis divergences during strong momentum inflated model probability deltas.

4. **Critical Diagnosis of the Current Demo Bot (`run_live_bot.py` / `live_executor.py`)**:
   - **SHOWSTOPPER BUG FOUND**: In `run_live_bot.py` line 412, expiration settlement calls `self.db.record_trade_close(..., rationale=...)`. In `bot_database.py`, `record_trade_close` **does not accept `rationale`**, raising a `TypeError`. This crashes the loop before `self.active_position = None` can execute, trapping the bot into holding expired positions in an infinite crash loop (observed live on Trade #2).
   - **Position Sizing Divergence**: Current `run_live_bot.py` allocates a flat 15% equity, omitting the historical dynamic Net Post-Fee Half-Kelly check (`alloc_pct = min(0.15, half_kelly_frac)`).

---

## 1. Commit and Timestamp Archaeological Evidence

### A. Database Audit Trail (`kalshi_audit.db`)
Starting capital: **$50.00** at Trade #1 (2026-10-17 15:10:30 EDT).

| Trade # | Timestamp (EDT) | Ticker | Side @ Price | Qty | Cost | Exit P | Exit Reason | PnL ($) | Cumulative PnL | Equity ($) | Return (%) | Win Rate |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **1** | 10/17 15:10:30 | KXBTC15M-26OCT171515-15 | YES @ 44¢ | 16 | $7.04 | 57¢ | TAKE_PROFIT | +$1.92 | +$1.92 | $51.92 | +3.8% | 100.0% |
| **50** | 10/17 21:00:55 | KXBTC15M-26OCT172115-15 | YES @ 47¢ | 29 | $13.63 | 61¢ | TAKE_PROFIT | +$3.77 | +$47.64 | $97.64 | +95.3% | 80.0% |
| **82** | 10/19 14:15:52 | KXBTC15M-26OCT191430-30 | YES @ 49¢ | 112 | $54.88 | 71¢ | TAKE_PROFIT | +$23.75 | +$266.38 | $316.38 | +532.8% | 82.9% |
| **100** | 10/19 17:00:56 | KXBTC15M-26OCT191715-15 | YES @ 74¢ | 86 | $63.64 | 93¢ | TAKE_PROFIT | +$14.78 | +$461.36 | $511.36 | +922.7% | 83.0% |
| **105** | 10/19 18:15:56 | KXBTC15M-26OCT191830-30 | YES @ 60¢ | 126 | $75.60 | 83¢ | TAKE_PROFIT | +$25.61 | +$557.33 | $607.33 | +1114.7% | 82.9% |
| **106** | 10/19 18:23:50 | KXBTC15M-26OCT191830-30 | YES @ 69¢ | 116 | $80.04 | 91¢ | TAKE_PROFIT | +$23.11 | +$580.44 | $630.44 | +1160.9% | 83.0% |
| **107** | 10/19 18:29:24 | KXBTC15M-26OCT191830-30 | YES @ 45¢ | 182 | $81.90 | $1.00 | SETTLED_WIN | +$96.94 | +$677.38 | $727.38 | +1354.8% | 83.2% |
| **108** | 10/19 18:30:51 | KXBTC15M-26OCT191845-45 | YES @ 43¢ | 223 | $95.89 | $1.00 | SETTLED_WIN | +$123.28 | +$800.66 | $850.66 | +1601.3% | 83.3% |
| **109** | 10/19 18:45:55 | KXBTC15M-26OCT191900-00 | YES @ 51¢ | 140 | $71.40 | $1.00 | SETTLED_WIN | +$66.15 | +$866.81 | $916.81 | +1733.6% | 83.5% |
| **110** | 10/19 19:00:53 | KXBTC15M-26OCT191915-15 | YES @ 58¢ | 214 | $124.12 | 78¢ | TAKE_PROFIT | +$36.57 | +$903.38 | $953.38 | +1806.8% | 83.6% |
| **111** | 10/19 19:05:07 | KXBTC15M-26OCT191915-15 | YES @ 72¢ | 181 | $130.32 | $1.00 | SETTLED_WIN | +$48.12 | +$951.50 | $1,001.50 | +1903.0% | 83.8% |
| **112** | 10/19 19:15:51 | KXBTC15M-26OCT191930-30 | YES @ 35¢ | 382 | $133.70 | 47¢ | TAKE_PROFIT | +$33.08 | +$984.58 | $1,034.58 | +1969.2% | 83.9% |
| **113** | 10/19 19:25:31 | KXBTC15M-26OCT191930-30 | YES @ 51¢ | 276 | $140.76 | 65¢ | TAKE_PROFIT | +$29.41 | +$1,013.99 | **$1,063.99** | **+2,028.0%** | **84.1%** |
| **114** | 10/19 19:26:06 | KXBTC15M-26OCT191930-30 | YES @ 69¢ | 212 | $146.28 | 91¢ | TAKE_PROFIT | +$42.24 | +$1,056.23 | $1,106.23 | +2112.5% | 84.2% |
| **115** | 10/19 19:30:56 | KXBTC15M-26OCT191945-45 | YES @ 41¢ | 365 | $149.65 | 60¢ | TAKE_PROFIT | +$57.02 | +$1,113.25 | $1,163.25 | +2226.5% | 84.3% |
| **116** | 10/19 19:33:44 | KXBTC15M-26OCT191945-45 | YES @ 64¢ | 251 | $160.64 | 84¢ | TAKE_PROFIT | +$43.78 | +$1,157.03 | **$1,207.03** | **+2,314.1%** | **84.5%** |

### B. Git Commit Timeline Correlation
- **Commit `8763b865d746afec9849d27d06910888461d8115`**:
  - Author Date: `Mon Oct 19 14:16:21 2026 -0400`
  - Active continuously across Trades #82 through #116 (from 14:15:52 EDT to 19:38:25 EDT).
- **Commit `ea7c93c6dd6aa2f6bf2fc40efcaed44b38796c58`**:
  - Author Date: `Mon Oct 19 19:40:28 2026 -0400` (created exactly 2 minutes after Trade #116 exited).
  - Applied 3¢ adverse slippage to early market exits in `_close_position`.
- **Commit `70635df5ab516a18792921a574cddf64479fd84b`**:
  - Author Date: `Mon Oct 19 20:43:22 2026 -0400`
  - Clamped position size with a `$75.00` hard dollar cap, enforced a `25¢` floor, added re-entry lockout on loss, and made stop losses unconditional.

---

## 2. Winning Code Architecture (Commit `8763b86`)

### `paper_trader.py` (Winning Configuration)
```python
# Parameters
self.take_profit_roi_pct = 25.0    # Lock in profit at +25% ROI
self.stop_loss_roi_pct = 25.0      # Cut loss at -25% ROI (Dual-confirmation)
self.max_allocation_pct = 15.0     # 15% equity per trade
self.min_edge_pct = 4.0            # Minimum +4.0% model edge
self.slippage_penalty_cents = 3.0  # 3¢ adverse entry slippage
self.max_trades_per_candle = 3     # Intra-candle re-entry enabled

# Entry Logic
if "BUY YES" in signal and edge_pct >= self.min_edge_pct and model_prob_yes >= 60.0 and yes_ask > 0 and yes_ask <= 0.78:
    target_side = "YES"
    target_price = yes_ask
    model_prob = model_prob_yes
elif "BUY NO" in signal and edge_pct >= self.min_edge_pct and model_prob_no >= 60.0 and no_ask > 0 and no_ask <= 0.78:
    target_side = "NO"
    target_price = no_ask
    model_prob = model_prob_no

if not target_side or target_price <= 0.05:
    return

# Net Post-Fee Half-Kelly Allocation (Uncapped)
half_kelly_frac = float(scalp_data.get("half_kelly_pct", 0.0)) / 100.0
alloc_pct = min(self.max_allocation_pct / 100.0, half_kelly_frac)
equity = self.get_equity()
budget = min(self.cash, equity * alloc_pct)

fill_price = min(0.99, target_price + (self.slippage_penalty_cents / 100.0))
fee_per_contract = calculate_kalshi_fee(1, fill_price)
contracts = int(budget / (fill_price + fee_per_contract))

# Dual-Confirmation Stop Loss
if unrealized_pct <= -self.stop_loss_roi_pct and curr_model_prob <= 35.0:
    self._close_position(pos, exit_price=current_bid, reason="STOP_LOSS", ...)

# Active Take Profit
if unrealized_pct >= self.take_profit_roi_pct and (time.time() - pos["entry_timestamp"] >= 5.0):
    self._close_position(pos, exit_price=current_bid, reason="TAKE_PROFIT", ...)
```

---

## 3. Trading Mechanics & Performance Reality Check

### Did it compound 15% equity uncapped?
**YES.** There was no dollar cap. Position budget was `min(cash, equity * 15%)`. Sizing grew exponentially from $7.04 to $160.64.

### Trade Outcome Distribution (125 Total Trades)
- **TAKE_PROFIT**: 86 (68.8% of all trades, 80.4% of wins). Mean hold time: **267 seconds (~4.5 minutes)**.
- **SETTLED_WIN**: 21 (16.8% of all trades). Held to expiry (840–900s).
- **STOP_LOSS**: 16 (12.8% of all trades). Mean hold time: **294 seconds**.
- **SETTLED_LOSS**: 2 (1.6% of all trades).
- **Overall Win Rate**: **85.6%** (107 / 125).

### Was the +2,000% return bloated or distorted?
1. **Touch Fill Assumption vs Book Depth**:
   - The paper bot simulated fills for 200–380 contracts at the touch price. On Kalshi KXBTC15M, inside market maker quotes rarely exceed 25–50 contracts at the top level. Sweeping 300 contracts in live markets causes multi-tick slippage.
2. **Asymmetric Exit Slippage**:
   - In commit `8763b86`, take-profit sells received 100% of the inside bid price with zero slippage haircut.
3. **Spot Feed Basis Spread**:
   - `spot_price` came from Hyperliquid perpetual futures, while `candle_open` was sampled from Kraken USD spot. When perpetual futures traded at a premium or discount during sharp momentum moves, the resulting delta `(perp - spot_open)` produced elevated model probabilities.

---

## 4. Line-by-Line Comparison: Historical vs Current Implementation

| Feature / Logic | Winning Run (`8763b86`) | Current `run_live_bot.py` | Current `live_executor.py` | Current `paper_trader.py` (working tree) |
|---|---|---|---|---|
| **Max Trade Size** | **Uncapped** (15% equity) | **Uncapped** (`equity * 0.15`) | N/A (Executes OMS order count) | **Uncapped** (`equity * 0.15`) |
| **Half-Kelly Sizing** | `min(0.15, half_kelly_frac)` | **Missing** (flat 0.15) | N/A | **Missing** (flat 0.15) |
| **Price Bounds** | 5¢ to 78¢ | 20¢ to 78¢ | Validates level prices | 20¢ to 78¢ |
| **Take Profit** | +25% ROI (0¢ slip) | +25% ROI via REST limit | Limit order execution | +25% ROI (0¢ slip) |
| **Stop Loss** | -25% ROI **AND** Prob ≤ 35% | -25% ROI **AND** Prob ≤ 35% | Limit-Chasing Stop Loss (`floor=15¢`) | -25% ROI **AND** Prob ≤ 35% |
| **Re-entry Lockout** | None (up to 3 trades/candle) | **Enforced** if last was loss | N/A | Re-entry allowed if profit taken |
| **Order Execution** | Simulated immediate fill | Kalshi Demo REST API (`/orders`) | V2 mapping (`bid`/`ask` on YES) | Simulated immediate fill |
| **Expiry Close Method** | `record_trade_close()` | **CRASHES** on `rationale` kwarg | Reconciler syncs balance | `record_trade_close()` |

---

## 5. Critical Discrepancies & Recommendations

### Discrepancy 1: Showstopper Crash Bug in `run_live_bot.py` (Line 412)
- **Problem**: `run_live_bot.py` passes `rationale=...` into `self.db.record_trade_close()`, which only accepts 5 positional arguments (`trade_id, exit_price, exit_reason, pnl_dollars, pnl_pct`).
- **Proof**:
  ```python
  TypeError: AuditDatabase.record_trade_close() got an unexpected keyword argument 'rationale'
  ```
- **Consequence**: The trade never closes in state (`self.active_position = None` is bypassed). The bot is currently frozen in an infinite crash loop on expired Trade #2.
- **Fix**: Remove `rationale=...` or update `AuditDatabase.record_trade_close` in `bot_database.py` to accept `rationale: Optional[str] = None`.

### Discrepancy 2: Missing Half-Kelly Risk Throttling in `run_live_bot.py`
- **Problem**: `run_live_bot.py` uses `budget = min(self.cash, equity * self.max_allocation_pct)`, ignoring `half_kelly_frac`.
- **Fix**: Restore the winning formula:
  ```python
  alloc_pct = min(self.max_allocation_pct, half_kelly_frac)
  budget = min(self.cash, equity * alloc_pct)
  ```

### Discrepancy 3: Re-entry Lockout Strictness
- **Problem**: In the winning run, after taking profit on a trade that later expired out-of-the-money, the bot was not permanently locked out.
- **Fix**: Ensure lockout only triggers if the *current candle's previous trade* took a loss, allowing fresh entries when new candles form.
