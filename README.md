# Institutional Kalshi Bitcoin Scanner, Quantitative Prediction Engine & 15M Paper Trading Bot

An institutional-grade, high-frequency market scanner, quantitative prediction engine, and autonomous paper trading bot built specifically for **Kalshi Bitcoin Event Contracts**. It ingests multi-exchange spot reference rates, high-frequency candlesticks, and derivatives microstructure, prices hundreds of binary contracts across all strike ladders using Black-Scholes and Merton Jump-Diffusion Monte Carlo models, logs a full persistent SQLite audit trail, and provides a real-time Bloomberg-style analytics dashboard.

---

## Architecture Overview

```
                      ┌──────────────────────────────────────────────┐
                      │            Live Market Data Ingestion        │
                      │  • Hyperliquid (Sub-second Mid, Funding, OI) │
                      │  • Coinbase Spot (CF Benchmarks BRTI Base)   │
                      │  • Kraken (720+ 15m OHLC Candles)            │
                      └──────────────────────┬───────────────────────┘
                                             │
                                             ▼
                      ┌──────────────────────────────────────────────┐
                      │              btc_metrics.py                  │
                      │  • Realized Volatility: 15m, 1h, 24h, 7d     │
                      │  • Parkinson & Garman-Klass Volatility       │
                      │  • Momentum: RSI(14), MACD(12,26,9), ATR(14) │
                      │  • Bollinger Bands, EMAs (9,21), SMAs(50,200)│
                      └──────────────────────┬───────────────────────┘
                                             │
                                             ▼
                      ┌──────────────────────────────────────────────┐
                      │           prediction_engine.py               │
                      │  • Black-Scholes Binary Option Pricing       │
                      │  • 10,000-Path Jump-Diffusion Monte Carlo    │
                      │  • Analytical One-Touch Barrier Models       │
                      │  • 15-Minute Drift-Diffusion Scalp Model     │
                      │  • Monotonicity & Inversion Arb Detector     │
                      │  • Net EV, ROI %, and Half-Kelly Sizing      │
                      └──────────────────────┬───────────────────────┘
                                             │
                         ┌───────────────────┴───────────────────┐
                         ▼                                       ▼
       ┌───────────────────────────────────┐   ┌───────────────────────────────────┐
       │       kalshi_btc_scanner.py       │   │         paper_trader.py           │
       │ • Scans 600+ Active Kalshi Mkts   │   │ • $50.00 Initial Bankroll         │
       │ • KXBTC15M, KXBTCD, KXBTC,        │   │ • 15M Scalp Momentum Entries      │
       │   KXBTCMAXY, KXBTCMINY, etc.      │   │ • Dynamic Early Profit Securing   │
       │ • Identifies Mispricings & Edges  │   │ • Stop-Loss Adverse Cut & Re-entry│
       └─────────────────┬─────────────────┘   └─────────────────┬─────────────────┘
                         │                                       │
                         └───────────────────┬───────────────────┘
                                             │
                                             ▼
                      ┌──────────────────────────────────────────────┐
                      │        app.py (Starlette / Uvicorn)          │
                      │  • High-frequency SSE Stream (1.0s ticks)    │
                      │  • SQLite Audit Database (bot_database.py)   │
                      │  • Interactive Monte Carlo Simulation API    │
                      │  • Bloomberg Terminal UI (templates/index)   │
                      └──────────────────────────────────────────────┘
```

---

## Key Features

### 1. 15-Minute Autonomous Paper Trading Bot (`paper_trader.py`)
- **No API Keys Required**: Trades against live real-time Kalshi orderbook quotes in paper/simulation mode.
- **Starting Account Balance**: **$50.00**.
- **Dynamic Profit Securing (Take-Profit)**: Continuously monitors active trades; if market bid jumps by $+20\%$ to $+35\%$ (or $> +15¢$), sells early to lock in profits rather than risking a late reversal.
- **Adverse Drawdown Protection (Stop-Loss)**: If price action reverses and model probability drops below 35%, cuts loss early to preserve capital.
- **Mid-Candle Re-entry**: If profit is secured early in a 15-minute candle and edge reappears, bot has the ability to re-enter.
- **Settlement Resolution**: Contracts held to expiration resolve automatically ($1.00 for win, $0.00 for loss) with profits credited to paper cash.
- **Adjustable Parameters**: Configurable Take-Profit %, Stop-Loss %, Max Allocation % (e.g. 15% = ~$7.50 max risk per trade on $50), and Min Edge %.
- **Manual Control**: Dashboard includes 1-click "Emergency Close / Lock Profit" button.

### 2. Comprehensive Audit Database (`bot_database.py`)
- **Persistent SQLite Database (`kalshi_audit.db`)**:
  - `trades`: Full record of every opened and closed trade, side, entry price, size, exit price, exit reason (`TAKE_PROFIT`, `STOP_LOSS`, `SETTLED_WIN`, `SETTLED_LOSS`), PnL ($ and %), and exact mathematical rationale.
  - `metrics_snapshots`: Rolling historical snapshots of spot price, delta, 24h vol, RSI, funding rate, and model probabilities.
  - `portfolio_snapshots`: Time-series equity curve tracking cash, floating value, total equity, and win rate.
- **1-Click Audit Export**: Download full audit history as a structured CSV via `GET /api/audit/export`.

### 3. Multi-Exchange Bitcoin Data & Microstructure (`btc_metrics.py`)
- Real-time spot rates from Hyperliquid, Coinbase, and Kraken.
- Matches the CFTC **CF Benchmarks Bitcoin Real-Time Index (BRTI)** used for Kalshi settlement.
- Multi-horizon Realized Volatility: 15-Minute Dollar Vol, 1-Hour, 24-Hour, 7-Day, Parkinson High-Low Vol, Garman-Klass Vol.
- Technicals: RSI(14), MACD(12,26,9), Bollinger Bands, ATR(14), EMAs (9, 21), SMAs (50, 200), and 24h VWAP.
- Derivatives: Perpetuals Funding Rate (8h & annualized %) and Open Interest (BTC and USD).

### 4. Quantitative Prediction Models (`prediction_engine.py`)
- **Black-Scholes Binary Option Pricing**:
  $$d_2 = \frac{\ln(S_t / K) + (\mu - \frac{1}{2}\sigma^2)\tau}{\sigma \sqrt{\tau}}, \quad P(S_T \ge K) = \Phi(d_2)$$
- **10,000+ Path Merton Jump-Diffusion Monte Carlo Engine**:
  Vectorized simulation in NumPy accounting for discontinuous jump shocks.
- **Closed-Form One-Touch Barrier Options**:
  Analytical pricing for annual and monthly floors/ceilings (`KXBTCMAXY`, `KXBTCMINY`, `KXBTCMINMON`).
- **High-Frequency 15-Minute Scalp Model (`KXBTC15M`)**:
  Drift-diffusion model scaling volatility with remaining candle decay ($\sigma_\tau = \sigma_{15m} \sqrt{\tau / 900}$).
- **Monotonicity & Inversion Arbitrage Detector**: Audits strike ladders for risk-free pricing anomalies.
- **Kelly Criterion**: Calculates fee-adjusted Net EV and Half-Kelly staking.

---

## Quick Start

### Directory Location
The project is located at:
```bash
/home/jooshoo/kalshi_scanner
```

---

## Running the Dashboards & Bots

### 1. Chainlit Agent & Brain Dashboard (Port 8089)
Native Chain-of-Thought visualization, live market reasoning, tool calls, and interactive quant chat:
```bash
cd /home/jooshoo/kalshi_scanner
chainlit run chainlit_agent.py --port 8089
```
Access at:
- **Local**: `http://localhost:8089`
- **LAN**: `http://10.0.0.50:8089`

### 2. Live Web Dashboard & Multi-Coin Server (Port 8088)
Continuous Starlette / SSE streaming dashboard with Bloomberg-style terminal and multi-coin tabs:
```bash
cd /home/jooshoo/kalshi_scanner
python3 app.py
```
Access at:
- **Local**: `http://localhost:8088`
- **LAN**: `http://10.0.0.50:8088`

### 3. Dedicated Execution Bot (CLI)
```bash
cd /home/jooshoo/kalshi_scanner

# Run in Production Paper Trading Mode (Real orderbooks, $0 Risk):
python3 run_live_bot.py --mode=prod-paper

# Run in Kalshi Demo Mode (Real demo orders on exchange):
python3 run_live_bot.py --mode=live-demo
```

---

## Project Structure & File Guide

```
/home/jooshoo/kalshi_scanner/
├── core/                                # Shared Quantitative Infrastructure
│   ├── multi_coin_trader.py             # Multi-coin paper engine & dynamic rebalancer
│   ├── live_executor.py                 # OMS, L2 orderbook sync, bounded spread-crossing
│   ├── prediction_engine.py             # Drift-diffusion & Black-Scholes pricing
│   ├── portfolio_manager.py             # Global cash & multi-asset capital allocator
│   ├── kalshi_client.py                 # Async REST & WebSocket client
│   ├── bot_database.py                  # SQLite audit persistence
│   └── tests/                           # Integration test suite
│
├── coins/                               # Asset-Specific Workspaces
│   ├── btc/                             # KXBTC15M (Bitcoin)
│   ├── eth/                             # KXETH15M (Ethereum)
│   ├── sol/                             # KXSOL15M (Solana)
│   └── xrp/                             # KXXRP15M (Ripple)
│
├── Dashboards & Interfaces:
│   ├── chainlit_agent.py                # Chainlit Brain & Agent Dashboard (Port 8089)
│   ├── chainlit.md                      # Chainlit welcome configuration
│   ├── .chainlit/                       # Chainlit theme & CoT settings
│   ├── app.py                           # Starlette / Uvicorn Server & API (Port 8088)
│   └── templates/index.html             # Bloomberg-style Terminal UI
│
├── Execution Bots:
│   ├── run_live_bot.py                  # Standalone CLI execution bot (Demo or Prod-Paper)
│   ├── burn_in_multicoin.py             # Multi-coin historical calibration & burn-in script
│   └── paper_trader.py                  # Single-coin paper trading engine
│
└── SQLite Audit Databases:
    ├── multi_coin_paper.db              # 4-Coin Production Paper Trading ($100 baseline)
    ├── kalshi_live_demo.db              # Kalshi Live Demo Order Execution
    └── kalshi_audit.db                  # Single-Coin Paper Trading Audit Trail
```

---

## API Endpoints (Port 8088)

- `GET /api/scan`: Instantaneous snapshot of all metrics, strikes, and top +EV opportunities.
- `GET /api/stream`: High-frequency Server-Sent Events (SSE) streaming live ticks every second.
- `GET /api/multicoin/status`: 4-coin portfolio equity, dynamic weights, and active positions.
- `GET /api/multicoin/trades`: Recent multi-coin trade fills and PnL.
- `GET /api/audit/block_summary`: Real-time block audit data for automated monitoring.
- `GET /api/bot/status`: Single-coin bot balance, equity, and parameters.
- `GET /api/audit/export`: Complete CSV audit export.
