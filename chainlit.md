# 🧠 Kalshi 4-Coin Autonomous Brain & Agent Dashboard

Welcome to the **Kalshi Multi-Asset Quantitative Trading Brain** (BTC, ETH, SOL, XRP).

Unlike standard dashboards that re-run scripts from top to bottom and cause UI flickering, this dashboard is built on **Chainlit** for real-time streaming, interactive agent dialog, and native **Chain of Thought (CoT)** inspection.

---

### 🔍 Features
- **Chain of Thought Visualization**: Native nested dropdowns displaying:
  1. `fetch_hyperliquid_spot`: Real-time spot mids & 15m candle opens from Hyperliquid.
  2. `fetch_kalshi_production_orderbook`: Institutional bid/ask depth from Kalshi Production (`api.elections.kalshi.com`).
  3. `drift_diffusion_model`: Quantitative $Z$-score and Black-Scholes Gaussian probabilities $P(\text{YES}) / P(\text{NO})$.
  4. `evaluate_kalshi_taker_fee_and_edge`: Quadratic fee curve and net $+4.0\%$ EV edge calculations.
  5. `portfolio_risk_and_allocation`: Performance-based capital weighting ($100 baseline) and 20% trade risk caps.
- **Asynchronous Execution**: Runs non-blocking alongside your high-frequency trading loop.
- **Zero-Flicker WebSockets**: Smooth state updates and live trading alerts.

---

### ⚡ Quick Commands
Type these commands directly into the chat prompt below:
- `/scan` — Execute a real-time live Chain of Thought evaluation across all 4 coins.
- `/status` — View current portfolio equity, available cash, dynamic weights, and active positions.
- `/trades` — Audit recent trade fills, exit prices, and PnL metrics.
- `/risk` — Review Half-Kelly sizing, 20% equity risk caps, and the 60s expiration safety cutoff.
