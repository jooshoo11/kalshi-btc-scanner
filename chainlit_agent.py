"""
chainlit_agent.py — Kalshi 4-Coin Autonomous Brain & Agent Dashboard.

Powered by Chainlit:
- Purpose-built for LLM and Quantitative Agents with ZERO-flicker streaming.
- Native "Chain of Thought" dropdown visualizations (`cl.Step`) exposing:
  1. fetch_hyperliquid_spot (Spot, 15m Candle Open, Delta)
  2. fetch_kalshi_production_orderbook (Tickers, Top-of-Book Bids/Asks, Institutional Spreads)
  3. drift_diffusion_model (Z-score, Normal CDF Model Probabilities)
  4. evaluate_kalshi_fee_and_edge (Exact Kalshi Taker Fee & Net EV Edge %)
  5. portfolio_allocation_decision (Dynamic Weight Sizing, 20% Equity Cap, Risk Gates)
- Interactive Chat Interface:
  - Quick action starters (/scan, /status, /trades, /risk)
  - Quantitative Q&A on pricing models, fee curves, and risk mitigation
"""

import os
import sys
import time
import math
import json
import sqlite3
import asyncio
from typing import Dict, Any, List, Optional
import httpx
import chainlit as cl

from core.multi_coin_trader import MultiCoinPaperEngine, calculate_kalshi_taker_fee
from live_executor import KALSHI_PROD_REST_URL

# Coin Configuration parameters
COIN_CONFIGS = {
    "BTC": {"series": "KXBTC15M", "vol_15m": 95.0, "icon": "₿", "name": "Bitcoin"},
    "ETH": {"series": "KXETH15M", "vol_15m": 6.5, "icon": "Ξ", "name": "Ethereum"},
    "SOL": {"series": "KXSOL15M", "vol_15m": 1.2, "icon": "◎", "name": "Solana"},
    "XRP": {"series": "KXXRP15M", "vol_15m": 0.025, "icon": "✕", "name": "Ripple XRP"}
}

def normal_cdf(x: float) -> float:
    """Standard normal cumulative distribution function."""
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def get_engine() -> MultiCoinPaperEngine:
    """Get or instantiate the multi-coin engine reading from multi_coin_paper.db."""
    return MultiCoinPaperEngine(initial_balance=100.0, db_path="multi_coin_paper.db")


# ----------------------------------------------------------------------
# Starters: Quick action buttons displayed on session start
# ----------------------------------------------------------------------
@cl.set_starters
async def set_starters():
    return [
        cl.Starter(
            label="Live 4-Coin Brain Scan",
            message="/scan",
            icon="/public/brain.png" if os.path.exists("public/brain.png") else None,
        ),
        cl.Starter(
            label="Portfolio & Capital Allocation",
            message="/status",
            icon="/public/wallet.png" if os.path.exists("public/wallet.png") else None,
        ),
        cl.Starter(
            label="Recent Trade Audit",
            message="/trades",
            icon="/public/history.png" if os.path.exists("public/history.png") else None,
        ),
        cl.Starter(
            label="Risk Limits & Model Rules",
            message="/risk",
            icon="/public/shield.png" if os.path.exists("public/shield.png") else None,
        ),
    ]


# ----------------------------------------------------------------------
# On Chat Start: Welcome banner & system introduction
# ----------------------------------------------------------------------
@cl.on_chat_start
async def on_chat_start():
    engine = get_engine()
    status = engine.get_status_dict()

    weights_md = " • ".join([f"**{c}**: {status['coin_weights'].get(c, 0.25)*100:.1f}% (${status['coin_equities'].get(c, 25.0):.2f})" for c in COIN_CONFIGS])

    welcome_text = f"""# 🧠 Kalshi 4-Coin Autonomous Brain & Agent Dashboard
**Multi-Asset Quantitative Scalper (BTC, ETH, SOL, XRP)**
*Real-time Shadow Paper Trading against Kalshi Institutional Production Order Books*

---

### 📊 Portfolio Baseline
- **Total Portfolio Equity**: **${status['total_equity']:.2f}** (Initial Baseline: **$100.00**)
- **Cumulative Net Profit**: **+${status['total_pnl']:.2f}** (**+{status['total_return_pct']:.1f}%**)
- **Total Closed Trades**: **{status['total_trades']}** (Overall Win Rate: **{status['overall_win_rate_pct']:.1f}%**)
- **Dynamic Performance Allocations**:
  {weights_md}

---

### ⚡ Available Commands
- Click **"Live 4-Coin Brain Scan"** or type `/scan` to trigger a live Chain-of-Thought inspection across all 4 coins.
- Type `/status` for real-time asset weights, sub-equities, and active positions.
- Type `/trades` to audit recent fills, exit timestamps, and PnL metrics.
- Type `/risk` to review Half-Kelly position sizing and the 60-second expiration safety gate.
- Ask any natural language question about the drift-diffusion formula, Kalshi taker fees, or trade decisions!
"""
    await cl.Message(content=welcome_text).send()


# ----------------------------------------------------------------------
# Core Chain-of-Thought Evaluator across all 4 Coins
# ----------------------------------------------------------------------
async def run_cot_scan() -> Dict[str, Any]:
    """
    Executes a step-by-step Chain of Thought evaluation across BTC, ETH, SOL, XRP:
    Renders native nested dropdowns in Chainlit UI showing exact tool calls,
    raw JSON payloads, formulas, and trade decisions.
    """
    engine = get_engine()
    now = time.time()
    now_ms = int(now * 1000)
    candle_15m_start = (now_ms // (15 * 60 * 1000)) * (15 * 60 * 1000)
    candle_15m_end = candle_15m_start + (15 * 60 * 1000)
    seconds_left = max(1.0, float((candle_15m_end - now_ms) / 1000.0))

    scan_results = {}

    async with httpx.AsyncClient(timeout=5.0) as client:
        # Step 0: Batch fetch spot mids from Hyperliquid
        hl_mids = {}
        try:
            r = await client.post("https://api.hyperliquid.xyz/info", json={"type": "allMids"})
            if r.status_code == 200:
                hl_mids = r.json()
        except Exception as e:
            pass

        for coin, cfg in COIN_CONFIGS.items():
            async with cl.Step(name=f"Brain Analysis: {cfg['icon']} {coin} 15-Minute Contract", type="run") as coin_step:
                # 1. Tool Call: fetch_hyperliquid_spot
                spot_price = float(hl_mids.get(coin, 0.0))
                candle_open = spot_price
                delta = 0.0

                async with cl.Step(name="tool: fetch_hyperliquid_spot", type="tool") as step_spot:
                    step_spot.input = {
                        "endpoint": "https://api.hyperliquid.xyz/info",
                        "request_type": "candleSnapshot",
                        "coin": coin,
                        "interval": "15m",
                        "candle_start_epoch_ms": candle_15m_start
                    }
                    try:
                        c_resp = await client.post("https://api.hyperliquid.xyz/info", json={
                            "type": "candleSnapshot",
                            "req": {
                                "coin": coin,
                                "interval": "15m",
                                "startTime": candle_15m_start,
                                "endTime": now_ms
                            }
                        })
                        if c_resp.status_code == 200:
                            candles = c_resp.json()
                            if candles:
                                candle_open = float(candles[-1].get("o", spot_price))
                    except Exception as err:
                        pass

                    delta = spot_price - candle_open
                    step_spot.output = json.dumps({
                        "coin": coin,
                        "spot_price": spot_price,
                        "candle_open_price": candle_open,
                        "delta": round(delta, 4),
                        "seconds_remaining_in_candle": int(seconds_left)
                    }, indent=2)

                # 2. Tool Call: fetch_kalshi_production_orderbook
                mkt_data = {}
                ticker = ""
                yes_ask = 0.0
                yes_bid = 0.0
                no_ask = 0.0
                no_bid = 0.0

                async with cl.Step(name="tool: fetch_kalshi_production_orderbook", type="tool") as step_ob:
                    step_ob.input = {
                        "endpoint": f"{KALSHI_PROD_REST_URL}/markets",
                        "series_ticker": cfg["series"],
                        "status": "open"
                    }
                    try:
                        m_resp = await client.get(f"{KALSHI_PROD_REST_URL}/markets?series_ticker={cfg['series']}&status=open")
                        if m_resp.status_code == 200:
                            mkts = m_resp.json().get("markets", [])
                            now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                            active = [m for m in mkts if (m.get("close_time") or "") >= now_iso]
                            if active:
                                mkt_data = active[0]
                            elif mkts:
                                mkt_data = mkts[0]
                    except Exception as err:
                        pass

                    ticker = mkt_data.get("ticker", "UNKNOWN")
                    yes_ask = float(mkt_data.get("yes_ask_dollars") or 0.0)
                    yes_bid = float(mkt_data.get("yes_bid_dollars") or 0.0)
                    no_ask = float(mkt_data.get("no_ask_dollars") or 0.0)
                    no_bid = float(mkt_data.get("no_bid_dollars") or 0.0)

                    step_ob.output = json.dumps({
                        "ticker": ticker,
                        "close_time": mkt_data.get("close_time"),
                        "best_yes_ask": f"${yes_ask:.2f} ({int(yes_ask*100)}¢)",
                        "best_yes_bid": f"${yes_bid:.2f} ({int(yes_bid*100)}¢)",
                        "best_no_ask": f"${no_ask:.2f} ({int(no_ask*100)}¢)",
                        "best_no_bid": f"${no_bid:.2f} ({int(no_bid*100)}¢)",
                        "spread_cents": int(round((yes_ask - yes_bid) * 100)) if (yes_ask and yes_bid) else 0
                    }, indent=2)

                # 3. Thought Process: Drift-Diffusion Quantitative Engine
                sigma = cfg["vol_15m"] * math.sqrt(seconds_left / 900.0)
                z = delta / max(0.0001, sigma)
                prob_yes = normal_cdf(z)
                prob_no = 1.0 - prob_yes
                prob_yes_pct = prob_yes * 100.0
                prob_no_pct = prob_no * 100.0

                async with cl.Step(name="thought: drift_diffusion_quant_engine", type="thought") as step_model:
                    step_model.input = {
                        "formula": "Z = (Spot - CandleOpen) / (Vol15m * sqrt(TimeRemaining / 900s))",
                        "variables": {
                            "delta": round(delta, 4),
                            "baseline_vol_15m": cfg["vol_15m"],
                            "time_decay_factor": f"sqrt({seconds_left:.1f} / 900) = {math.sqrt(seconds_left / 900.0):.4f}",
                            "dynamic_sigma": round(sigma, 4)
                        }
                    }
                    step_model.output = (
                        f"📊 Z-Score = {z:+.4f}\n"
                        f"✓ Theoretical Prob YES: {prob_yes_pct:.2f}%\n"
                        f"✓ Theoretical Prob NO:  {prob_no_pct:.2f}%\n"
                        f"Target Conviction Gate: >= 60.0%"
                    )

                # 4. Tool Call: evaluate_kalshi_taker_fee_and_edge
                fee_y = calculate_kalshi_taker_fee(1, yes_ask)
                fee_n = calculate_kalshi_taker_fee(1, no_ask)
                edge_y = (prob_yes - (yes_ask + fee_y)) * 100.0 if yes_ask > 0 else -100.0
                edge_n = (prob_no - (no_ask + fee_n)) * 100.0 if no_ask > 0 else -100.0

                async with cl.Step(name="tool: evaluate_kalshi_taker_fee_and_edge", type="tool") as step_edge:
                    step_edge.input = {
                        "kalshi_quadratic_fee_formula": "ceil(0.07 * contracts * P * (1 - P) * 100) / 100",
                        "market_prices": {"yes_ask": yes_ask, "no_ask": no_ask},
                        "model_probabilities": {"P_yes": round(prob_yes, 4), "P_no": round(prob_no, 4)}
                    }
                    step_edge.output = json.dumps({
                        "yes_contract": {
                            "ask_price": f"{int(yes_ask*100)}¢",
                            "taker_fee": f"${fee_y:.4f}",
                            "breakeven_probability": f"{(yes_ask + fee_y)*100:.2f}%",
                            "net_edge_pct": f"{edge_y:+.2f}%"
                        },
                        "no_contract": {
                            "ask_price": f"{int(no_ask*100)}¢",
                            "taker_fee": f"${fee_n:.4f}",
                            "breakeven_probability": f"{(no_ask + fee_n)*100:.2f}%",
                            "net_edge_pct": f"{edge_n:+.2f}%"
                        },
                        "min_required_edge": "+4.00%"
                    }, indent=2)

                # 5. Thought Process: Portfolio Risk & Allocation Decision
                coin_weight = engine.coin_weights.get(coin, 0.25)
                coin_equity = engine.coin_equities.get(coin, engine.total_equity * coin_weight)
                active_pos = engine.active_positions.get(coin)

                decision = "HOLD"
                decision_reason = ""
                signal = ""
                target_side = ""
                target_ask = 0.0
                edge_val = 0.0
                prob_val = 50.0

                if active_pos:
                    decision = "MONITORING_OPEN_POSITION"
                    decision_reason = f"Holding active {active_pos['side']} position ({active_pos['contracts']}x @ {int(active_pos['entry_price']*100)}¢). Monitoring +25% TP / -25% SL."
                elif seconds_left < 60:
                    decision = "HOLD (EXPIRATION_CUTOFF)"
                    decision_reason = f"Seconds remaining ({int(seconds_left)}s) < 60s cutoff. Late-candle trap protection active."
                elif prob_yes_pct >= 60.0 and edge_y >= 4.0 and 0.20 <= yes_ask <= 0.80:
                    decision = "EXECUTE_BUY_YES"
                    signal = "BUY YES"
                    target_side = "YES"
                    target_ask = yes_ask
                    edge_val = edge_y
                    prob_val = prob_yes_pct
                    decision_reason = f"Conviction {prob_yes_pct:.1f}% >= 60% AND Edge {edge_y:+.1f}% >= +4.0%. Fulfills institutional criteria."
                elif prob_no_pct >= 60.0 and edge_n >= 4.0 and 0.20 <= no_ask <= 0.80:
                    decision = "EXECUTE_BUY_NO"
                    signal = "BUY NO"
                    target_side = "NO"
                    target_ask = no_ask
                    edge_val = edge_n
                    prob_val = prob_no_pct
                    decision_reason = f"Conviction {prob_no_pct:.1f}% >= 60% AND Edge {edge_n:+.1f}% >= +4.0%. Fulfills institutional criteria."
                else:
                    reasons = []
                    if max(prob_yes_pct, prob_no_pct) < 60.0:
                        reasons.append(f"Conviction ({max(prob_yes_pct, prob_no_pct):.1f}%) < 60.0% gate")
                    if max(edge_y, edge_n) < 4.0:
                        reasons.append(f"Net Edge ({max(edge_y, edge_n):+.1f}%) < +4.0% gate")
                    decision = "HOLD (CRITERIA_UNMET)"
                    decision_reason = " • ".join(reasons)

                async with cl.Step(name="thought: portfolio_risk_and_allocation", type="thought") as step_dec:
                    step_dec.input = {
                        "allocated_sub_equity": f"${coin_equity:.2f} ({coin_weight*100:.1f}%)",
                        "max_trade_allocation": f"${coin_equity * 0.20:.2f} (20% cap)",
                        "min_seconds_left": "60 seconds",
                        "take_profit_target": "+25.0% ROI",
                        "stop_loss_target": "-25.0% ROI"
                    }
                    step_dec.output = f"🎯 Action Decision: {decision}\nRationale: {decision_reason}"

                scan_results[coin] = {
                    "coin": coin,
                    "ticker": ticker,
                    "spot": spot_price,
                    "delta": delta,
                    "prob_yes": prob_yes_pct,
                    "prob_no": prob_no_pct,
                    "yes_ask": yes_ask,
                    "no_ask": no_ask,
                    "edge_y": edge_y,
                    "edge_n": edge_n,
                    "decision": decision,
                    "decision_reason": decision_reason,
                    "signal": signal,
                    "equity": coin_equity,
                    "weight": coin_weight
                }

    return scan_results


# ----------------------------------------------------------------------
# Message Router: Handles /scan, /status, /trades, /risk, and Chat Q&A
# ----------------------------------------------------------------------
@cl.on_message
async def on_message(message: cl.Message):
    cmd = message.content.strip().lower()
    engine = get_engine()

    # COMMAND 1: /scan or "scan"
    if cmd in ("/scan", "scan", "run scan", "analyze", "evaluate"):
        status_msg = await cl.Message(content="🔍 Initiating real-time 4-coin quantitative Chain of Thought scan...").send()

        results = await run_cot_scan()

        rows = []
        for coin, data in results.items():
            best_dir = "YES" if data["prob_yes"] >= data["prob_no"] else "NO"
            best_prob = max(data["prob_yes"], data["prob_no"])
            best_edge = data["edge_y"] if best_dir == "YES" else data["edge_n"]
            ask_val = data["yes_ask"] if best_dir == "YES" else data["no_ask"]
            status_badge = "🟢 BUY" if "EXECUTE" in data["decision"] else ("🟡 MONITOR" if "MONITOR" in data["decision"] else "⚪ HOLD")

            rows.append(
                f"| **{coin}** | `{data['ticker']}` | ${data['spot']:,.2f} | `{data['delta']:+.2f}` | "
                f"**{best_prob:.1f}%** ({best_dir}) | {int(ask_val*100)}¢ | **{best_edge:+.1f}%** | {status_badge} |"
            )

        table_md = "\n".join(rows)

        summary_md = f"""### 🎯 4-Coin Live Scan Summary
*Evaluated against Kalshi Production Order Books (`api.elections.kalshi.com`)*

| Asset | Contract Ticker | Spot Price | 15m Delta | Conviction | Ask | Net Edge | Action |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
{table_md}

---
💡 *Click on any nested **"Brain Analysis"** dropdown above to inspect the raw tool calls, Hyperliquid deltas, and drift-diffusion calculations.*
"""
        await cl.Message(content=summary_md).send()
        return

    # COMMAND 2: /status or "status"
    if cmd in ("/status", "status", "portfolio", "balance", "equity"):
        status = engine.get_status_dict()
        stats = status.get("coin_stats", {})

        stat_rows = []
        for c in ("BTC", "ETH", "SOL", "XRP"):
            s = stats.get(c, {})
            w = status["coin_weights"].get(c, 0.25) * 100.0
            eq = status["coin_equities"].get(c, 25.0)
            trades = s.get("total_trades", 0)
            wr = s.get("win_rate_pct", 0.0)
            pnl = s.get("total_pnl", 0.0)
            pos = status["active_positions"].get(c)
            pos_str = f"**{pos['side']}** {pos['contracts']}x @ {int(pos['entry_price']*100)}¢" if pos else "None (Cash)"

            stat_rows.append(
                f"| **{c}** | {w:.1f}% | **${eq:.2f}** | {trades} | **{wr:.1f}%** | {pnl:+.2f} | {pos_str} |"
            )

        stat_table = "\n".join(stat_rows)

        status_md = f"""### 📊 Multi-Coin Portfolio & Performance State
- **Total Portfolio Equity**: **${status['total_equity']:.2f}**
- **Available Cash**: **${status['total_cash']:.2f}**
- **Cumulative PnL**: **+${status['total_pnl']:.2f}** (**+{status['total_return_pct']:.1f}%**)
- **Total Closed Trades**: **{status['total_trades']}** (Overall Win Rate: **{status['overall_win_rate_pct']:.1f}%**)

#### Asset Allocations & Sub-Equities
| Coin | Dynamic Weight | Sub-Equity | Trades | Win Rate | Net PnL | Active Position |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
{stat_table}
"""
        await cl.Message(content=status_md).send()
        return

    # COMMAND 3: /trades or "trades"
    if cmd in ("/trades", "trades", "history", "recent"):
        recent = engine.db.get_recent_trades(limit=10)
        if not recent:
            await cl.Message(content="No trades recorded in database yet.").send()
            return

        trade_rows = []
        for t in recent:
            exit_p = t.get("exit_price")
            exit_str = f"{int(exit_p*100)}¢" if exit_p is not None else "HOLD"
            pnl = t.get("pnl_dollars", 0.0)
            pnl_badge = f"🟢 +${pnl:.2f}" if pnl > 0 else (f"🔴 -${abs(pnl):.2f}" if pnl < 0 else "$0.00")
            ts_str = time.strftime("%H:%M:%S", time.localtime(t.get("timestamp", time.time())))

            trade_rows.append(
                f"| #{t.get('id')} | `{ts_str}` | **{t.get('coin')}** | `{t.get('ticker')}` | "
                f"{t.get('side')} | {t.get('contracts')}x @ {int(t.get('entry_price',0)*100)}¢ | "
                f"{exit_str} | {t.get('exit_reason')} | {pnl_badge} |"
            )

        trades_table = "\n".join(trade_rows)
        report_md = f"""### 📜 Recent 10 Multi-Coin Paper Fills
| # | Time | Coin | Contract Ticker | Side | Size @ Entry | Exit | Reason | Net PnL |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
{trades_table}
"""
        await cl.Message(content=report_md).send()
        return

    # COMMAND 4: /risk or "risk"
    if cmd in ("/risk", "risk", "rules", "gates"):
        risk_md = r"""### 🛡️ Institutional Risk & Execution Gates
The 4-Coin Autonomous Brain strictly enforces quantitative rules to prevent drawdown:

1. **Conviction Gate**:
   - Model probability $\ge 60.0\%$ via 15-minute Drift-Diffusion Normal CDF.
2. **Net Edge Threshold**:
   - Post-fee EV edge $\ge +4.0\%$ after subtracting Kalshi taker fees.
3. **Execution Price Corridor**:
   - Entry asks strictly bound between **$0.20 and $0.80** (avoids catastrophic low-probability tail risks).
4. **Dynamic Portfolio Sizing**:
   - Trade size capped at strictly **20% of the coin's allocated sub-equity** using Half-Kelly sizing.
5. **Expiration Safety Cutoff**:
   - No entries allowed with **$< 60$ seconds remaining** in the 15-minute candle to prevent settlement rollover traps.
6. **Automated Exit Routing**:
   - **Take Profit**: $+25.0\%$ ROI.
   - **Stop Loss**: $-25.0\%$ ROI (dispatches complete exit with limit-chasing floor).
"""
        await cl.Message(content=risk_md).send()
        return

    # GENERAL LLM / QUANTITATIVE Q&A
    # If the user asks a natural language question
    async with cl.Step(name="quant_agent_reasoning", type="llm") as step_reason:
        step_reason.input = message.content

        # Contextual answering
        response_text = ""
        prompt_lower = message.content.lower()

        if "why" in prompt_lower and ("trade" in prompt_lower or "buy" in prompt_lower):
            results = await run_cot_scan()
            details = []
            for c, d in results.items():
                details.append(f"- **{c}**: {d['decision_reason']}")
            reasons_md = "\n".join(details)
            response_text = f"""### Current Market Reasoning:
I evaluated the live order books on Kalshi Production right now:

{reasons_md}

Because our risk rules require **>= 60% probability** AND **>= +4.0% edge**, capital is protected in cash until an institutional pricing discrepancy occurs."""

        elif "fee" in prompt_lower:
            response_text = r"""### Kalshi Taker Fee Schedule:
Kalshi calculates taker fees using a quadratic parabolic curve:
$$\text{Fee} = \lceil 0.07 \cdot \text{contracts} \cdot P \cdot (1 - P) \cdot 100 \rceil / 100$$
- At 50¢ ($P=0.50$), fee is maximum: ~$0.0175 per contract (~1.8¢).
- At 20¢ or 80¢ ($P=0.20$), fee drops to: ~$0.0112 per contract (~1.1¢).
Our prediction engine deducts this exact taker fee before verifying the $+4.0\%$ net edge."""

        elif "drift" in prompt_lower or "formula" in prompt_lower or "math" in prompt_lower:
            response_text = r"""### Drift-Diffusion Pricing Formula:
We model 15-minute binary options as a drift-diffusion process:
$$Z = \frac{\text{Spot} - \text{Candle Open}}{\sigma_{15m} \cdot \sqrt{T / 900}}$$
where:
- $\sigma_{15m}$ is calibrated volatility: BTC ($95.0), ETH ($6.5), SOL ($1.2), XRP ($0.025).
- $T$ is seconds remaining in the 15-minute candle.
- Theoretical probability is computed via the Gaussian CDF: $P(\text{YES}) = \Phi(Z)$.
When the market ask is cheaper than $P(\text{YES}) - \text{Fee} - 0.04$, we execute with an edge."""

        else:
            response_text = f"""I am the **Kalshi 4-Coin Autonomous Brain**. 
I monitor live orderbooks on Kalshi Production across **BTC, ETH, SOL, and XRP** with performance-based dynamic capital allocation ($100 initial baseline).

You can:
- Type `/scan` to view my full step-by-step Chain of Thought for all 4 coins.
- Type `/status` to view total equity, cash, and coin weights.
- Type `/trades` to audit recent fills and closed PnL.
- Type `/risk` to review position sizing and safety gates."""

        step_reason.output = response_text

    await cl.Message(content=response_text).send()
