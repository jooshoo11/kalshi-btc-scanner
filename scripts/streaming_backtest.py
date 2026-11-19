#!/usr/bin/env python3
"""
scripts/streaming_backtest.py — Live Real-Time Streaming Backtest & Walk-Forward Engine

Streams real historical Kalshi L1/L2 book ticks + multi-exchange spot ticks
(Coinbase, Kraken, Hyperliquid, Binance) from:
  1. ~/kalshi_paper_bot/logs/leadlag/spot_ticks_*.csv (79 hourly files, 3.7 GB)
  2. ./data/ticks/ (newly recorded live ticks from scripts/tick_recorder.py)

Features:
  - Fast 1-second multi-exchange + Kalshi book resampler with persistent workspace cache
    (data/leadlag_1s_cache.pkl) so subsequent runs execute in < 2 seconds.
  - Real-time streaming terminal dashboard: emits live candle-by-candle trade executions,
    L2 VWAP fill slippage, Kalshi non-linear fees, CF Benchmarks 60s arithmetic-mean
    settlements, and live equity curves.
  - Side-by-side comparison of 5 strategies:
      1. S1_TRIAL3_BASELINE    (42c cap, REST quote fill, +25% TP / -25% SL)
      2. S2_TRIAL4_DEPLOYED    (Current deployed 35c/78c caps, L2 VWAP fill + 15% depth cap)
      3. S3_REC_LEADLAG_OFI    (Recommended #1: Binance/HL vs CB/Kraken lead-lag basis + OFI filter)
      4. S4_REC_CONVEX_RATCHET (Recommended #2: +45% Convex Maker TP + +18% Breakeven Stop Ratchet)
      5. S5_REC_HYBRID_ENSEMBLE(Recommended #3: Lead-Lag + Convex Ratchet + Final-45s CF Lock)
  - Rolling 5-Fold Out-of-Sample Walk-Forward Optimization & Verification.
"""

import argparse
import glob
import itertools
import json
import math
import os
import pickle
import re
import sys
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_LEADLAG_GLOB = os.path.expanduser("~/kalshi_paper_bot/logs/leadlag/spot_ticks_*.csv")
DEFAULT_CACHE_PATH = str(REPO_ROOT / "data" / "leadlag_1s_cache.pkl")
DEFAULT_REPORT_JSON = str(REPO_ROOT / "data" / "streaming_backtest_results.json")

# Base 15-minute dollar volatility defaults from coins/*/config.json
BASE_VOL_15M_PCT = {
    "BTC": 95.0 / 70000.0,   # ~13.5 bps per 15m candle (scaled dynamically to spot)
    "ETH": 6.5 / 2200.0,     # ~29.5 bps per 15m candle
    "SOL": 0.45 / 92.0,
    "XRP": 0.0055 / 1.18,
}


def calculate_kalshi_taker_fee(contracts: int, price: float) -> float:
    """Exact Kalshi non-linear taker fee: ceil(0.07 * C * P * (1 - P) * 100) / 100."""
    if contracts <= 0 or price <= 0.0 or price >= 1.0:
        return 0.0
    raw_cents = 0.07 * float(contracts) * float(price) * (1.0 - float(price)) * 100.0
    return math.ceil(raw_cents - 1e-9) / 100.0


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def simulate_l2_vwap_fill(
    top_price: float,
    top_size: float,
    contracts: int,
    enforce_depth_cap: bool = True,
    depth_cap_pct: float = 0.15,
    ladder_decay: float = 0.85,
) -> Tuple[int, float, float]:
    """
    Model realistic L2 orderbook sweep using visible top-of-book size (`bid_sz` / `ask_sz`)
    and geometric liquidity ladder across adjacent 1c levels when an order exceeds top size.
    Returns (filled_contracts, vwap_price, total_visible_depth).
    """
    if contracts <= 0 or top_price <= 0.0 or top_price >= 1.0:
        return 0, top_price, 0.0

    # Visible depth across top 5 levels (~2.6x top-of-book on Kalshi crypto 15m markets)
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


@dataclass
class CandleTimeline:
    """
    1-second resampled 15-minute Kalshi market timeline with multi-exchange spot feeds.
    """
    coin: str
    ticker: str
    open_ts: int
    close_ts: int
    strike: float                # CF Benchmarks 60s mean at candle open
    settlement_price: float      # CF Benchmarks 60s arithmetic mean at candle close
    yes_won: bool                # True if settlement_price >= strike
    # Parallel 1-second arrays sorted by timestamp ts_sec
    ts_sec: List[int] = field(default_factory=list)
    cf_spot: List[float] = field(default_factory=list)     # Coinbase/Kraken reference spot
    lead_spot: List[float] = field(default_factory=list)   # Hyperliquid/Binance lead spot
    yes_bid: List[float] = field(default_factory=list)
    yes_ask: List[float] = field(default_factory=list)
    yes_bid_sz: List[float] = field(default_factory=list)
    yes_ask_sz: List[float] = field(default_factory=list)
    signed_flow: List[float] = field(default_factory=list) # Net buy - sell USD flow in 1s


def _safe_float(s: str) -> Optional[float]:
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def build_or_load_1s_candles(
    csv_glob: str = DEFAULT_LEADLAG_GLOB,
    cache_path: str = DEFAULT_CACHE_PATH,
    coins: Tuple[str, ...] = ("BTC", "ETH"),
    force_rebuild: bool = False,
    stream_progress: bool = True,
) -> List[CandleTimeline]:
    """
    Streams through all hourly leadlag tick CSVs, downsamples to 1-second multi-exchange +
    Kalshi L1/L2 book timelines per 15-minute candle, and caches to `cache_path`.
    """
    if not force_rebuild and os.path.exists(cache_path):
        try:
            with open(cache_path, "rb") as f:
                cached = pickle.load(f)
            candles = [c for c in cached.get("candles", []) if c.coin in coins]
            if candles:
                if stream_progress:
                    print(
                        f"⚡ [Cache Hit] Loaded {len(candles)} 15-minute 1s-resolution market timelines "
                        f"from {cache_path} ({cached.get('source_files', 0)} hourly files)"
                    )
                return candles
        except Exception as e:
            print(f"[Cache Warning] Rebuilding cache ({e})")

    csv_files = sorted(glob.glob(csv_glob))
    if not csv_files:
        raise FileNotFoundError(f"No tick CSV files matched: {csv_glob}")

    if stream_progress:
        print(f"📡 [Streaming Ingestion] Processing {len(csv_files)} hourly tick CSV files from {os.path.dirname(csv_files[0])}...")

    # Per-coin 1-second state across all files:
    # sec_spot_cf[coin][sec] = last CB/Kraken price
    # sec_spot_lead[coin][sec] = last HL/Binance price
    # sec_flow[coin][sec] = signed trade USD flow
    # sec_book[coin][sec] = (ticker, bid, ask, bid_sz, ask_sz)
    sec_spot_cf: Dict[str, Dict[int, float]] = {c: {} for c in coins}
    sec_spot_lead: Dict[str, Dict[int, float]] = {c: {} for c in coins}
    sec_flow: Dict[str, Dict[int, float]] = {c: defaultdict(float) for c in coins}
    sec_book: Dict[str, Dict[int, Tuple[str, float, float, float, float]]] = {c: {} for c in coins}

    t_start = time.time()
    total_rows = 0
    coin_set = set(coins)

    for idx, path in enumerate(csv_files, 1):
        f_t0 = time.time()
        f_rows = 0
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.readline()  # skip header
            for line in f:
                f_rows += 1
                parts = line.rstrip("\n").split(",")
                if len(parts) < 12:
                    continue
                coin = parts[3]
                if coin not in coin_set:
                    continue
                try:
                    sec = int(parts[0]) // 1_000_000_000
                except ValueError:
                    continue

                kind = parts[1]
                src = parts[2]
                if kind == "book" and src == "kalshi":
                    ticker = parts[4]
                    if not ticker:
                        continue
                    bid = _safe_float(parts[5])
                    ask = _safe_float(parts[6])
                    if bid is None or ask is None or bid <= 0.0 or ask <= 0.0 or bid >= ask:
                        continue
                    bid_sz = _safe_float(parts[7]) or 100.0
                    ask_sz = _safe_float(parts[8]) or 100.0
                    sec_book[coin][sec] = (ticker, bid, ask, bid_sz, ask_sz)
                elif kind == "spot":
                    if src == "basis":
                        continue
                    px = _safe_float(parts[9])
                    if px is None or px <= 0.0:
                        continue
                    if src in ("coinbase", "kraken"):
                        sec_spot_cf[coin][sec] = px
                    elif src in ("hyperliquid", "binance"):
                        sec_spot_lead[coin][sec] = px
                    qty = _safe_float(parts[10])
                    side = parts[11]
                    if qty and qty > 0 and side in ("buy", "sell"):
                        signed = (qty * px) if side == "buy" else -(qty * px)
                        sec_flow[coin][sec] += signed

        total_rows += f_rows
        if stream_progress and (idx % 5 == 0 or idx == len(csv_files) or idx == 1):
            elapsed = time.time() - t_start
            rate = (total_rows / 1e6) / max(0.01, elapsed)
            print(
                f"  ├─ [{idx:02d}/{len(csv_files):02d}] {os.path.basename(path)} | "
                f"+{f_rows:,} rows ({time.time()-f_t0:.1f}s) | Cum: {total_rows/1e6:.1f}M rows ({rate:.2f}M/s)",
                flush=True,
            )

    # Group book seconds into 15-minute (900s) market candles by (coin, ticker, bucket_900)
    candles: List[CandleTimeline] = []
    for coin in coins:
        # Build forward-filled spot lookup for CF reference and Lead spot
        all_secs = sorted(set(sec_spot_cf[coin].keys()) | set(sec_spot_lead[coin].keys()) | set(sec_book[coin].keys()))
        if not all_secs:
            continue

        cf_map = sec_spot_cf[coin]
        lead_map = sec_spot_lead[coin]
        flow_map = sec_flow[coin]
        book_map = sec_book[coin]

        # Forward-fill spot series over all active seconds
        ff_cf: Dict[int, float] = {}
        ff_lead: Dict[int, float] = {}
        last_cf = 0.0
        last_lead = 0.0
        for s in all_secs:
            if s in cf_map:
                last_cf = cf_map[s]
            if s in lead_map:
                last_lead = lead_map[s]
            eff_cf = last_cf if last_cf > 0 else last_lead
            eff_lead = last_lead if last_lead > 0 else last_cf
            if eff_cf > 0:
                ff_cf[s] = eff_cf
                ff_lead[s] = eff_lead

        # Group book observations by (ticker, 15m boundary)
        by_candle: Dict[Tuple[str, int], List[int]] = defaultdict(list)
        for s, (ticker, bid, ask, b_sz, a_sz) in sorted(book_map.items()):
            if s not in ff_cf:
                continue
            open_boundary = (s // 900) * 900
            by_candle[(ticker, open_boundary)].append(s)

        for (ticker, open_ts), secs in sorted(by_candle.items(), key=lambda x: x[0][1]):
            if len(secs) < 45:  # require at least 45 active book seconds in the candle
                continue
            close_ts = open_ts + 900
            # Require observations near both the start/middle and end of the candle so settlement is observable
            if secs[-1] < close_ts - 120:
                continue

            # CF Benchmarks 60s arithmetic mean at candle open (strike)
            open_window = [ff_cf[s] for s in range(open_ts - 60, open_ts + 15) if s in ff_cf]
            strike = sum(open_window) / len(open_window) if open_window else ff_cf[secs[0]]

            # CF Benchmarks 60s arithmetic mean over final 60s [close_ts - 60, close_ts)
            close_window = [ff_cf[s] for s in range(close_ts - 60, close_ts + 1) if s in ff_cf]
            if len(close_window) < 10:
                # Fallback to last 60 observed seconds in the candle
                close_window = [ff_cf[s] for s in secs[-60:] if s in ff_cf]
            settlement_price = sum(close_window) / len(close_window)
            yes_won = (settlement_price >= strike)

            ct = CandleTimeline(
                coin=coin,
                ticker=ticker,
                open_ts=open_ts,
                close_ts=close_ts,
                strike=round(strike, 4),
                settlement_price=round(settlement_price, 4),
                yes_won=yes_won,
            )
            for s in secs:
                _, bid, ask, b_sz, a_sz = book_map[s]
                ct.ts_sec.append(s)
                ct.cf_spot.append(ff_cf[s])
                ct.lead_spot.append(ff_lead.get(s, ff_cf[s]))
                ct.yes_bid.append(bid)
                ct.yes_ask.append(ask)
                ct.yes_bid_sz.append(b_sz)
                ct.yes_ask_sz.append(a_sz)
                ct.signed_flow.append(flow_map.get(s, 0.0))
            candles.append(ct)

    candles.sort(key=lambda c: (c.open_ts, c.coin, c.ticker))
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "wb") as f:
        pickle.dump(
            {
                "created_at": time.time(),
                "source_files": len(csv_files),
                "total_rows": total_rows,
                "candles": candles,
            },
            f,
            protocol=pickle.HIGHEST_PROTOCOL,
        )
    if stream_progress:
        print(
            f"✅ [Cache Saved] Built {len(candles)} 15-minute market timelines from {total_rows:,} ticks "
            f"in {time.time()-t_start:.1f}s -> {cache_path}\n"
        )
    return candles


@dataclass
class StrategyConfig:
    name: str
    description: str
    max_entry_price: float = 0.35
    min_entry_price: float = 0.20
    certainty_arb_cap: float = 0.78
    enable_certainty_arb: bool = True
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
    # Signal reference & exit regime:
    # - "raw_hl_vs_cf_strike": exact app.py behavior (raw lead_spot - cf_strike, -25% bid SL, 120s time exit)
    # - "cf_disloc_Structural": de-biased CF Benchmarks spot dislocation (z >= min_z, py - ask >= min_cf_edge)
    #   with +55%..+65% Maker TP and structural Z-score stop (no -25% bid-noise shakeout or 120s forced dump)
    signal_mode: str = "raw_hl_vs_cf_strike"
    settle_on_raw_lead: bool = False
    min_cf_edge_dollars: float = 0.08
    min_dir_z: float = 0.0
    min_vel10_bps: float = 0.0
    min_leadlag_imp_bps: float = -99.0
    structural_stop_z: Optional[float] = -1.8  # None = hold to settlement unless Maker TP fills
    structural_stop_roi_pct: float = 45.0


def get_strategy_suite() -> List[StrategyConfig]:
    return [
        StrategyConfig(
            name="S1_TRIAL3_RAW_IDEAL",
            description="Trial 3 Raw app.py (42c cap, un-debiased HL-vs-BRTI delta, REST quote fill, -25% SL / 120s time exit)",
            max_entry_price=0.42,
            certainty_arb_cap=0.85,
            use_l2_vwap_fills=False,
            enforce_depth_cap=False,
            signal_mode="raw_hl_vs_cf_strike",
            settle_on_raw_lead=True,
            take_profit_roi_pct=25.0,
            stop_loss_roi_pct=25.0,
            max_hold_seconds=120,
        ),
        StrategyConfig(
            name="S2_TRIAL4_DEPLOYED_L2",
            description="Current Deployed Rules on Real 1s Book (35c/78c caps, raw HL-vs-BRTI delta, L2 VWAP fill, -25% SL / 120s exit)",
            max_entry_price=0.35,
            certainty_arb_cap=0.78,
            use_l2_vwap_fills=True,
            enforce_depth_cap=True,
            signal_mode="raw_hl_vs_cf_strike",
            settle_on_raw_lead=False,
            take_profit_roi_pct=25.0,
            stop_loss_roi_pct=25.0,
            max_hold_seconds=120,
        ),
        StrategyConfig(
            name="S3_REC_CF_DISLOC_NOSL",
            description="Recommended #1: De-Biased CF Spot Dislocation (20-45c, edge>=8c, z>=0, v10>=0.1bps) + +55% Maker TP, No Noise SL",
            max_entry_price=0.45,
            min_sec_btc=90,
            min_sec_eth=90,
            max_sec_entry=660,
            max_spread_dollars=0.05,
            max_trades_per_candle=1,
            use_l2_vwap_fills=True,
            enforce_depth_cap=True,
            signal_mode="cf_disloc_structural",
            min_cf_edge_dollars=0.08,
            min_dir_z=0.0,
            min_vel10_bps=0.10,
            min_leadlag_imp_bps=-99.0,
            take_profit_roi_pct=55.0,
            structural_stop_z=None,
        ),
        StrategyConfig(
            name="S4_REC_STRUCT_STOP_55TP",
            description="Recommended #2: CF Dislocation (20-48c, edge>=8c, z>=0.05, v10>=0.2bps) + +55% Maker TP + Deep Structural SL (z<=-1.8)",
            max_entry_price=0.48,
            min_sec_btc=90,
            min_sec_eth=90,
            max_sec_entry=660,
            max_spread_dollars=0.05,
            max_trades_per_candle=1,
            use_l2_vwap_fills=True,
            enforce_depth_cap=True,
            signal_mode="cf_disloc_structural",
            min_cf_edge_dollars=0.08,
            min_dir_z=0.05,
            min_vel10_bps=0.20,
            min_leadlag_imp_bps=-99.0,
            take_profit_roi_pct=55.0,
            structural_stop_z=-1.8,
            structural_stop_roi_pct=45.0,
        ),
        StrategyConfig(
            name="S5_REC_LEADLAG_ENSEMBLE",
            description="Recommended #3 (Best PF/Sharpe): CF Dislocation (20-48c, edge>=9c, z>=0.05) + De-Biased Lead-Lag Filter + +55% TP + Structural SL",
            max_entry_price=0.48,
            min_sec_btc=90,
            min_sec_eth=90,
            max_sec_entry=660,
            max_spread_dollars=0.05,
            max_trades_per_candle=1,
            use_l2_vwap_fills=True,
            enforce_depth_cap=True,
            signal_mode="cf_disloc_structural",
            min_cf_edge_dollars=0.09,
            min_dir_z=0.05,
            min_vel10_bps=0.0,
            min_leadlag_imp_bps=-0.50,
            take_profit_roi_pct=55.0,
            structural_stop_z=-1.8,
            structural_stop_roi_pct=45.0,
        ),
    ]


def simulate_candle_for_strategy(
    candle: CandleTimeline,
    cfg: StrategyConfig,
    equity: float,
    coin_weight: float = 0.50,
    max_trade_alloc_pct: float = 20.0,
) -> Tuple[float, List[Dict[str, Any]]]:
    """
    Simulate a single 15-minute candle timeline second-by-second under `cfg`.
    Returns (new_equity, closed_trades).
    """
    n = len(candle.ts_sec)
    if n < 10:
        return equity, []

    coin = candle.coin
    strike = candle.strike
    base_vol_pct = BASE_VOL_15M_PCT.get(coin, 0.0020)
    base_vol_dollar = strike * base_vol_pct

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
        spot = candle.cf_spot[i]
        lead = candle.lead_spot[i]
        y_bid = candle.yes_bid[i]
        y_ask = candle.yes_ask[i]
        y_bid_sz = candle.yes_bid_sz[i]
        y_ask_sz = candle.yes_ask_sz[i]
        n_bid = round(1.0 - y_ask, 4)
        n_ask = round(1.0 - y_bid, 4)
        n_bid_sz = y_ask_sz
        n_ask_sz = y_bid_sz

        spot_win.append((ts, spot))
        flow_win.append(candle.signed_flow[i])
        if seconds_left <= 60:
            cf_final60_samples.append(spot)

        # Rolling perpetual-vs-spot basis EMA (de-biases permanent HL/Binance perp premium over CF Benchmarks)
        inst_basis = lead - spot
        if basis_ema is None:
            basis_ema = inst_basis
        else:
            basis_ema = 0.95 * basis_ema + 0.05 * inst_basis
        lead_impulse_dollar = inst_basis - basis_ema
        leadlag_bps = (lead_impulse_dollar / max(1e-6, spot)) * 10000.0

        # Dynamic realized volatility (blended 50/50 with base 15m vol)
        eff_vol = base_vol_dollar
        if len(spot_win) >= 10:
            diffs = [
                abs(spot_win[j][1] - spot_win[j - 1][1]) / math.sqrt(max(1.0, spot_win[j][0] - spot_win[j - 1][0]))
                for j in range(1, len(spot_win))
            ]
            if diffs:
                realized_15m = (sum(diffs) / len(diffs)) * 30.0
                eff_vol = max(base_vol_dollar * 0.6, min(base_vol_dollar * 2.2, 0.5 * base_vol_dollar + 0.5 * realized_15m))

        # Signed flow OFI Z-score
        ofi_z = 0.0
        if len(flow_win) >= 5:
            mean_f = sum(flow_win) / len(flow_win)
            var_f = sum((x - mean_f) ** 2 for x in flow_win) / len(flow_win)
            std_f = math.sqrt(var_f) if var_f > 1.0 else 1.0
            ofi_z = max(-3.0, min(3.0, (sum(list(flow_win)[-5:]) / (5.0 * std_f))))

        # 10s & 20s spot velocity
        vel_20s = spot - spot_win[0][1] if len(spot_win) >= 4 else 0.0
        vel_10s_bps = ((spot - candle.cf_spot[max(0, i - 10)]) / max(1e-6, spot)) * 10000.0
        sigma_base = base_vol_dollar * math.sqrt(max(1.0, float(seconds_left)) / 900.0)
        sigma = eff_vol * math.sqrt(max(1.0, float(seconds_left)) / 900.0)
        z_cf = (spot - strike) / max(1e-4, sigma_base)
        py_cf = normal_cdf(z_cf)
        pn_cf = 1.0 - py_cf

        # Drift-diffusion probability calculation by signal_mode
        if cfg.signal_mode == "raw_hl_vs_cf_strike":
            # Exact app.py formula: compares raw Hyperliquid perp price against CF Benchmarks floor_strike
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

        # ==================== 1. MANAGE ACTIVE POSITION ====================
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
                if cfg.settle_on_raw_lead:
                    yes_settled = (lead >= strike)
                    won = yes_settled if side == "YES" else (not yes_settled)
                    reason_str = "SETTLED_EST_WIN" if won else "SETTLED_EST_LOSS"
                else:
                    won = candle.yes_won if side == "YES" else (not candle.yes_won)
                    reason_str = "SETTLED_WIN" if won else "SETTLED_LOSS"
                exit_px = 1.0 if won else 0.0
                payout = pos["contracts"] * exit_px
                pnl = round(payout - (pos["cost"] + pos["entry_fee"]), 2)
                equity = round(equity + payout, 2)
                trades.append({
                    **pos,
                    "exit_ts": ts,
                    "exit_price": exit_px,
                    "exit_fee": 0.0,
                    "exit_reason": reason_str,
                    "pnl": pnl,
                    "roi_pct": round((pnl / max(0.01, pos["cost"] + pos["entry_fee"])) * 100.0, 1),
                    "equity_after": equity,
                })
                pos = None
                last_exit_ts = ts
                continue

            # B. Take Profit (Passive Maker Limit Order -> 0c fee)
            if roi_pct >= cfg.take_profit_roi_pct:
                exit_reason = "TAKE_PROFIT"
                is_maker_exit = cfg.maker_take_profit

            # C. Stop Loss / Time Exit by regime
            if exit_reason is None:
                if cfg.signal_mode == "raw_hl_vs_cf_strike":
                    is_hard_stop = (roi_pct <= -cfg.stop_loss_hard_roi_pct)
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
                    # cf_disloc_structural: avoids -25% bid-noise shakeouts; only stops out on deep structural breakdown
                    if (
                        cfg.structural_stop_z is not None
                        and hold_secs >= 60
                        and dir_z_cf <= cfg.structural_stop_z
                        and roi_pct <= -cfg.structural_stop_roi_pct
                    ):
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
                trades.append({
                    **pos,
                    "exit_ts": ts,
                    "exit_price": exit_px,
                    "exit_fee": exit_fee,
                    "exit_reason": exit_reason,
                    "pnl": pnl,
                    "roi_pct": round((pnl / max(0.01, pos["cost"] + pos["entry_fee"])) * 100.0, 1),
                    "equity_after": equity,
                })
                pos = None
                last_exit_ts = ts
            continue

        # ==================== 2. EVALUATE NEW ENTRY ====================
        if stopped_out_in_candle or trades_in_candle >= cfg.max_trades_per_candle or (ts - last_exit_ts) < 4:
            continue

        spread_y = y_ask - y_bid
        spread_n = n_ask - n_bid
        is_liquid_y = (y_bid >= 0.05) and (spread_y <= cfg.max_spread_dollars) and (spread_y / max(0.01, y_ask) <= 0.35)
        is_liquid_n = (n_bid >= 0.05) and (spread_n <= cfg.max_spread_dollars) and (spread_n / max(0.01, n_ask) <= 0.35)

        fee_y = calculate_kalshi_taker_fee(40, y_ask) / 40.0 if 0.0 < y_ask < 1.0 else 0.0
        fee_n = calculate_kalshi_taker_fee(40, n_ask) / 40.0 if 0.0 < n_ask < 1.0 else 0.0
        edge_y = (prob_yes - (y_ask + fee_y)) * 100.0 if y_ask > 0 else -100.0
        edge_n = (prob_no - (n_ask + fee_n)) * 100.0 if n_ask > 0 else -100.0

        adverse_vel_thresh = 18.0 if coin == "BTC" else 2.0
        target_side = None
        quote_ask = 0.0
        quote_sz = 0.0
        model_prob_entry = 0.0
        edge_entry = 0.0
        is_cert_arb = False

        if min_sec <= seconds_left <= cfg.max_sec_entry:
            if cfg.signal_mode == "raw_hl_vs_cf_strike":
                if (
                    is_liquid_y
                    and cfg.min_entry_price <= y_ask <= cfg.max_entry_price
                    and prob_yes_pct >= min_prob
                    and edge_y >= cfg.min_edge_pct
                    and vel_20s >= -adverse_vel_thresh
                ):
                    target_side = "YES"
                    quote_ask = y_ask
                    quote_sz = y_ask_sz
                    model_prob_entry = prob_yes_pct
                    edge_entry = edge_y
                elif (
                    is_liquid_n
                    and cfg.min_entry_price <= n_ask <= cfg.max_entry_price
                    and prob_no_pct >= min_prob
                    and edge_n >= cfg.min_edge_pct
                    and vel_20s <= adverse_vel_thresh
                ):
                    target_side = "NO"
                    quote_ask = n_ask
                    quote_sz = n_ask_sz
                    model_prob_entry = prob_no_pct
                    edge_entry = edge_n
            else:
                # cf_disloc_structural: True CF Benchmarks spot has crossed strike (z_cf >= min_dir_z)
                # while Kalshi ask lags true CF probability by >= min_cf_edge_dollars
                if i < 15:
                    continue
                if (
                    is_liquid_y
                    and cfg.min_entry_price <= y_ask <= cfg.max_entry_price
                    and (py_cf - y_ask) >= cfg.min_cf_edge_dollars
                    and z_cf >= cfg.min_dir_z
                    and vel_10s_bps >= cfg.min_vel10_bps
                    and leadlag_bps >= cfg.min_leadlag_imp_bps
                ):
                    target_side = "YES"
                    quote_ask = y_ask
                    quote_sz = y_ask_sz
                    model_prob_entry = py_cf * 100.0
                    edge_entry = (py_cf - (y_ask + fee_y)) * 100.0
                elif (
                    is_liquid_n
                    and cfg.min_entry_price <= n_ask <= cfg.max_entry_price
                    and (pn_cf - n_ask) >= cfg.min_cf_edge_dollars
                    and (-z_cf) >= cfg.min_dir_z
                    and (-vel_10s_bps) >= cfg.min_vel10_bps
                    and (-leadlag_bps) >= cfg.min_leadlag_imp_bps
                ):
                    target_side = "NO"
                    quote_ask = n_ask
                    quote_sz = n_ask_sz
                    model_prob_entry = pn_cf * 100.0
                    edge_entry = (pn_cf - (n_ask + fee_n)) * 100.0

        # Final-45s CF Benchmarks Partial-Average Certainty Arbitrage (only for raw_hl_vs_cf_strike)
        if (
            target_side is None
            and cfg.signal_mode == "raw_hl_vs_cf_strike"
            and cfg.enable_certainty_arb
            and 5 < seconds_left <= 45
            and len(cf_final60_samples) >= 15
        ):
            n_obs = len(cf_final60_samples)
            obs_sum = sum(cf_final60_samples)
            n_rem = max(1, 60 - n_obs)
            projected_mean = (obs_sum + n_rem * spot) / 60.0
            rem_sigma = eff_vol * math.sqrt(seconds_left / 900.0) * (n_rem / 60.0)
            z_cert = (projected_mean - strike) / max(1e-4, rem_sigma)
            p_cert_yes = normal_cdf(z_cert)
            p_cert_no = 1.0 - p_cert_yes
            if p_cert_yes >= 0.985 and is_liquid_y and 0.20 <= y_ask <= cfg.certainty_arb_cap:
                c_edge = (p_cert_yes - (y_ask + fee_y)) * 100.0
                if c_edge >= 3.5:
                    target_side = "YES"
                    quote_ask = y_ask
                    quote_sz = y_ask_sz
                    model_prob_entry = p_cert_yes * 100.0
                    edge_entry = c_edge
                    is_cert_arb = True
            elif p_cert_no >= 0.985 and is_liquid_n and 0.20 <= n_ask <= cfg.certainty_arb_cap:
                c_edge = (p_cert_no - (n_ask + fee_n)) * 100.0
                if c_edge >= 3.5:
                    target_side = "NO"
                    quote_ask = n_ask
                    quote_sz = n_ask_sz
                    model_prob_entry = p_cert_no * 100.0
                    edge_entry = c_edge
                    is_cert_arb = True

        if target_side is None:
            continue

        # Position sizing
        if cfg.signal_mode == "cf_disloc_structural":
            budget = min(25.0, equity * 0.15)
            contracts = int(budget / max(0.01, quote_ask + 0.02))
            if contracts < 5:
                continue
        else:
            sub_equity = equity * coin_weight
            budget = min(sub_equity * (max_trade_alloc_pct / 100.0), equity)
            dynamic_cap = min(max(25.0, equity * 0.12), 50.0)
            budget = min(budget, dynamic_cap)
            fee_est = 0.07 * quote_ask * (1.0 - quote_ask)
            contracts = int(budget / max(0.01, quote_ask + fee_est))
            contracts = min(contracts, 120)
            if contracts < 15:
                continue

        # L2 orderbook sweep & depth cap validation
        if cfg.use_l2_vwap_fills:
            filled_c, fill_price, book_depth = simulate_l2_vwap_fill(
                top_price=quote_ask,
                top_size=quote_sz,
                contracts=contracts,
                enforce_depth_cap=cfg.enforce_depth_cap,
                depth_cap_pct=0.15,
            )
            if filled_c < 1:
                continue
            max_cap = cfg.certainty_arb_cap if is_cert_arb else (cfg.max_entry_price + 0.01)
            if fill_price > max_cap:
                continue
            if cfg.signal_mode == "raw_hl_vs_cf_strike":
                unit_fee = calculate_kalshi_taker_fee(filled_c, fill_price) / float(filled_c)
                net_edge_after_slip = (model_prob_entry / 100.0 - (fill_price + unit_fee)) * 100.0
                min_req_edge = 3.0 if is_cert_arb else cfg.min_edge_pct
                if net_edge_after_slip < min_req_edge:
                    continue
            contracts = filled_c
        else:
            fill_price = quote_ask
            book_depth = quote_sz

        cost = round(contracts * fill_price, 2)
        entry_fee = calculate_kalshi_taker_fee(contracts, fill_price)
        if cost + entry_fee > equity:
            contracts = int((equity - entry_fee) / max(0.01, fill_price))
            if contracts < 1:
                continue
            cost = round(contracts * fill_price, 2)
            entry_fee = calculate_kalshi_taker_fee(contracts, fill_price)

        equity = round(equity - (cost + entry_fee), 2)
        trades_in_candle += 1
        slippage_bps = round(((fill_price - quote_ask) / max(0.01, quote_ask)) * 10000.0, 1)
        pos = {
            "coin": coin,
            "ticker": candle.ticker,
            "side": target_side,
            "contracts": contracts,
            "quote_price": quote_ask,
            "entry_price": fill_price,
            "slippage_bps": slippage_bps,
            "book_depth": int(book_depth),
            "cost": cost,
            "entry_fee": entry_fee,
            "entry_ts": ts,
            "seconds_left_entry": seconds_left,
            "model_prob": round(model_prob_entry, 1),
            "edge_pct": round(edge_entry, 1),
            "leadlag_bps": round(leadlag_bps, 2),
            "ofi_z": round(ofi_z, 2),
            "is_cert_arb": is_cert_arb,
        }

    return equity, trades


def summarize_trades(trades: List[Dict[str, Any]], initial_balance: float = 100.0) -> Dict[str, Any]:
    equity = initial_balance
    peak = initial_balance
    max_dd_dollars = 0.0
    max_dd_pct = 0.0
    wins = 0
    losses = 0
    gross_win = 0.0
    gross_loss = 0.0
    total_fees = 0.0
    returns: List[float] = []
    by_coin: Dict[str, Dict[str, float]] = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    by_reason: Dict[str, int] = defaultdict(int)

    for t in trades:
        pnl = float(t["pnl"])
        prev_eq = max(1.0, equity)
        equity = round(equity + pnl, 2)
        returns.append(pnl / prev_eq)
        total_fees += float(t.get("entry_fee", 0.0)) + float(t.get("exit_fee", 0.0))
        if pnl > 0:
            wins += 1
            gross_win += pnl
        else:
            losses += 1
            gross_loss += abs(pnl)
        if equity > peak:
            peak = equity
        dd_d = peak - equity
        dd_p = (dd_d / peak) * 100.0 if peak > 0 else 0.0
        if dd_d > max_dd_dollars:
            max_dd_dollars = dd_d
        if dd_p > max_dd_pct:
            max_dd_pct = dd_p

        c = t["coin"]
        by_coin[c]["trades"] += 1
        if pnl > 0:
            by_coin[c]["wins"] += 1
        by_coin[c]["pnl"] = round(by_coin[c]["pnl"] + pnl, 2)
        by_reason[t["exit_reason"]] += 1

    total = wins + losses
    win_rate = (wins / total * 100.0) if total > 0 else 0.0
    pf = (gross_win / gross_loss) if gross_loss > 0 else (99.9 if gross_win > 0 else 0.0)
    net_pnl = round(equity - initial_balance, 2)
    expectancy = round(net_pnl / total, 2) if total > 0 else 0.0

    sharpe = 0.0
    if len(returns) >= 3:
        mean_r = sum(returns) / len(returns)
        var_r = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        std_r = math.sqrt(var_r) if var_r > 1e-12 else 0.0
        if std_r > 0:
            sharpe = round((mean_r / std_r) * math.sqrt(len(returns)), 2)

    return {
        "initial_balance": initial_balance,
        "final_equity": equity,
        "peak_equity": round(peak, 2),
        "net_pnl": net_pnl,
        "return_pct": round((net_pnl / initial_balance) * 100.0, 2),
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(win_rate, 2),
        "profit_factor": round(pf, 2),
        "expectancy_per_trade": expectancy,
        "gross_win": round(gross_win, 2),
        "gross_loss": round(gross_loss, 2),
        "max_dd_dollars": round(max_dd_dollars, 2),
        "max_dd_pct": round(max_dd_pct, 2),
        "sharpe": sharpe,
        "fees_paid": round(total_fees, 2),
        "by_coin": dict(by_coin),
        "by_reason": dict(by_reason),
    }


def run_streaming_backtest(
    candles: List[CandleTimeline],
    strategies: List[StrategyConfig],
    initial_balance: float = 100.0,
    stream_delay_ms: float = 0.0,
    stream_every_n_candles: int = 1,
) -> Dict[str, Dict[str, Any]]:
    """
    Streams chronologically through all 15-minute candles and prints live real-time
    trade notifications and rolling scoreboard updates.
    """
    equities = {s.name: initial_balance for s in strategies}
    all_trades: Dict[str, List[Dict[str, Any]]] = {s.name: [] for s in strategies}

    print("=" * 118)
    print(
        f"🚀 LIVE STREAMING MULTI-STRATEGY BACKTEST | {len(candles)} 15M Market Timelines | "
        f"Initial Bankroll: ${initial_balance:.2f} | Exact Kalshi Fees"
    )
    print("=" * 118)
    for s in strategies:
        print(f"  • {s.name:<24} : {s.description}")
    print("-" * 118, flush=True)

    focus_strats = {"S2_TRIAL4_DEPLOYED_L2", "S5_REC_LEADLAG_ENSEMBLE"}

    for idx, candle in enumerate(candles, 1):
        dt_str = datetime.fromtimestamp(candle.open_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        candle_events = []

        for s in strategies:
            new_eq, c_trades = simulate_candle_for_strategy(candle, s, equities[s.name])
            equities[s.name] = new_eq
            if c_trades:
                all_trades[s.name].extend(c_trades)
                if s.name in focus_strats:
                    for tr in c_trades:
                        icon = "🟢" if tr["pnl"] > 0 else "🔴"
                        tag = "DEPLOYED" if s.name == "S2_TRIAL4_DEPLOYED_L2" else "REC_ENS "
                        candle_events.append(
                            f"    {icon} [{tag}] {tr['coin']} {tr['side']:<3} {tr['contracts']:>3}x "
                            f"entry={tr['entry_price']*100:4.1f}¢ (quote {tr['quote_price']*100:2.0f}¢, slip {tr['slippage_bps']:+5.0f}bps) "
                            f"-> exit={tr['exit_price']*100:5.1f}¢ [{tr['exit_reason']:<14}] "
                            f"PnL={tr['pnl']:+7.2f} ({tr['roi_pct']:+5.1f}%) | Eq=${tr['equity_after']:7.2f}"
                        )

        if candle_events or (idx % max(1, stream_every_n_candles) == 0) or idx == len(candles):
            board = " | ".join(
                f"{s.name.split('_')[0]}:${equities[s.name]:7.2f} ({len(all_trades[s.name])}tr)"
                for s in strategies
            )
            settle_tag = "YES_WON" if candle.yes_won else "NO_WON "
            print(
                f"[{idx:03d}/{len(candles):03d}] {dt_str} | {candle.coin:<3} {candle.ticker:<24} "
                f"Strike={candle.strike:8.2f} Settle={candle.settlement_price:8.2f} ({settle_tag}) || {board}",
                flush=True,
            )
            for ev in candle_events:
                print(ev, flush=True)
            if stream_delay_ms > 0:
                time.sleep(stream_delay_ms / 1000.0)

    print("\n" + "=" * 118)
    print("📊 FINAL MULTI-STRATEGY COMPARISON TABLE (Chronological Tick Stream)")
    print("=" * 118)
    hdr = (
        f"{'Strategy':<25} | {'Trades':>6} | {'W / L':>9} | {'WinRate':>7} | "
        f"{'Net PnL':>10} | {'Final Eq':>9} | {'PF':>5} | {'Expect':>7} | {'MaxDD%':>7} | {'Sharpe':>6} | {'Fees':>7}"
    )
    print(hdr)
    print("-" * 118)

    summaries = {}
    for s in strategies:
        m = summarize_trades(all_trades[s.name], initial_balance=initial_balance)
        summaries[s.name] = {"config": s.__dict__, "metrics": m, "trades": all_trades[s.name]}
        wl = f"{m['wins']}W/{m['losses']}L"
        print(
            f"{s.name:<25} | {m['total_trades']:>6} | {wl:>9} | {m['win_rate_pct']:>6.1f}% | "
            f"${m['net_pnl']:>+9.2f} | ${m['final_equity']:>8.2f} | {m['profit_factor']:>5.2f} | "
            f"${m['expectancy_per_trade']:>+6.2f} | {m['max_dd_pct']:>6.1f}% | {m['sharpe']:>6.2f} | ${m['fees_paid']:>6.2f}",
            flush=True,
        )
    print("=" * 118 + "\n", flush=True)
    return summaries


def run_rolling_walk_forward(
    candles: List[CandleTimeline],
    n_folds: int = 5,
    initial_balance: float = 100.0,
) -> Dict[str, Any]:
    """
    Executes a Rolling Out-of-Sample Walk-Forward Optimization across `n_folds` chronological folds.
    In each fold:
      - In-Sample (Train) window optimizes (max_entry_price, min_edge_pct, take_profit_roi_pct,
        breakeven_ratchet_roi_pct, require_leadlag_confirm) under realistic L2 VWAP fills.
      - Out-of-Sample (Test) window evaluates the winning parameter set strictly forward on unseen candles.
    """
    print("=" * 118)
    print(f"🔬 ROLLING {n_folds}-FOLD OUT-OF-SAMPLE WALK-FORWARD VALIDATION (L2 VWAP Fills + 15% Book Depth Cap)")
    print("=" * 118, flush=True)

    total_c = len(candles)
    if total_c < 30:
        print("Insufficient candles for walk-forward segmentation.")
        return {}

    # Candidate parameter grid across CF dislocation + structural stop + lead-lag filter parameters
    grid: List[StrategyConfig] = []
    for max_p, cf_edge, min_z, v10, ll_imp, tp, sl_z in itertools.product(
        [0.45, 0.48],
        [0.08, 0.09],
        [0.0, 0.05],
        [0.0, 0.10],
        [-0.50, -0.20],
        [55.0, 65.0],
        [None, -1.8],
    ):
        grid.append(
            StrategyConfig(
                name=f"WF_p{int(max_p*100)}_e{int(cf_edge*100)}c_z{min_z}_v{v10}_ll{ll_imp}_tp{int(tp)}_sl{sl_z}",
                description="Walk-forward candidate",
                signal_mode="cf_disloc_structural",
                max_entry_price=max_p,
                min_sec_btc=90,
                min_sec_eth=90,
                max_sec_entry=660,
                max_spread_dollars=0.05,
                max_trades_per_candle=1,
                min_cf_edge_dollars=cf_edge,
                min_dir_z=min_z,
                min_vel10_bps=v10,
                min_leadlag_imp_bps=ll_imp,
                take_profit_roi_pct=tp,
                structural_stop_z=sl_z,
                structural_stop_roi_pct=45.0,
                use_l2_vwap_fills=True,
                enforce_depth_cap=True,
            )
        )

    fold_span = total_c // (n_folds + 1)
    oos_combined_trades: List[Dict[str, Any]] = []
    baseline_oos_trades: List[Dict[str, Any]] = []
    rec_s5_oos_trades: List[Dict[str, Any]] = []
    fold_reports = []
    running_oos_eq = initial_balance
    running_base_eq = initial_balance
    running_s5_eq = initial_balance

    suite = get_strategy_suite()
    base_deployed_cfg = suite[1]  # S2_TRIAL4_DEPLOYED_L2
    rec_s5_cfg = suite[4]         # S5_REC_LEADLAG_ENSEMBLE

    for fold in range(1, n_folds + 1):
        train_start = (fold - 1) * (fold_span // 2)
        train_end = train_start + int(fold_span * 1.4)
        test_start = train_end
        test_end = min(total_c, test_start + fold_span) if fold < n_folds else total_c
        if test_start >= total_c or test_end - test_start < 5:
            break

        train_candles = candles[train_start:train_end]
        test_candles = candles[test_start:test_end]

        best_cfg = grid[0]
        best_score = -1e9
        best_train_m = None

        for cand in grid:
            eq = initial_balance
            tr_list: List[Dict[str, Any]] = []
            for c in train_candles:
                eq, c_tr = simulate_candle_for_strategy(c, cand, eq)
                tr_list.extend(c_tr)
            m = summarize_trades(tr_list, initial_balance=initial_balance)
            if m["total_trades"] < 5:
                score = -1000.0 + m["net_pnl"]
            else:
                # Regularized risk-adjusted objective (avoids small-sample 100% WR overfitting)
                capped_pf = min(2.2, m["profit_factor"])
                score = m["net_pnl"] * capped_pf - 2.0 * m["max_dd_dollars"] + 1.5 * m["total_trades"]
            if score > best_score:
                best_score = score
                best_cfg = cand
                best_train_m = m

        # Evaluate selected config Out-of-Sample on test_candles
        fold_start_eq = running_oos_eq
        fold_oos_trades: List[Dict[str, Any]] = []
        for c in test_candles:
            running_oos_eq, c_tr = simulate_candle_for_strategy(c, best_cfg, running_oos_eq)
            fold_oos_trades.extend(c_tr)
            oos_combined_trades.extend(c_tr)

        # Evaluate static S2_TRIAL4_DEPLOYED_L2 and static S5_REC_LEADLAG_ENSEMBLE on the same OOS window
        for c in test_candles:
            running_base_eq, b_tr = simulate_candle_for_strategy(c, base_deployed_cfg, running_base_eq)
            baseline_oos_trades.extend(b_tr)
            running_s5_eq, s5_tr = simulate_candle_for_strategy(c, rec_s5_cfg, running_s5_eq)
            rec_s5_oos_trades.extend(s5_tr)

        oos_m = summarize_trades(fold_oos_trades, initial_balance=fold_start_eq)
        t0_str = datetime.fromtimestamp(test_candles[0].open_ts, tz=timezone.utc).strftime("%m-%d %H:%M")
        t1_str = datetime.fromtimestamp(test_candles[-1].close_ts, tz=timezone.utc).strftime("%m-%d %H:%M")

        print(
            f"  ▶ Fold {fold}/{n_folds} | Train [{len(train_candles):>3} mkt] -> Test [{len(test_candles):>3} mkt | {t0_str}..{t1_str}] | "
            f"Selected: {best_cfg.name:<38}\n"
            f"      ├─ In-Sample  : PnL ${best_train_m['net_pnl']:+8.2f} | WR {best_train_m['win_rate_pct']:5.1f}% | "
            f"PF {best_train_m['profit_factor']:4.2f} | Trades {best_train_m['total_trades']:>3}\n"
            f"      └─ Out-Sample : PnL ${oos_m['net_pnl']:+8.2f} | WR {oos_m['win_rate_pct']:5.1f}% | "
            f"PF {oos_m['profit_factor']:4.2f} | Trades {oos_m['total_trades']:>3} | Cum OOS Eq: ${running_oos_eq:8.2f}",
            flush=True,
        )
        fold_reports.append({
            "fold": fold,
            "train_markets": len(train_candles),
            "test_markets": len(test_candles),
            "selected_params": best_cfg.name,
            "in_sample": best_train_m,
            "out_of_sample": oos_m,
        })

    overall_oos = summarize_trades(oos_combined_trades, initial_balance=initial_balance)
    overall_s5_oos = summarize_trades(rec_s5_oos_trades, initial_balance=initial_balance)
    overall_base_oos = summarize_trades(baseline_oos_trades, initial_balance=initial_balance)
    print("-" * 118)
    print(
        f"🏆 CUMULATIVE OUT-OF-SAMPLE WALK-FORWARD RESULT ({len(fold_reports)} Folds, Unseen Test Windows Only):\n"
        f"  • Adaptive Walk-Forward OOS : Net PnL ${overall_oos['net_pnl']:+8.2f} (Final Eq ${overall_oos['final_equity']:.2f}) | "
        f"Trades {overall_oos['total_trades']:>2} | WR {overall_oos['win_rate_pct']:5.1f}% | PF {overall_oos['profit_factor']:4.2f} | "
        f"MaxDD {overall_oos['max_dd_pct']:4.1f}% | Sharpe {overall_oos['sharpe']:+5.2f}\n"
        f"  • Recommended S5 Static OOS : Net PnL ${overall_s5_oos['net_pnl']:+8.2f} (Final Eq ${overall_s5_oos['final_equity']:.2f}) | "
        f"Trades {overall_s5_oos['total_trades']:>2} | WR {overall_s5_oos['win_rate_pct']:5.1f}% | PF {overall_s5_oos['profit_factor']:4.2f} | "
        f"MaxDD {overall_s5_oos['max_dd_pct']:4.1f}% | Sharpe {overall_s5_oos['sharpe']:+5.2f}\n"
        f"  • Deployed S2 Static OOS    : Net PnL ${overall_base_oos['net_pnl']:+8.2f} (Final Eq ${overall_base_oos['final_equity']:.2f}) | "
        f"Trades {overall_base_oos['total_trades']:>2} | WR {overall_base_oos['win_rate_pct']:5.1f}% | PF {overall_base_oos['profit_factor']:4.2f} | "
        f"MaxDD {overall_base_oos['max_dd_pct']:4.1f}% | Sharpe {overall_base_oos['sharpe']:+5.2f}"
    )
    print("=" * 118 + "\n", flush=True)
    return {
        "folds": fold_reports,
        "cumulative_adaptive_oos": overall_oos,
        "cumulative_recommended_s5_oos": overall_s5_oos,
        "cumulative_static_s2_oos": overall_base_oos,
    }


def main():
    ap = argparse.ArgumentParser(description="Live Streaming Backtest & Walk-Forward Engine")
    ap.add_argument("--csv-glob", default=DEFAULT_LEADLAG_GLOB, help="Path glob to leadlag spot_ticks_*.csv")
    ap.add_argument("--cache-path", default=DEFAULT_CACHE_PATH, help="1-second resampled candle cache path")
    ap.add_argument("--coins", default="BTC,ETH", help="Comma-separated active coins (default: BTC,ETH)")
    ap.add_argument("--initial-balance", type=float, default=100.0, help="Starting bankroll ($)")
    ap.add_argument("--stream-delay-ms", type=float, default=0.0, help="Optional delay (ms) per candle for visual streaming")
    ap.add_argument("--stream-every", type=int, default=5, help="Print status line every N candles (trades always print live)")
    ap.add_argument("--folds", type=int, default=5, help="Number of rolling walk-forward folds")
    ap.add_argument("--rebuild-cache", action="store_true", help="Force rebuild of 1s candle cache from raw CSVs")
    args = ap.parse_args()

    coins = tuple(c.strip().upper() for c in args.coins.split(",") if c.strip())
    candles = build_or_load_1s_candles(
        csv_glob=args.csv_glob,
        cache_path=args.cache_path,
        coins=coins,
        force_rebuild=args.rebuild_cache,
        stream_progress=True,
    )

    strategies = get_strategy_suite()
    strat_results = run_streaming_backtest(
        candles=candles,
        strategies=strategies,
        initial_balance=args.initial_balance,
        stream_delay_ms=args.stream_delay_ms,
        stream_every_n_candles=args.stream_every,
    )

    wf_results = run_rolling_walk_forward(
        candles=candles,
        n_folds=args.folds,
        initial_balance=args.initial_balance,
    )

    out_payload = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "total_candles": len(candles),
        "coins": list(coins),
        "strategies": {k: v["metrics"] for k, v in strat_results.items()},
        "walk_forward": wf_results,
    }
    os.makedirs(os.path.dirname(DEFAULT_REPORT_JSON), exist_ok=True)
    with open(DEFAULT_REPORT_JSON, "w") as f:
        json.dump(out_payload, f, indent=2)
    print(f"💾 Saved full backtest & walk-forward JSON report to {DEFAULT_REPORT_JSON}")


if __name__ == "__main__":
    main()
