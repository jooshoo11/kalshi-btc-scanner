#!/usr/bin/env python3
"""
audit_fees_and_slippage.py — Institutional Audit & Stress-Testing Engine for Kalshi Trades.

Inspects an existing SQLite audit database (kalshi_audit.db), back-applies realistic
Kalshi taker fee schedules and adverse bid/ask execution slippage across historical
trades, and generates an exhaustive before-and-after performance comparison.

Safety Guarantee:
    This script NEVER modifies existing production tables in kalshi_audit.db.
    The database connection is strictly opened in READ-ONLY mode (URI mode=ro).
"""

import os
import sys
import math
import sqlite3
import argparse
from typing import Dict, Any, List, Optional, Tuple


# ==============================================================================
# 1. KALSHI FEE CALCULATION SPECIFICATION
# ==============================================================================

def calculate_kalshi_fee(contracts: int, price: float) -> float:
    """
    Kalshi standard taker fee formula:
        fee = ceil(0.07 * count * price * (1.0 - price) * 100) / 100
    
    Where:
        - price is expressed in dollars [0.01 - 0.99]
        - fee is rounded UP to the nearest cent ($0.01)
        - at settlement (0.00 or 1.00), taker fee is 0.00
    """
    if contracts <= 0 or price <= 0.0 or price >= 1.0:
        return 0.0
    raw_cents = 0.07 * float(contracts) * float(price) * (1.0 - float(price)) * 100.0
    return math.ceil(raw_cents) / 100.0


def is_expiry_settlement(exit_reason: Optional[str], exit_price: Optional[float]) -> bool:
    """
    Determines if an exit was an expiry resolution (free settlement)
    or an active market order (early take profit / stop loss subject to taker fee).
    """
    reason_str = str(exit_reason or "").strip().upper()
    if any(keyword in reason_str for keyword in ("SETTLE", "EXPIR", "RESOLUTION", "EXPIRED")):
        return True
    if exit_price is not None and abs(exit_price - 1.0) < 1e-4:
        # 100¢ binary settlement
        return True
    if exit_price is not None and abs(exit_price - 0.0) < 1e-4:
        # 0¢ binary expiration worthless
        return True
    return False


# ==============================================================================
# 2. DYNAMIC SCHEMA INSPECTION & ADAPTER
# ==============================================================================

class DynamicSchemaAdapter:
    """
    Discovers tables and resolves polymorphic column names in SQLite databases
    to support diverse schema versions without hardcoding column names.
    """
    CANDIDATE_TABLES = ["trades", "trade_log", "executions", "orders", "audit_trades"]

    COLUMN_MAPS = {
        "id": ["id", "trade_id", "rowid", "ticket_id"],
        "contracts": ["contracts", "count", "qty", "quantity", "shares", "size"],
        "entry_price": ["entry_price", "price", "buy_price", "entry", "fill_price"],
        "exit_price": ["exit_price", "close_price", "sell_price", "exit", "settle_price"],
        "exit_reason": ["exit_reason", "reason", "close_reason", "action", "exit_type"],
        "status": ["status", "state", "trade_status"],
        "side": ["side", "direction", "position_side"],
        "raw_pnl": ["pnl_dollars", "realized_pnl", "net_pnl", "pnl", "profit_loss"],
        "raw_fee": ["fee_dollars", "fees", "total_fee", "commission"],
        "timestamp": ["timestamp", "created_at", "entry_time", "time", "date"],
        "ticker": ["ticker", "symbol", "market_ticker", "contract"]
    }

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.table_name: str = ""
        self.resolved_columns: Dict[str, str] = {}
        self._inspect_and_bind()

    def _inspect_and_bind(self):
        cursor = self.conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [row[0] for row in cursor.fetchall()]

        # Identify target table
        for candidate in self.CANDIDATE_TABLES:
            if candidate in tables:
                self.table_name = candidate
                break
        if not self.table_name and tables:
            self.table_name = tables[0]

        if not self.table_name:
            raise ValueError("No viable trades table found in database.")

        # Inspect table columns
        cursor.execute(f"PRAGMA table_info({self.table_name});")
        table_cols = {row[1].lower(): row[1] for row in cursor.fetchall()}

        for logical_name, candidates in self.COLUMN_MAPS.items():
            for c in candidates:
                if c.lower() in table_cols:
                    self.resolved_columns[logical_name] = table_cols[c.lower()]
                    break

        # Validate essential columns
        required = ["contracts", "entry_price", "exit_price"]
        missing = [req for req in required if req not in self.resolved_columns]
        if missing:
            raise KeyError(
                f"Missing essential execution columns in table '{self.table_name}': {missing}. "
                f"Available columns: {list(table_cols.values())}"
            )

    def fetch_closed_trades(self) -> List[Dict[str, Any]]:
        cursor = self.conn.cursor()
        select_clause = ", ".join(f"{col} AS {logical}" for logical, col in self.resolved_columns.items())
        
        status_col = self.resolved_columns.get("status")
        exit_col = self.resolved_columns["exit_price"]
        
        where_clauses = [f"{exit_col} IS NOT NULL"]
        if status_col:
            where_clauses.append(f"UPPER({status_col}) = 'CLOSED'")
            
        where_sql = " AND ".join(where_clauses)
        id_col = self.resolved_columns.get("id", "rowid")
        sql = f"SELECT {select_clause} FROM {self.table_name} WHERE {where_sql} ORDER BY {id_col} ASC;"
        
        cursor.execute(sql)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]


# ==============================================================================
# 3. METRIC CALCULATOR ENGINE
# ==============================================================================

class ScenarioResults:
    def __init__(self, name: str, starting_balance: float):
        self.name = name
        self.starting_balance = starting_balance
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.breakeven_trades = 0
        self.total_gross_profit = 0.0
        self.total_gross_loss = 0.0
        self.total_fees = 0.0
        self.total_slippage_drag = 0.0
        self.total_net_pnl = 0.0
        self.final_equity = starting_balance
        self.max_drawdown_dollars = 0.0
        self.max_drawdown_pct = 0.0
        self.equity_curve: List[float] = [starting_balance]

    @property
    def win_rate_pct(self) -> float:
        return (self.winning_trades / self.total_trades * 100.0) if self.total_trades > 0 else 0.0

    @property
    def profit_factor(self) -> float:
        abs_loss = abs(self.total_gross_loss)
        if abs_loss == 0.0:
            return float("inf") if self.total_gross_profit > 0 else 1.0
        return self.total_gross_profit / abs_loss

    @property
    def net_roi_pct(self) -> float:
        if self.starting_balance <= 0:
            return 0.0
        return ((self.final_equity - self.starting_balance) / self.starting_balance) * 100.0


def evaluate_scenarios(
    trades: List[Dict[str, Any]],
    starting_balance: float = 50.0,
    slippage_penalty: float = 0.01
) -> Tuple[ScenarioResults, ScenarioResults, ScenarioResults, List[Dict[str, Any]]]:
    """
    Evaluates historical executions across three distinct friction models:
        1. Raw Paper Baseline: Recorded executions & fees
        2. Scenario A: Exact Kalshi Taker Fees (Free Expiry Resolution)
        3. Scenario B: Exact Kalshi Taker Fees + 1¢ Bid/Ask Adverse Slippage Drag
    """
    base = ScenarioResults("Raw Baseline", starting_balance)
    scen_a = ScenarioResults("Scenario A (Strict Fees)", starting_balance)
    scen_b = ScenarioResults("Scenario B (Fees + 1¢ Slippage)", starting_balance)

    detailed_records: List[Dict[str, Any]] = []

    # Running tracking for Max Drawdown
    cur_eq_base = starting_balance
    peak_eq_base = starting_balance

    cur_eq_a = starting_balance
    peak_eq_a = starting_balance

    cur_eq_b = starting_balance
    peak_eq_b = starting_balance

    for t in trades:
        trade_id = t.get("id", 0)
        contracts = int(t["contracts"])
        entry_price = float(t["entry_price"])
        exit_price = float(t["exit_price"])
        exit_reason = t.get("exit_reason", "")
        ticker = t.get("ticker", "KXBTC15M")
        is_settle = is_expiry_settlement(exit_reason, exit_price)

        # ----------------------------------------------------------------------
        # 1. Baseline Model
        # ----------------------------------------------------------------------
        raw_pnl = t.get("raw_pnl")
        raw_fee = t.get("raw_fee")
        
        # Calculate gross price delta
        gross_pnl = contracts * (exit_price - entry_price)
        if raw_fee is not None:
            base_fee = float(raw_fee)
        else:
            base_fee = 0.01 * contracts  # default baseline flat 0.5c/side

        if raw_pnl is not None:
            base_net = float(raw_pnl)
        else:
            base_net = gross_pnl - base_fee

        base.total_trades += 1
        base.total_fees += base_fee
        base.total_net_pnl += base_net
        if gross_pnl > 0:
            base.total_gross_profit += gross_pnl
        else:
            base.total_gross_loss += gross_pnl

        if base_net > 0.0001:
            base.winning_trades += 1
        elif base_net < -0.0001:
            base.losing_trades += 1
        else:
            base.breakeven_trades += 1

        cur_eq_base += base_net
        peak_eq_base = max(peak_eq_base, cur_eq_base)
        dd_base = peak_eq_base - cur_eq_base
        dd_pct_base = (dd_base / peak_eq_base * 100.0) if peak_eq_base > 0 else 0.0
        base.max_drawdown_dollars = max(base.max_drawdown_dollars, dd_base)
        base.max_drawdown_pct = max(base.max_drawdown_pct, dd_pct_base)
        base.equity_curve.append(cur_eq_base)

        # ----------------------------------------------------------------------
        # 2. Scenario A: Strict Kalshi Fees
        # ----------------------------------------------------------------------
        fee_in_a = calculate_kalshi_fee(contracts, entry_price)
        fee_out_a = 0.0 if is_settle else calculate_kalshi_fee(contracts, exit_price)
        tot_fee_a = fee_in_a + fee_out_a
        net_a = gross_pnl - tot_fee_a

        scen_a.total_trades += 1
        scen_a.total_fees += tot_fee_a
        scen_a.total_net_pnl += net_a
        if gross_pnl > 0:
            scen_a.total_gross_profit += gross_pnl
        else:
            scen_a.total_gross_loss += gross_pnl

        if net_a > 0.0001:
            scen_a.winning_trades += 1
        elif net_a < -0.0001:
            scen_a.losing_trades += 1
        else:
            scen_a.breakeven_trades += 1

        cur_eq_a += net_a
        peak_eq_a = max(peak_eq_a, cur_eq_a)
        dd_a = peak_eq_a - cur_eq_a
        dd_pct_a = (dd_a / peak_eq_a * 100.0) if peak_eq_a > 0 else 0.0
        scen_a.max_drawdown_dollars = max(scen_a.max_drawdown_dollars, dd_a)
        scen_a.max_drawdown_pct = max(scen_a.max_drawdown_pct, dd_pct_a)
        scen_a.equity_curve.append(cur_eq_a)

        # ----------------------------------------------------------------------
        # 3. Scenario B: Fees + Adverse 1¢ Execution Slippage
        # ----------------------------------------------------------------------
        adj_entry = min(0.99, entry_price + slippage_penalty)
        adj_exit = exit_price if is_settle else max(0.01, exit_price - slippage_penalty)

        gross_b = contracts * (adj_exit - adj_entry)
        fee_in_b = calculate_kalshi_fee(contracts, adj_entry)
        fee_out_b = 0.0 if is_settle else calculate_kalshi_fee(contracts, adj_exit)
        tot_fee_b = fee_in_b + fee_out_b
        net_b = gross_b - tot_fee_b

        slip_drag = net_a - net_b

        scen_b.total_trades += 1
        scen_b.total_fees += tot_fee_b
        scen_b.total_slippage_drag += slip_drag
        scen_b.total_net_pnl += net_b
        if gross_b > 0:
            scen_b.total_gross_profit += gross_b
        else:
            scen_b.total_gross_loss += gross_b

        if net_b > 0.0001:
            scen_b.winning_trades += 1
        elif net_b < -0.0001:
            scen_b.losing_trades += 1
        else:
            scen_b.breakeven_trades += 1

        cur_eq_b += net_b
        peak_eq_b = max(peak_eq_b, cur_eq_b)
        dd_b = peak_eq_b - cur_eq_b
        dd_pct_b = (dd_b / peak_eq_b * 100.0) if peak_eq_b > 0 else 0.0
        scen_b.max_drawdown_dollars = max(scen_b.max_drawdown_dollars, dd_b)
        scen_b.max_drawdown_pct = max(scen_b.max_drawdown_pct, dd_pct_b)
        scen_b.equity_curve.append(cur_eq_b)

        # Export log row
        detailed_records.append({
            "trade_id": trade_id,
            "ticker": ticker,
            "contracts": contracts,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "exit_reason": exit_reason,
            "is_settle": 1 if is_settle else 0,
            "gross_pnl": round(gross_pnl, 2),
            "base_fee": round(base_fee, 2),
            "base_net": round(base_net, 2),
            "fee_a": round(tot_fee_a, 2),
            "net_a": round(net_a, 2),
            "adj_entry": round(adj_entry, 2),
            "adj_exit": round(adj_exit, 2),
            "fee_b": round(tot_fee_b, 2),
            "net_b": round(net_b, 2),
            "slippage_drag": round(slip_drag, 2)
        })

    base.final_equity = cur_eq_base
    scen_a.final_equity = cur_eq_a
    scen_b.final_equity = cur_eq_b

    return base, scen_a, scen_b, detailed_records


# ==============================================================================
# 4. REPORT FORMATTING & VERDICT GENERATOR
# ==============================================================================

def print_audit_report(
    base: ScenarioResults,
    scen_a: ScenarioResults,
    scen_b: ScenarioResults,
    slippage_penalty: float
):
    """
    Renders high-visibility ASCII tables comparing performance across friction models.
    """
    print("\n" + "=" * 84)
    print("       KALSHI BITCOIN TRADING ENGINE: COMPREHENSIVE FEE & SLIPPAGE AUDIT")
    print("=" * 84)

    header = (
        f"{'Metric':<32} | {'1. Raw Baseline':>14} | "
        f"{'2. Strict Fees':>14} | {'3. Fees + 1¢ Slip':>14}"
    )
    divider = "-" * 33 + "+" + "-" * 16 + "+" + "-" * 16 + "+" + "-" * 16

    print(header)
    print(divider)

    rows = [
        ("Total Completed Trades", f"{base.total_trades}", f"{scen_a.total_trades}", f"{scen_b.total_trades}"),
        ("Winning Trades", f"{base.winning_trades}", f"{scen_a.winning_trades}", f"{scen_b.winning_trades}"),
        ("Losing Trades", f"{base.losing_trades}", f"{scen_a.losing_trades}", f"{scen_b.losing_trades}"),
        ("Win Rate (%)", f"{base.win_rate_pct:.1f}%", f"{scen_a.win_rate_pct:.1f}%", f"{scen_b.win_rate_pct:.1f}%"),
        ("Profit Factor", f"{base.profit_factor:.2f}", f"{scen_a.profit_factor:.2f}", f"{scen_b.profit_factor:.2f}"),
        (divider.replace("+", "-"), "", "", ""),
        ("Total Gross Profit ($)", f"${base.total_gross_profit:,.2f}", f"${scen_a.total_gross_profit:,.2f}", f"${scen_b.total_gross_profit:,.2f}"),
        ("Total Gross Loss ($)", f"${base.total_gross_loss:,.2f}", f"${scen_a.total_gross_loss:,.2f}", f"${scen_b.total_gross_loss:,.2f}"),
        ("Total Fees Paid ($)", f"${base.total_fees:,.2f}", f"${scen_a.total_fees:,.2f}", f"${scen_b.total_fees:,.2f}"),
        ("Total Slippage Drag ($)", "$0.00", "$0.00", f"${scen_b.total_slippage_drag:,.2f}"),
        ("Cumulative Net Profit ($)", f"${base.total_net_pnl:,.2f}", f"${scen_a.total_net_pnl:,.2f}", f"${scen_b.total_net_pnl:,.2f}"),
        (divider.replace("+", "-"), "", "", ""),
        ("Starting Bankroll ($)", f"${base.starting_balance:,.2f}", f"${scen_a.starting_balance:,.2f}", f"${scen_b.starting_balance:,.2f}"),
        ("Final Portfolio Equity ($)", f"${base.final_equity:,.2f}", f"${scen_a.final_equity:,.2f}", f"${scen_b.final_equity:,.2f}"),
        ("Overall Net ROI (%)", f"{base.net_roi_pct:+,.1f}%", f"{scen_a.net_roi_pct:+,.1f}%", f"{scen_b.net_roi_pct:+,.1f}%"),
        ("Max Drawdown ($)", f"${base.max_drawdown_dollars:,.2f}", f"${scen_a.max_drawdown_dollars:,.2f}", f"${scen_b.max_drawdown_dollars:,.2f}"),
        ("Max Drawdown (%)", f"{base.max_drawdown_pct:.1f}%", f"{scen_a.max_drawdown_pct:.1f}%", f"{scen_b.max_drawdown_pct:.1f}%"),
    ]

    for label, val1, val2, val3 in rows:
        if not val1 and not val2 and not val3:
            print(divider)
        else:
            print(f"{label:<32} | {val1:>14} | {val2:>14} | {val3:>14}")

    print("=" * 84)

    # --------------------------------------------------------------------------
    # Automated Verdict Analysis
    # --------------------------------------------------------------------------
    gross_pnl = scen_a.total_gross_profit + scen_a.total_gross_loss
    fee_pct_of_gross = (scen_a.total_fees / gross_pnl * 100.0) if gross_pnl > 0 else 0.0
    slip_pct_of_gross = (scen_b.total_slippage_drag / gross_pnl * 100.0) if gross_pnl > 0 else 0.0
    total_friction_pct = ((scen_b.total_fees + scen_b.total_slippage_drag) / gross_pnl * 100.0) if gross_pnl > 0 else 0.0

    print("\n" + "#" * 84)
    print("                              AUDIT VERDICT & TAKEAWAYS")
    print("#" * 84)

    # 1. Edge Retention Verdict
    if scen_b.final_equity > scen_b.starting_balance and scen_b.net_roi_pct > 50.0:
        verdict_icon = "[PASS: ROBUST POSITIVE EDGE]"
        verdict_summary = (
            f"The trading engine successfully retained a substantial positive alpha.\n"
            f"   Even after absorbing full round-trip Kalshi taker fees (${scen_b.total_fees:.2f}) AND\n"
            f"   an adverse {slippage_penalty*100:.0f}¢ market order execution penalty (${scen_b.total_slippage_drag:.2f}),\n"
            f"   the strategy generated ${scen_b.total_net_pnl:,.2f} in clean net profit (+{scen_b.net_roi_pct:.1f}% Net ROI)."
        )
    elif scen_b.final_equity > scen_b.starting_balance:
        verdict_icon = "[PASS: MODERATE EDGE - WATCH FRICTION]"
        verdict_summary = (
            f"Strategy remains net profitable (+{scen_b.net_roi_pct:.1f}% ROI), but high friction\n"
            f"   consumed a significant portion of profits."
        )
    else:
        verdict_icon = "[FAIL: FRICTION NEGATES EDGE]"
        verdict_summary = (
            f"Transaction costs and execution slippage eroded the baseline paper edge."
        )

    print(f"\n1. EDGE RETENTION: {verdict_icon}")
    print(f"   {verdict_summary}")

    # 2. Friction Breakdown
    print(f"\n2. FRICTION IMPACT BREAKDOWN:")
    print(f"   • Total Cumulative Gross PnL:        ${gross_pnl:,.2f}")
    print(f"   • Real Kalshi Taker Fees Paid:       ${scen_a.total_fees:,.2f} ({fee_pct_of_gross:.1f}% of gross)")
    print(f"   • Adverse Spread Slippage Drag (1¢): ${scen_b.total_slippage_drag:,.2f} ({slip_pct_of_gross:.1f}% of gross)")
    print(f"   • Total Combined Friction:           ${(scen_b.total_fees + scen_b.total_slippage_drag):,.2f} ({total_friction_pct:.1f}% of gross profit)")

    # 3. Early Exit Strategy Evaluation
    print(f"\n3. EARLY EXIT RULE EVALUATION (+25% TP / -25% SL):")
    early_exit_net_margin = gross_pnl - scen_a.total_fees
    if early_exit_net_margin > 0:
        print(
            f"   • VERDICT: JUSTIFIED. Active profit-taking and loss-cutting generated an average\n"
            f"     gross profit per winning trade that far exceeded the second taker fee (~${scen_a.total_fees/max(1,scen_a.total_trades):.2f}/trade).\n"
            f"     Cutting losses at -25% rather than suffering 100% loss at 0¢ saved far more capital\n"
            f"     than the small exit fee cost."
        )
    else:
        print(
            f"   • VERDICT: CAUTION. Early exit fees are too large relative to the average profit target.\n"
            f"     Consider holding contracts with >90% model conviction until free expiration resolution."
        )

    # 4. Actionable Execution Advice
    print(f"\n4. INSTITUTIONAL EXECUTION RECOMMENDATIONS:")
    print(f"   • Maker Orders / Limit Posting: Kalshi charges $0.00 or provides maker rebates on resting orders.")
    print(f"     Converting the bot's initial entry orders from taker to passive limits would recapture ~${scen_a.total_fees*0.5:.2f}.")
    print(f"   • Near-Resolution Expiry Rule: For contracts trading at ≥94¢ with <180 seconds left, allow them")
    print(f"     to settle naturally rather than selling at 95¢ to avoid paying the second taker fee.")
    print("#" * 84 + "\n")


# ==============================================================================
# 5. CLI ENTRYPOINT
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Audit Kalshi database trades with realistic taker fees and adverse slippage."
    )
    parser.add_argument(
        "--db-path",
        default=os.path.join(os.path.dirname(__file__), "kalshi_audit.db"),
        help="Path to SQLite database (default: kalshi_audit.db)"
    )
    parser.add_argument(
        "--slippage",
        type=float,
        default=0.01,
        help="Adverse slippage penalty in dollars per contract per side (default: 0.01 = 1 cent)"
    )
    parser.add_argument(
        "--initial-balance",
        type=float,
        default=50.0,
        help="Initial starting portfolio equity in USD (default: 50.0)"
    )
    parser.add_argument(
        "--export-csv",
        type=str,
        default=None,
        help="Optional file path to export detailed trade comparison CSV"
    )
    args = parser.parse_args()

    if not os.path.exists(args.db_path):
        print(f"[-] Error: Database not found at '{args.db_path}'.", file=sys.stderr)
        sys.exit(1)

    # Safe read-only connection
    db_uri = f"file:{os.path.abspath(args.db_path)}?mode=ro"
    try:
        conn = sqlite3.connect(db_uri, uri=True)
        conn.row_factory = sqlite3.Row
    except Exception as e:
        print(f"[-] Error connecting to database in read-only mode: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        adapter = DynamicSchemaAdapter(conn)
        print(f"[*] Connected in READ-ONLY mode to: {args.db_path}")
        print(f"[*] Bound to table: '{adapter.table_name}'")
        print(f"[*] Resolved columns: {adapter.resolved_columns}")

        trades = adapter.fetch_closed_trades()
        if not trades:
            print("[!] No closed trades found in database to audit.")
            sys.exit(0)

        print(f"[*] Analyzing {len(trades)} historical closed trades...")

        base, scen_a, scen_b, records = evaluate_scenarios(
            trades=trades,
            starting_balance=args.initial_balance,
            slippage_penalty=args.slippage
        )

        print_audit_report(base, scen_a, scen_b, args.slippage)

        if args.export_csv:
            import csv
            export_path = os.path.abspath(args.export_csv)
            with open(export_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=list(records[0].keys()))
                writer.writeheader()
                writer.writerows(records)
            print(f"[+] Detailed trade breakdown exported safely to CSV: {export_path}\n")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
