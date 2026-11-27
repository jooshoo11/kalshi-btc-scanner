#!/usr/bin/env python3
"""
scripts/tick_backtest_engine.py — Backtest deployed strategy on ACTUAL recorded tick data.

Loads real 1-second Kalshi L1/L2 book + spot ticks from data/ticks/<source>/<date>/<hour>.csv
(resumed by scripts/tick_recorder.py), resamples into 15-minute candle timelines, and
simulates the S2_TRIAL4_DEPLOYED_L2 and S5_REC_LEADLAG_ENSEMBLE strategies point-in-time
with exact Kalshi fees, L2 VWAP fills, 15% depth cap, and all trade filters.

Output: JSON report + equity curve CSV to data/backtest_validation/
"""

import csv
import glob
import json
import math
import os
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# Exact Kalshi taker fee (must match core/prediction_engine.py)
# ---------------------------------------------------------------------------
def calculate_kalshi_taker_fee(contracts: int, price: float) -> float:
    if contracts <= 0 or price <= 0.0 or price >= 1.0:
        return 0.0
    raw_cents = 0.07 * float(contracts) * float(price) * (1.0 - float(price)) * 100.0
    return math.ceil(raw_cents - 1e-9) / 100.0


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


# ---------------------------------------------------------------------------
# Tick loading from data/ticks/
# ---------------------------------------------------------------------------
@dataclass
class TickRow:
    recv_ts: float
    coin: str
    ticker: str
    close_time: str
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    last_price: float
    volume: float
    open_interest: float
    floor_strike: float
    yes_bid_size: float
    yes_ask_size: float


def load_kalshi_ticks(data_root: str, coins: Tuple[str, ...]) -> Dict[int, List[TickRow]]:
    """
    Load all kalshi tick CSVs, group by 1-second timestamp bucket per coin.
    Returns dict: coin -> { sec -> [TickRow] }
    """
    sec_map: Dict[str, Dict[int, List[TickRow]]] = {c: defaultdict(list) for c in coins}

    csv_files = sorted(glob.glob(os.path.join(data_root, "kalshi", "*", "*.csv")))
    for path in csv_files:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            reader = csv.DictReader(f)
            for row in reader:
                coin = row.get("coin", "").strip().upper()
                if coin not in coins:
                    continue
                try:
                    recv_ts = float(row["recv_ts"])
                    sec = int(recv_ts)
                except (ValueError, KeyError):
                    continue
                try:
                    tick = TickRow(
                        recv_ts=recv_ts,
                        coin=coin,
                        ticker=row.get("ticker", "").strip(),
                        close_time=row.get("close_time", "").strip(),
                        yes_bid=float(row.get("yes_bid") or 0),
                        yes_ask=float(row.get("yes_ask") or 0),
                        no_bid=float(row.get("no_bid") or 0),
                        no_ask=float(row.get("no_ask") or 0),
                        last_price=float(row.get("last_price") or 0),
                        volume=float(row.get("volume") or 0),
                        open_interest=float(row.get("open_interest") or 0),
                        floor_strike=float(row.get("floor_strike") or 0),
                        yes_bid_size=float(row.get("yes_bid_size") or 0),
                        yes_ask_size=float(row.get("yes_ask_size") or 0),
                    )
                except (ValueError, TypeError):
                    continue
                sec_map[coin][sec].append(tick)

    return sec_map


# ---------------------------------------------------------------------------
# Spot loading from coinbase/kraken/hyperliquid_trades
# ---------------------------------------------------------------------------
def load_spot_ticks(data_root: str, coins: Tuple[str, ...]) -> Dict[str, Dict[int, float]]:
    """
    Load spot trade ticks from coinbase + kraken + hyperliquid_trades.
    Returns coin -> { sec -> price } (last price per second).
    """
    spot_map: Dict[str, Dict[int, float]] = {c: {} for c in coins}

    for source in ("coinbase", "kraken", "hyperliquid_trades"):
        src_dir = os.path.join(data_root, source)
        if not os.path.isdir(src_dir):
            continue
        csv_files = sorted(glob.glob(os.path.join(src_dir, "*", "*.csv")))
        for path in csv_files:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    coin = row.get("coin", "").strip().upper()
                    if coin not in coins:
                        continue
                    try:
                        recv_ts = float(row["recv_ts"])
                        sec = int(recv_ts)
                        price = float(row.get("price") or 0)
                    except (ValueError, KeyError, TypeError):
                        continue
                    if price > 0:
                        spot_map[coin][sec] = price

    return spot_map


# ---------------------------------------------------------------------------
# Build 15-minute candle timelines
# ---------------------------------------------------------------------------
@dataclass
class Candle:
    coin: str
    ticker: str
    open_ts: int
    close_ts: int
    strike: float        # CF Benchmarks 60s mean at candle open
    settlement_price: float  # CF Benchmarks 60s arithmetic mean at close
    yes_won: bool
    # parallel arrays
    ts_sec: List[int] = field(default_factory=list)
    spot: List[float] = field(default_factory=list)
    yes_bid: List[float] = field(default_factory=list)
    yes_ask: List[float] = field(default_factory=list)
    yes_bid_sz: List[float] = field(default_factory=list)
    yes_ask_sz: List[float] = field(default_factory=list)


def build_candles(kalshi_sec_map: Dict[int, List[TickRow]],
                  spot_map: Dict[int, float],
                  coin: str) -> List[Candle]:
    """
    Group Kalshi ticks by 15-minute candle boundaries using close_time.
    Resample spot to 1s via forward-fill.
    """
    candles: List[Candle] = []
    candle_groups: Dict[int, List[TickRow]] = defaultdict(list)

    for sec, ticks in kalshi_sec_map.items():
        for t in ticks:
            if not t.close_time:
                continue
            try:
                close_dt = datetime.fromisoformat(t.close_time.replace("Z", "+00:00"))
                close_ts = int(close_dt.timestamp())
            except ValueError:
                continue
            open_ts = close_ts - 900
            candle_groups[open_ts].append(t)

    all_secs = sorted(kalshi_sec_map.keys())
    if not all_secs:
        return candles

    # forward-fill spot over all seconds
    ff_spot: Dict[int, float] = {}
    last_px = 0.0
    for s in all_secs:
        if s in spot_map:
            last_px = spot_map[s]
        if last_px > 0:
            ff_spot[s] = last_px

    for open_ts in sorted(candle_groups.keys()):
        ticks = candle_groups[open_ts]
        close_ts = open_ts + 900
        ticker = ticks[0].ticker

        # settlement: CF Benchmarks 60s arithmetic mean at close
        close_window = [ff_spot[s] for s in range(close_ts - 60, close_ts + 1) if s in ff_spot]
        if len(close_window) < 10:
            # fallback: last observed seconds in candle
            close_window = [ff_spot[s] for s in sorted(kalshi_sec_map.keys())
                            if open_ts <= s < close_ts and s in ff_spot][-60:]
        if not close_window:
            continue
        settlement_price = sum(close_window) / len(close_window)

        # strike: floor_strike from ticks (CF Benchmarks at open)
        floor_strikes = [t.floor_strike for t in ticks if t.floor_strike > 0]
        strike = floor_strikes[0] if floor_strikes else ff_spot.get(open_ts, settlement_price)

        yes_won = settlement_price >= strike

        # build per-second arrays from ticks present in this candle
        candle_secs = sorted({int(round(t.recv_ts)) for t in ticks})
        ct = Candle(
            coin=coin,
            ticker=ticker,
            open_ts=open_ts,
            close_ts=close_ts,
            strike=round(strike, 4),
            settlement_price=round(settlement_price, 4),
            yes_won=yes_won,
        )
        for s in candle_secs:
            sec_ticks = [t for t in ticks if int(round(t.recv_ts)) == s]
            if not sec_ticks:
                continue
            t = sec_ticks[-1]  # last tick in second
            ct.ts_sec.append(s)
            ct.spot.append(ff_spot.get(s, t.last_price))
            ct.yes_bid.append(t.yes_bid)
            ct.yes_ask.append(t.yes_ask)
            ct.yes_bid_sz.append(t.yes_bid_size)
            ct.yes_ask_sz.append(t.yes_ask_size)

        if len(ct.ts_sec) >= 10:
            candles.append(ct)

    candles.sort(key=lambda c: c.open_ts)
    return candles


# ---------------------------------------------------------------------------
# L2 VWAP fill simulator (matches streaming_backtest logic)
# ---------------------------------------------------------------------------
def simulate_l2_vwap_fill(top_price: float, top_size: float, contracts: int,
                          enforce_depth_cap: bool = True, depth_cap_pct: float = 0.15,
                          ladder_decay: float = 0.85) -> Tuple[int, float, float]:
    if contracts <= 0 or top_price <= 0.0 or top_price >= 1.0:
        return 0, top_price, 0.0
    top_qty = max(1.0, float(top_size or 100.0))
    est_5lvl_depth = top_qty * 2.6
    target_c = contracts
    if enforce_depth_cap:
        max_safe = max(1, int(est_5lvl_depth * depth_cap_pct))
        target_c = min(contracts, max_safe)
    rem = target_c
    acc_qty = 0
    acc_cost = 0.0
    lvl_qty = top_qty
    for lvl in range(6):
        px = min(0.99, max(0.01, top_price + 0.01 * lvl))
        avail = max(1, int(round(lvl_qty)))
        fill = min(rem, avail)
        acc_cost += fill * px
        acc_qty += fill
        rem -= fill
        if rem <= 0:
            break
        lvl_qty *= ladder_decay
    if acc_qty <= 0:
        return 0, top_price, est_5lvl_depth
    return acc_qty, round(acc_cost / acc_qty, 4), est_5lvl_depth


# ---------------------------------------------------------------------------
# Strategy configs (mirrors scripts/streaming_backtest.py StrategyConfig)
# ---------------------------------------------------------------------------
@dataclass
class StrategyConfig:
    name: str
    max_entry_price: float = 0.35
    min_entry_price: float = 0.20
    certainty_arb_cap: float = 0.78
    min_edge_pct: float = 4.0
    min_prob_btc: float = 65.0
    min_prob_eth: float = 60.0
    min_sec_btc: int = 360
    min_sec_eth: int = 240
    max_sec_entry: int = 500
    take_profit_roi_pct: float = 25.0
    stop_loss_roi_pct: float = 25.0
    stop_loss_hard_roi_pct: float = 35.0
    stop_grace_seconds: int = 30
    max_hold_seconds: int = 120
    maker_take_profit: bool = True
    use_l2_vwap_fills: bool = True
    enforce_depth_cap: bool = True
    max_spread_dollars: float = 0.10
    max_trades_per_candle: int = 3
    signal_mode: str = "raw_hl_vs_cf_strike"
    min_cf_edge_dollars: float = 0.08
    min_dir_z: float = 0.0
    min_vel10_bps: float = 0.0
    min_leadlag_imp_bps: float = -99.0
    structural_stop_z: Optional[float] = -1.8
    structural_stop_roi_pct: float = 45.0


def get_strategy_suite() -> List[StrategyConfig]:
    return [
        StrategyConfig(
            name="S2_TRIAL4_DEPLOYED_L2",
            max_entry_price=0.35,
            certainty_arb_cap=0.78,
            min_edge_pct=4.0,
            min_prob_btc=65.0,
            min_prob_eth=60.0,
            min_sec_btc=360,
            min_sec_eth=240,
            max_sec_entry=500,
            take_profit_roi_pct=25.0,
            stop_loss_roi_pct=25.0,
            stop_loss_hard_roi_pct=35.0,
            stop_grace_seconds=30,
            max_hold_seconds=120,
            maker_take_profit=True,
            use_l2_vwap_fills=True,
            enforce_depth_cap=True,
            max_spread_dollars=0.10,
            max_trades_per_candle=3,
            signal_mode="raw_hl_vs_cf_strike",
        ),
        StrategyConfig(
            name="S5_REC_LEADLAG_ENSEMBLE",
            max_entry_price=0.48,
            certainty_arb_cap=0.78,
            min_edge_pct=4.0,
            min_prob_btc=65.0,
            min_prob_eth=60.0,
            min_sec_btc=90,
            min_sec_eth=90,
            max_sec_entry=660,
            take_profit_roi_pct=55.0,
            stop_loss_roi_pct=25.0,
            stop_loss_hard_roi_pct=35.0,
            stop_grace_seconds=30,
            max_hold_seconds=120,
            maker_take_profit=True,
            use_l2_vwap_fills=True,
            enforce_depth_cap=True,
            max_spread_dollars=0.05,
            max_trades_per_candle=1,
            signal_mode="cf_disloc_structural",
            min_cf_edge_dollars=0.09,
            min_dir_z=0.05,
            min_vel10_bps=0.0,
            min_leadlag_imp_bps=-0.50,
            structural_stop_z=-1.8,
            structural_stop_roi_pct=45.0,
        ),
    ]


# ---------------------------------------------------------------------------
# Per-candle simulation (point-in-time, mirrors streaming_backtest logic)
# ---------------------------------------------------------------------------
def simulate_candle(candle: Candle, cfg: StrategyConfig, equity: float,
                    spot_lead_map: Dict[int, float],
                    coin_weight: float = 0.50,
                    max_trade_alloc_pct: float = 20.0,
                    base_contracts: int = 40) -> Tuple[float, List[Dict[str, Any]]]:
    n = len(candle.ts_sec)
    if n < 10:
        return equity, []

    coin = candle.coin
    strike = candle.strike
    min_sec = cfg.min_sec_btc if coin == "BTC" else cfg.min_sec_eth
    min_prob = cfg.min_prob_btc if coin == "BTC" else cfg.min_prob_eth

    trades: List[Dict[str, Any]] = []
    pos: Optional[Dict[str, Any]] = None
    stopped_out_in_candle = False
    trades_in_candle = 0
    last_exit_ts = 0

    spot_win = deque(maxlen=30)
    flow_win = deque(maxlen=20)
    cf_final60_samples: List[float] = []
    basis_ema: Optional[float] = None

    for i in range(n):
        ts = candle.ts_sec[i]
        seconds_left = max(0, candle.close_ts - ts)
        spot = candle.spot[i]
        lead = spot_lead_map.get(ts, spot)
        y_bid = candle.yes_bid[i]
        y_ask = candle.yes_ask[i]
        y_bid_sz = candle.yes_bid_sz[i]
        y_ask_sz = candle.yes_ask_sz[i]
        n_bid = round(1.0 - y_ask, 4)
        n_ask = round(1.0 - y_bid, 4)
        n_bid_sz = y_ask_sz
        n_ask_sz = y_bid_sz

        spot_win.append((ts, spot))
        cf_final60_samples.append(spot)

        # Rolling basis EMA (de-biases perp premium over CF Benchmarks)
        inst_basis = lead - spot
        if basis_ema is None:
            basis_ema = inst_basis
        else:
            basis_ema = 0.95 * basis_ema + 0.05 * inst_basis
        lead_impulse_dollar = inst_basis - basis_ema
        leadlag_bps = (lead_impulse_dollar / max(1e-6, spot)) * 10000.0

        # Dynamic realized vol
        eff_vol = strike * 0.0015  # base 15m vol proxy
        if len(spot_win) >= 10:
            diffs = [abs(spot_win[j][1] - spot_win[j-1][1]) / math.sqrt(max(1.0, spot_win[j][0]-spot_win[j-1][0]))
                     for j in range(1, len(spot_win))]
            if diffs:
                realized_15m = (sum(diffs) / len(diffs)) * 30.0
                eff_vol = max(eff_vol * 0.6, min(eff_vol * 2.2, 0.5 * eff_vol + 0.5 * realized_15m))

        # OFI Z-score (proxy using flow = 0 since we don't have signed flow in kalshi ticks)
        ofi_z = 0.0

        vel_20s = spot - spot_win[0][1] if len(spot_win) >= 4 else 0.0
        vel_10s_bps = ((spot - candle.spot[max(0, i-10)]) / max(1e-6, spot)) * 10000.0
        sigma_base = eff_vol * math.sqrt(max(1.0, float(seconds_left)) / 900.0)
        sigma = eff_vol * math.sqrt(max(1.0, float(seconds_left)) / 900.0)
        z_cf = (spot - strike) / max(1e-4, sigma_base)
        py_cf = normal_cdf(z_cf)
        pn_cf = 1.0 - py_cf

        # Drift-diffusion probability by signal mode
        if cfg.signal_mode == "raw_hl_vs_cf_strike":
            delta = lead - strike
            z = delta / max(1e-4, sigma)
            ofi_bias = max(-0.35, min(0.35, 0.12 * ofi_z))
            prob_yes = normal_cdf(z + ofi_bias)
            prob_no = 1.0 - prob_yes
        else:
            delta = spot - strike
            z = z_cf
            prob_yes = py_cf
            prob_no = pn_cf

        prob_yes_pct = prob_yes * 100.0
        prob_no_pct = prob_no * 100.0

        # ---- 1. Manage active position ----
        if pos is not None:
            side = pos["side"]
            cur_bid = y_bid if side == "YES" else n_bid
            cur_bid_sz = y_bid_sz if side == "YES" else n_bid_sz
            cur_prob = prob_yes_pct if side == "YES" else prob_no_pct
            entry_p = pos["entry_price"]
            roi_pct = ((cur_bid - entry_p) / max(0.01, entry_p)) * 100.0
            pos["peak_roi_pct"] = max(pos.get("peak_roi_pct", roi_pct), roi_pct)
            hold_secs = ts - pos["entry_ts"]
            ref_spot_for_stop = lead if cfg.signal_mode == "raw_hl_vs_cf_strike" else spot
            is_spot_adverse = (ref_spot_for_stop < strike) if side == "YES" else (ref_spot_for_stop > strike)
            dir_z_cf = z_cf if side == "YES" else -z_cf

            exit_reason = None
            is_maker_exit = False
            exit_px = cur_bid

            # A. Expiration resolution
            if seconds_left <= 3 or i == n - 1:
                won = candle.yes_won if side == "YES" else (not candle.yes_won)
                exit_px = 1.0 if won else 0.0
                payout = pos["contracts"] * exit_px
                pnl = round(payout - (pos["cost"] + pos["entry_fee"]), 2)
                equity = round(equity + payout, 2)
                trades.append({**pos, "exit_ts": ts, "exit_price": exit_px,
                               "exit_fee": 0.0, "exit_reason": "SETTLED",
                               "pnl": pnl,
                               "roi_pct": round((pnl / max(0.01, pos["cost"]+pos["entry_fee"]))*100.0, 1),
                               "equity_after": equity})
                pos = None
                last_exit_ts = ts
                continue

            # B. Take Profit (passive maker -> 0c fee)
            if roi_pct >= cfg.take_profit_roi_pct:
                exit_reason = "TAKE_PROFIT"
                is_maker_exit = cfg.maker_take_profit

            # C. Stop loss / time exit by regime
            if exit_reason is None:
                if cfg.signal_mode == "raw_hl_vs_cf_strike":
                    is_hard_stop = roi_pct <= -cfg.stop_loss_hard_roi_pct
                    if hold_secs < cfg.stop_grace_seconds and not is_spot_adverse and not is_hard_stop:
                        pass
                    else:
                        is_dual = (roi_pct <= -cfg.stop_loss_roi_pct) and (cur_prob <= 42.0 or is_spot_adverse)
                        if is_dual or is_hard_stop:
                            exit_reason = "STOP_LOSS"
                            stopped_out_in_candle = True
                    if exit_reason is None and hold_secs >= cfg.max_hold_seconds:
                        if not is_spot_adverse and cur_prob >= 55.0:
                            pass
                        else:
                            exit_reason = "TIME_EXIT"
                else:
                    if (cfg.structural_stop_z is not None and hold_secs >= 60
                            and dir_z_cf <= cfg.structural_stop_z
                            and roi_pct <= -cfg.structural_stop_roi_pct):
                        exit_reason = "STRUCT_STOP"
                        stopped_out_in_candle = True

            if exit_reason is not None:
                if cfg.use_l2_vwap_fills and not is_maker_exit:
                    _, swept_px, _ = simulate_l2_vwap_fill(
                        top_price=max(0.01, 1.0 - cur_bid),
                        top_size=cur_bid_sz,
                        contracts=pos["contracts"],
                        enforce_depth_cap=False,
                    )
                    exit_px = round(max(0.01, 1.0 - swept_px), 4)
                else:
                    exit_px = cur_bid
                exit_fee = 0.0 if is_maker_exit else calculate_kalshi_taker_fee(pos["contracts"], exit_px)
                payout = pos["contracts"] * exit_px - exit_fee
                pnl = round(payout - (pos["cost"] + pos["entry_fee"]), 2)
                equity = round(equity + payout, 2)
                trades.append({**pos, "exit_ts": ts, "exit_price": exit_px,
                               "exit_fee": exit_fee, "exit_reason": exit_reason,
                               "pnl": pnl,
                               "roi_pct": round((pnl / max(0.01, pos["cost"]+pos["entry_fee"]))*100.0, 1),
                               "equity_after": equity})
                pos = None
                last_exit_ts = ts
            continue

        # ---- 2. Evaluate new entry ----
        if stopped_out_in_candle or trades_in_candle >= cfg.max_trades_per_candle or (ts - last_exit_ts) < 4:
            continue

        spread_y = y_ask - y_bid
        spread_n = n_ask - n_bid
        is_liquid_y = (y_bid >= 0.05) and (spread_y <= cfg.max_spread_dollars) and (spread_y / max(0.01, y_ask) <= 0.35)
        is_liquid_n = (n_bid >= 0.05) and (spread_n <= cfg.max_spread_dollars) and (spread_n / max(0.01, n_ask) <= 0.35)

        fee_y = calculate_kalshi_taker_fee(base_contracts, y_ask) / base_contracts if 0.0 < y_ask < 1.0 else 0.0
        fee_n = calculate_kalshi_taker_fee(base_contracts, n_ask) / base_contracts if 0.0 < n_ask < 1.0 else 0.0
        edge_y = (prob_yes - (y_ask + fee_y)) * 100.0 if y_ask > 0 else -100.0
        edge_n = (prob_no - (n_ask + fee_n)) * 100.0 if n_ask > 0 else -100.0

        adverse_vel_thresh = 18.0 if coin == "BTC" else 2.0
        target_side = None
        quote_ask = 0.0
        quote_sz = 0.0
        model_prob_entry = 0.0
        edge_entry = 0.0

        if min_sec <= seconds_left <= cfg.max_sec_entry:
            if cfg.signal_mode == "raw_hl_vs_cf_strike":
                if (is_liquid_y and cfg.min_entry_price <= y_ask <= cfg.max_entry_price
                        and prob_yes_pct >= min_prob and edge_y >= cfg.min_edge_pct
                        and vel_20s >= -adverse_vel_thresh):
                    target_side = "YES"; quote_ask = y_ask; quote_sz = y_ask_sz
                    model_prob_entry = prob_yes_pct; edge_entry = edge_y
                elif (is_liquid_n and cfg.min_entry_price <= n_ask <= cfg.max_entry_price
                        and prob_no_pct >= min_prob and edge_n >= cfg.min_edge_pct
                        and vel_20s <= adverse_vel_thresh):
                    target_side = "NO"; quote_ask = n_ask; quote_sz = n_ask_sz
                    model_prob_entry = prob_no_pct; edge_entry = edge_n
            else:
                if i < 15:
                    continue
                if (is_liquid_y and cfg.min_entry_price <= y_ask <= cfg.max_entry_price
                        and (py_cf - y_ask) >= cfg.min_cf_edge_dollars
                        and z_cf >= cfg.min_dir_z
                        and vel_10s_bps >= cfg.min_vel10_bps
                        and leadlag_bps >= cfg.min_leadlag_imp_bps):
                    target_side = "YES"; quote_ask = y_ask; quote_sz = y_ask_sz
                    model_prob_entry = py_cf * 100.0
                    edge_entry = (py_cf - (y_ask + fee_y)) * 100.0
                elif (is_liquid_n and cfg.min_entry_price <= n_ask <= cfg.max_entry_price
                        and (pn_cf - n_ask) >= cfg.min_cf_edge_dollars
                        and (-z_cf) >= cfg.min_dir_z
                        and (-vel_10s_bps) >= cfg.min_vel10_bps
                        and (-leadlag_bps) >= cfg.min_leadlag_imp_bps):
                    target_side = "NO"; quote_ask = n_ask; quote_sz = n_ask_sz
                    model_prob_entry = pn_cf * 100.0
                    edge_entry = (pn_cf - (n_ask + fee_n)) * 100.0

        if not target_side:
            continue

        # Position sizing: half-Kelly capped
        contract_cost = quote_ask + (1.0 / 100.0)  # 1c slippage
        b = (1.0 - contract_cost) / contract_cost
        p = prob_yes if target_side == "YES" else prob_no
        kelly_f = max(0.0, (p * (b + 1) - 1) / b) if b > 0 else 0.0
        half_kelly = min(kelly_f * 0.5, 0.08)
        budget = min(equity * (max_trade_alloc_pct / 100.0), equity * half_kelly)
        contracts = max(1, int(budget / contract_cost))
        contracts = min(contracts, base_contracts)

        entry_fee = calculate_kalshi_taker_fee(contracts, contract_cost)
        total_entry_cost = contracts * contract_cost + entry_fee
        if total_entry_cost > equity:
            continue

        pos = {
            "side": target_side,
            "entry_price": quote_ask,
            "contracts": contracts,
            "cost": total_entry_cost,
            "entry_fee": entry_fee,
            "entry_ts": ts,
            "strike": strike,
            "coin": coin,
        }
        equity -= total_entry_cost
        trades_in_candle += 1

    return equity, trades


# ---------------------------------------------------------------------------
# Risk metrics (lightweight, matches backtests/risk_metrics.py output)
# ---------------------------------------------------------------------------
def calculate_risk_metrics(equity_curve: List[float]) -> Dict[str, float]:
    if len(equity_curve) < 2:
        return {"total_return_pct": 0.0, "max_drawdown_pct": 0.0, "sharpe_ratio": 0.0}
    eq = [float(x) for x in equity_curve]
    initial = eq[0]
    final = eq[-1]
    total_return_pct = ((final - initial) / initial) * 100.0 if initial > 0 else 0.0

    returns = [(eq[i] - eq[i-1]) / max(0.01, eq[i-1]) for i in range(1, len(eq))]
    if not returns:
        return {"total_return_pct": total_return_pct, "max_drawdown_pct": 0.0, "sharpe_ratio": 0.0}

    mean_r = sum(returns) / len(returns)
    std_r = math.sqrt(sum((r - mean_r) ** 2 for r in returns) / max(1, len(returns) - 1)) if len(returns) > 1 else 0.0
    sharpe = (mean_r / std_r * math.sqrt(35040)) if std_r > 0 else 0.0

    running_max = eq[0]
    max_dd = 0.0
    for v in eq:
        running_max = max(running_max, v)
        dd = (running_max - v) / running_max if running_max > 0 else 0.0
        max_dd = max(max_dd, dd)

    wins = sum(1 for r in returns if r > 0)
    win_rate = (wins / len(returns)) * 100.0 if returns else 0.0

    return {
        "initial_equity": round(initial, 2),
        "final_equity": round(final, 2),
        "total_return_pct": round(total_return_pct, 2),
        "max_drawdown_pct": round(max_dd * 100.0, 2),
        "sharpe_ratio": round(sharpe, 2),
        "win_rate_pct": round(win_rate, 2),
        "total_intervals": len(returns),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Backtest on actual recorded tick data")
    parser.add_argument("--data-root", default=str(REPO_ROOT / "data" / "ticks"))
    parser.add_argument("--coins", default="BTC,ETH")
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "data" / "backtest_validation"))
    parser.add_argument("--initial-equity", type=float, default=100.0)
    args = parser.parse_args()

    coins = tuple(c.strip().upper() for c in args.coins.split(","))
    os.makedirs(args.out_dir, exist_ok=True)

    print(f"[1/4] Loading Kalshi ticks from {args.data_root} ...")
    kalshi_sec_map = load_kalshi_ticks(args.data_root, coins)
    for c in coins:
        print(f"    {c}: {sum(len(v) for v in kalshi_sec_map[c].values())} ticks across {len(kalshi_sec_map[c])} seconds")

    print("[2/4] Loading spot ticks ...")
    spot_map = load_spot_ticks(args.data_root, coins)
    for c in coins:
        print(f"    {c}: {len(spot_map[c])} seconds with spot data")

    print("[3/4] Building 15-minute candle timelines ...")
    all_candles: List[Candle] = []
    for c in coins:
        candles = build_candles(kalshi_sec_map[c], spot_map[c], c)
        print(f"    {c}: {len(candles)} candles")
        all_candles.extend(candles)
    all_candles.sort(key=lambda c: (c.open_ts, c.coin))
    print(f"    Total: {len(all_candles)} candles across {coins}")

    strategies = get_strategy_suite()
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "data_root": args.data_root,
        "coins": list(coins),
        "total_candles": len(all_candles),
        "initial_equity": args.initial_equity,
        "strategies": {},
    }

    for cfg in strategies:
        print(f"[4/4] Running strategy: {cfg.name} ...")
        equity = args.initial_equity
        equity_curve = [equity]
        all_trades: List[Dict[str, Any]] = []

        for candle in all_candles:
            equity, trades = simulate_candle(
                candle, cfg, equity,
                spot_lead_map=spot_map.get(candle.coin, {}),
                coin_weight=0.50,
                max_trade_alloc_pct=20.0,
                base_contracts=40,
            )
            equity_curve.append(equity)
            for t in trades:
                all_trades.append(t)

        metrics = calculate_risk_metrics(equity_curve)
        wins = sum(1 for t in all_trades if t.get("pnl", 0) > 0)
        losses = sum(1 for t in all_trades if t.get("pnl", 0) < 0)
        gross_win = sum(t.get("pnl", 0) for t in all_trades if t.get("pnl", 0) > 0)
        gross_loss = abs(sum(t.get("pnl", 0) for t in all_trades if t.get("pnl", 0) < 0))
        profit_factor = (gross_win / gross_loss) if gross_loss > 0 else 0.0
        fees_paid = sum(t.get("entry_fee", 0) + t.get("exit_fee", 0) for t in all_trades)

        report["strategies"][cfg.name] = {
            "config": {
                "max_entry_price": cfg.max_entry_price,
                "min_entry_price": cfg.min_entry_price,
                "min_edge_pct": cfg.min_edge_pct,
                "min_prob_btc": cfg.min_prob_btc,
                "min_prob_eth": cfg.min_prob_eth,
                "min_sec_btc": cfg.min_sec_btc,
                "min_sec_eth": cfg.min_sec_eth,
                "max_sec_entry": cfg.max_sec_entry,
                "take_profit_roi_pct": cfg.take_profit_roi_pct,
                "stop_loss_roi_pct": cfg.stop_loss_roi_pct,
                "max_hold_seconds": cfg.max_hold_seconds,
                "signal_mode": cfg.signal_mode,
                "min_cf_edge_dollars": cfg.min_cf_edge_dollars,
                "min_dir_z": cfg.min_dir_z,
                "structural_stop_z": cfg.structural_stop_z,
            },
            "initial_balance": args.initial_equity,
            "final_equity": round(equity, 2),
            "net_pnl": round(equity - args.initial_equity, 2),
            "return_pct": round(((equity - args.initial_equity) / args.initial_equity) * 100.0, 2),
            "total_trades": len(all_trades),
            "wins": wins,
            "losses": losses,
            "win_rate_pct": round((wins / len(all_trades)) * 100.0, 2) if all_trades else 0.0,
            "profit_factor": round(profit_factor, 2),
            "expectancy_per_trade": round((equity - args.initial_equity) / max(1, len(all_trades)), 2),
            "gross_win": round(gross_win, 2),
            "gross_loss": round(gross_loss, 2),
            "max_dd_dollars": round(args.initial_equity - min(equity_curve), 2),
            "max_dd_pct": round((args.initial_equity - min(equity_curve)) / args.initial_equity * 100.0, 2),
            "sharpe": metrics["sharpe_ratio"],
            "fees_paid": round(fees_paid, 2),
            "metrics": metrics,
        }

        # Write equity curve CSV
        eq_csv = os.path.join(args.out_dir, f"equity_curve_{cfg.name}.csv")
        with open(eq_csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["step", "equity"])
            for i, v in enumerate(equity_curve):
                w.writerow([i, round(v, 2)])

    # Write JSON report
    report_path = os.path.join(args.out_dir, "tick_backtest_report.json")
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)

    print("\n" + "=" * 70)
    print("TICK BACKTEST RESULTS (ACTUAL RECORDED DATA)")
    print("=" * 70)
    for name, s in report["strategies"].items():
        print(f"\n=== {name} ===")
        print(f"  Final equity:  ${s['final_equity']:.2f} (from ${s['initial_balance']:.2f})")
        print(f"  Net PnL:       ${s['net_pnl']:+.2f} ({s['return_pct']:+.2f}%)")
        print(f"  Trades:        {s['total_trades']} | Wins: {s['wins']} | Losses: {s['losses']} | Win rate: {s['win_rate_pct']:.1f}%")
        print(f"  Profit factor: {s['profit_factor']:.2f} | Expectancy: ${s['expectancy_per_trade']:+.2f}/trade")
        print(f"  Max DD:        ${s['max_dd_dollars']:.2f} ({s['max_dd_pct']:.1f}%) | Sharpe: {s['sharpe']:.2f}")
        print(f"  Fees paid:     ${s['fees_paid']:.2f}")
    print(f"\nReport: {report_path}")
    print("=" * 70)


if __name__ == "__main__":
    main()

