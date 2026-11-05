"""
core/portfolio_state.py — SHARED portfolio-wide risk state (agreed plan, phase 3).

One state file (portfolio_state.json at repo root) holds the portfolio-wide
day-start equity and daily-loss tripped flag, shared by ALL engines
(multi-coin + legacy). Baseline is initialized ONLY from confirmed-settlement
equity (initial balance + SUM of CLOSED pnl where exit_reason does NOT start
with 'SETTLED_UNKNOWN'/'SETTLED_ESTIMATED'), never from raw OPEN trades rows
(agreed condition 5).

Engines register equity providers so the daily-loss gate can evaluate the
COMBINED equity of every engine at once, not just one engine in isolation.
"""
import json
import os
import sqlite3
import time
from typing import Any, Callable, Dict, List, Optional

# State file lives at the repo root (next to the SQLite ledgers), not inside core/
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.environ.get("PORTFOLIO_STATE_FILE") or os.path.join(_REPO_ROOT, "portfolio_state.json")

# Exit reasons that must NOT feed the baseline or any win-rate/PnL stats until confirmed
UNCONFIRMED_PREFIXES = ("SETTLED_UNKNOWN", "SETTLED_ESTIMATED")

# Registered callables returning one engine's current equity (float)
_equity_providers: List[Callable[[], float]] = []


def register_equity_provider(fn: Callable[[], float]) -> None:
    if fn not in _equity_providers:
        _equity_providers.append(fn)


def combined_equity() -> float:
    """Sum of every registered engine's current equity."""
    total = 0.0
    for fn in list(_equity_providers):
        try:
            total += float(fn() or 0.0)
        except Exception:
            pass
    return round(total, 2)


def confirmed_equity_from_db(db_path: str, initial_balance: float) -> float:
    """initial_balance + SUM(pnl) over CLOSED trades with confirmed (non-estimated) settlement."""
    if not os.path.exists(db_path):
        return round(initial_balance, 2)
    with sqlite3.connect(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COALESCE(SUM(pnl_dollars), 0.0) FROM trades "
            "WHERE status='CLOSED' AND pnl_dollars IS NOT NULL "
            "AND (exit_reason IS NULL OR (exit_reason NOT LIKE 'SETTLED_UNKNOWN%' AND exit_reason NOT LIKE 'SETTLED_ESTIMATED%'))"
        )
        row = cur.fetchone()
        return round(initial_balance + float(row[0] or 0.0), 2)


def load_state() -> Dict[str, Any]:
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_state(state: Dict[str, Any]) -> None:
    try:
        state["timestamp"] = time.time()
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, STATE_FILE)  # atomic
    except Exception:
        pass


def init_baseline(engines: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """
    Initialize the shared day-start baseline from confirmed-settlement equity only.
    engines: {name: {"db_path": str, "initial_balance": float}}.
    Re-initializes only when the calendar day changes; same-day calls are no-ops
    that just return the current state (so it is safe to call every 60s).
    """
    today = time.strftime("%Y-%m-%d")
    state = load_state()
    if state.get("day_start_date") != today:
        per_engine = {}
        baseline = 0.0
        for name, cfg in engines.items():
            eq = confirmed_equity_from_db(cfg["db_path"], cfg["initial_balance"])
            per_engine[name] = {"confirmed_equity": eq}
            baseline += eq
        state = {
            "day_start_date": today,
            "day_start_equity": round(baseline, 2),
            "daily_loss_tripped": False,
            "per_engine": per_engine,
        }
        save_state(state)
    return state


def check_portfolio_daily_loss(current_combined_equity: Optional[float] = None, max_daily_loss_pct: float = 10.0) -> Optional[str]:
    """
    Portfolio-wide daily loss gate shared by all engines. If current_combined_equity
    is None, evaluates combined_equity() from the registered providers.
    Returns a reason string if tripped (and latches the flag), else None.
    """
    if current_combined_equity is None:
        if not _equity_providers:
            return None  # no engines registered -> no portfolio visibility -> don't block
        current_combined_equity = combined_equity()
    state = load_state()
    today = time.strftime("%Y-%m-%d")
    if state.get("day_start_date") != today:
        return None  # not initialized yet this day; init_baseline will set it
    if state.get("daily_loss_tripped"):
        return f"PORTFOLIO daily loss limit already tripped today (day-start ${state.get('day_start_equity')})"
    base = float(state.get("day_start_equity") or 0.0)
    if base <= 0:
        return None
    loss_pct = ((base - current_combined_equity) / base) * 100.0
    if loss_pct >= max_daily_loss_pct:
        state["daily_loss_tripped"] = True
        state["tripped_at_equity"] = round(current_combined_equity, 2)
        state["tripped_loss_pct"] = round(loss_pct, 2)
        save_state(state)
        return (f"PORTFOLIO daily loss limit: {loss_pct:.1f}% >= {max_daily_loss_pct}% "
                f"(day-start ${base:.2f} -> ${current_combined_equity:.2f})")
    return None
