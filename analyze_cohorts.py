#!/usr/bin/env python3
"""
Comprehensive cohort analysis for Kalshi trading databases.
Computes trade breakdowns by cohort with all requested metrics.
"""

import sqlite3
import json
from datetime import datetime
from collections import defaultdict

# ============================================================
# COHORT BOUNDARIES (UTC timestamps)
# ============================================================
TS_1fd14ec = 1794799295.0   # 2026-11-16 03:21:35 UTC - cubic variance, maker TP, certainty arb, dynamic vol/rebalance
TS_14a29e7 = 1794844091.0   # 2026-11-16 15:48:11 UTC - 120-contract compounding, -35% hard stop, 15s grace

# Trial 1 archive: all trades (163-254) - single cohort

# ============================================================
# HELPER FUNCTIONS
# ============================================================
def bucket_entry_price(price):
    """Bucket entry price into ranges."""
    if price < 0.30: return "0.20-0.30"
    elif price < 0.40: return "0.30-0.40"
    elif price < 0.52: return "0.40-0.52"
    elif price < 0.65: return "0.52-0.65"
    else: return "0.65+"

def bucket_seconds_left(sec):
    """Bucket seconds left at entry."""
    if sec < 60: return "<60"
    elif sec < 120: return "60-120"
    elif sec < 180: return "120-180"
    elif sec < 300: return "180-300"
    elif sec < 450: return "300-450"
    else: return "450+"

def bucket_model_prob(prob):
    """Bucket model probability at entry."""
    if prob is None: return "unknown"
    if prob < 65: return "60-65"
    elif prob < 70: return "65-70"
    elif prob < 75: return "70-75"
    elif prob < 80: return "75-80"
    elif prob < 90: return "80-90"
    else: return "90+"

def bucket_contracts(contracts):
    """Bucket contract count."""
    if contracts < 40: return "<40"
    elif contracts < 50: return "40-49"
    elif contracts < 60: return "50-59"
    elif contracts < 80: return "60-79"
    elif contracts < 100: return "80-99"
    else: return "100+"

def compute_metrics(trades):
    """Compute comprehensive metrics for a list of trades."""
    if not trades:
        return None
    
    # Filter closed trades (have pnl)
    closed = [t for t in trades if t[5] is not None]  # pnl_dollars at index 5
    open_trades = [t for t in trades if t[5] is None]
    
    n = len(closed)
    if n == 0:
        return {"trade_count": 0, "open_count": len(open_trades)}
    
    # Basic stats
    total_pnl = sum(t[5] for t in closed)
    total_fees = sum(t[9] for t in closed)  # fee_dollars at index 9
    avg_pnl = total_pnl / n
    wins = [t for t in closed if t[5] > 0]
    losses = [t for t in closed if t[5] <= 0]
    win_rate = len(wins) / n * 100
    
    # Fee drag
    gross_pnl = total_pnl + total_fees
    fee_drag_pct = (total_fees / gross_pnl * 100) if gross_pnl != 0 else 0
    
    # Max drawdown (running equity curve)
    running = 0
    peak = 0
    max_dd = 0
    for t in closed:
        running += t[5]
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd
    
    # Per-asset breakdown (index 2 is coin or ticker)
    by_asset = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0, "fees": 0.0, "avg_pnl": 0.0})
    for t in closed:
        asset = t[2]  # coin or ticker at index 2
        # Normalize asset name
        if asset and 'BTC' in asset.upper():
            asset = 'BTC'
        elif asset and 'ETH' in asset.upper():
            asset = 'ETH'
        elif asset and 'SOL' in asset.upper():
            asset = 'SOL'
        elif asset and 'XRP' in asset.upper():
            asset = 'XRP'
        by_asset[asset]["trades"] += 1
        by_asset[asset]["pnl"] += t[5]
        by_asset[asset]["fees"] += t[9]
        if t[5] > 0:
            by_asset[asset]["wins"] += 1
    
    for asset in by_asset:
        d = by_asset[asset]
        d["win_rate"] = d["wins"] / d["trades"] * 100 if d["trades"] > 0 else 0
        d["avg_pnl"] = d["pnl"] / d["trades"] if d["trades"] > 0 else 0
    
    # Exit reason distribution
    exit_reasons = defaultdict(int)
    for t in closed:
        exit_reasons[t[6] or "UNKNOWN"] += 1  # exit_reason at index 6
    
    # Entry price distribution
    entry_price_dist = defaultdict(int)
    for t in closed:
        entry_price_dist[bucket_entry_price(t[3])] += 1  # entry_price at index 3
    
    # Seconds left at entry distribution
    sec_left_dist = defaultdict(int)
    for t in closed:
        sec_left_dist[bucket_seconds_left(t[8] or 0)] += 1  # seconds_left_at_entry at index 8
    
    # Model prob distribution
    model_prob_dist = defaultdict(int)
    for t in closed:
        model_prob_dist[bucket_model_prob(t[7])] += 1  # model_prob_entry at index 7
    
    # Contracts distribution
    contracts_dist = defaultdict(int)
    for t in closed:
        contracts_dist[bucket_contracts(t[4])] += 1  # contracts at index 4
    
    return {
        "trade_count": n,
        "open_count": len(open_trades),
        "win_rate": round(win_rate, 2),
        "total_pnl": round(total_pnl, 2),
        "avg_pnl": round(avg_pnl, 2),
        "total_fees": round(total_fees, 2),
        "fee_drag_pct": round(fee_drag_pct, 2),
        "max_drawdown": round(max_dd, 2),
        "per_asset": {k: {kk: round(vv, 2) if isinstance(vv, float) else vv for kk, vv in v.items()} for k, v in by_asset.items()},
        "exit_reasons": dict(exit_reasons),
        "entry_price_distribution": dict(entry_price_dist),
        "seconds_left_distribution": dict(sec_left_dist),
        "model_prob_distribution": dict(model_prob_dist),
        "contracts_distribution": dict(contracts_dist),
    }

def analyze_db(db_path, cohort_boundaries=None, db_name=""):
    """Analyze a database and return cohort metrics."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Check if 'coin' column exists
    cursor.execute("PRAGMA table_info(trades)")
    cols = [row[1] for row in cursor.fetchall()]
    has_coin = 'coin' in cols
    
    if has_coin:
        cursor.execute('''
            SELECT id, timestamp, coin, entry_price, contracts, pnl_dollars, 
                   exit_reason, model_prob_entry, seconds_left_at_entry, fee_dollars
            FROM trades ORDER BY id
        ''')
    else:
        # For dbs without coin column, use ticker/series to infer
        cursor.execute('''
            SELECT id, timestamp, ticker, entry_price, contracts, pnl_dollars, 
                   exit_reason, model_prob_entry, seconds_left_at_entry, fee_dollars
            FROM trades ORDER BY id
        ''')
    trades = cursor.fetchall()
    conn.close()
    
    print(f"{db_name}: {len(trades)} total trades")
    
    if len(trades) == 0:
        if cohort_boundaries:
            return {name: None for name, _ in cohort_boundaries}
        return {"all": None}
    
    if cohort_boundaries:
        # Split by cohort boundaries (list of (name, max_timestamp) tuples)
        cohorts = defaultdict(list)
        for t in trades:
            assigned = False
            for name, max_ts in cohort_boundaries:
                if t[1] < max_ts:
                    cohorts[name].append(t)
                    assigned = True
                    break
            if not assigned:
                cohorts[cohort_boundaries[-1][0]].append(t)
        
        results = {}
        for name, cohort_trades in cohorts.items():
            if cohort_trades:
                print(f"  Cohort {name}: {len(cohort_trades)} trades (IDs {cohort_trades[0][0]}-{cohort_trades[-1][0]})")
                results[name] = compute_metrics(cohort_trades)
            else:
                results[name] = None
        return results
    else:
        # Single cohort (all trades)
        return {"all": compute_metrics(trades)}

def analyze_kalshi_audit(db_path):
    """Analyze kalshi_audit.db for kill switch, OMS_ROLLBACK, portfolio snapshots."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Trade events - kill switch and OMS_ROLLBACK
    cursor.execute("SELECT event_type, timestamp, payload_json FROM trade_events WHERE event_type IN ('kill_switch', 'halt', 'OMS_ROLLBACK', 'risk_halt') ORDER BY timestamp")
    events = cursor.fetchall()
    
    # Also check for any event containing kill_switch or rollback in payload
    cursor.execute("SELECT event_type, timestamp, payload_json FROM trade_events ORDER BY timestamp")
    all_events = cursor.fetchall()
    
    kill_switch_events = []
    oms_rollback_events = []
    other_events = []
    
    for ev in all_events:
        ev_type, ts, payload = ev
        payload_lower = payload.lower() if payload else ""
        if 'kill_switch' in payload_lower or 'halt' in ev_type.lower() or 'halt' in payload_lower:
            kill_switch_events.append({"event_type": ev_type, "timestamp": ts, "payload": payload})
        elif 'rollback' in payload_lower or 'OMS_ROLLBACK' in ev_type:
            oms_rollback_events.append({"event_type": ev_type, "timestamp": ts, "payload": payload})
        else:
            other_events.append({"event_type": ev_type, "timestamp": ts, "payload": payload})
    
    # Portfolio snapshots timeline
    cursor.execute("SELECT timestamp, cash, pos_val, equity, realized, unrealized, win_rate, trades_count FROM portfolio_snapshots ORDER BY timestamp")
    portfolio_snapshots = cursor.fetchall()
    
    conn.close()
    
    return {
        "kill_switch_events": kill_switch_events,
        "oms_rollback_events": oms_rollback_events,
        "portfolio_snapshots": [
            {
                "timestamp": ts,
                "cash": cash,
                "pos_val": pos_val,
                "equity": equity,
                "realized": realized,
                "unrealized": unrealized,
                "win_rate": win_rate,
                "trades_count": trades_count
            }
            for ts, cash, pos_val, equity, realized, unrealized, win_rate, trades_count in portfolio_snapshots
        ],
        "total_events": len(all_events),
        "other_event_types": list(set(e[0] for e in all_events))
    }

# ============================================================
# MAIN ANALYSIS
# ============================================================
if __name__ == "__main__":
    results = {}
    
    # 1. Trial 2 main DB (multi_coin_paper.db) - Cohorts B, C, D
    print("\n=== Trial 2: multi_coin_paper.db ===")
    cohort_bounds = [
        ("B", TS_1fd14ec),   # Pre-1fd14ec
        ("C", TS_14a29e7),   # Post-1fd14ec, pre-14a29e7
        ("D", float('inf'))  # Post-14a29e7
    ]
    results["trial2_multi_coin_paper"] = analyze_db(
        "/home/jooshoo/kalshi-btc-scanner-clean/multi_coin_paper.db",
        cohort_bounds,
        "Trial 2"
    )
    
    # 2. Trial 1 archive DB
    print("\n=== Trial 1: archive/trial1/multi_coin_paper.db ===")
    results["trial1_archive"] = analyze_db(
        "/home/jooshoo/kalshi-btc-scanner-clean/archive/trial1/multi_coin_paper.db",
        None,
        "Trial 1 Archive"
    )
    
    # 3. kalshi_audit.db
    print("\n=== kalshi_audit.db ===")
    results["kalshi_audit"] = analyze_kalshi_audit("/home/jooshoo/kalshi-btc-scanner-clean/kalshi_audit.db")
    
    # 4. kalshi_live_demo.db
    print("\n=== kalshi_live_demo.db ===")
    results["kalshi_live_demo"] = analyze_db(
        "/home/jooshoo/kalshi-btc-scanner-clean/kalshi_live_demo.db",
        None,
        "Live Demo"
    )
    
    # Save results as JSON
    with open("/home/jooshoo/kalshi-btc-scanner-clean/cohort_analysis_results.json", "w") as f:
        json.dump(results, f, indent=2, default=str)
    
    print("\n=== Results saved to cohort_analysis_results.json ===")
    
    # Print summary
    print("\n=== SUMMARY ===")
    for db_name, db_results in results.items():
        if db_name in ["kalshi_audit"]:
            continue
        print(f"\n{db_name}:")
        for cohort, metrics in db_results.items():
            if metrics and metrics.get("trade_count", 0) > 0:
                print(f"  Cohort {cohort}: {metrics['trade_count']} trades, WR={metrics['win_rate']}%, PnL=${metrics['total_pnl']}, Fees=${metrics['total_fees']}, MaxDD=${metrics['max_drawdown']}")
            elif metrics:
                print(f"  Cohort {cohort}: {metrics['open_count']} open trades only")
    
    # Print kalshi_audit summary
    audit = results["kalshi_audit"]
    print(f"\nkalshi_audit.db:")
    print(f"  Kill switch/halt events: {len(audit['kill_switch_events'])}")
    print(f"  OMS_ROLLBACK events: {len(audit['oms_rollback_events'])}")
    print(f"  Portfolio snapshots: {len(audit['portfolio_snapshots'])}")
    print(f"  Event types: {audit['other_event_types']}")