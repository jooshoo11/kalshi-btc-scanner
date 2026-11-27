#!/usr/bin/env python3
"""
scripts/tui_dashboard.py — Enhanced Color-Terminal UI for Kalshi Live Trading.

Plain-text terminal dashboard with full ANSI color support, no curses dependency.
Works in any shell terminal with 256/truecolor support.

Usage:
    python3 scripts/tui_dashboard.py [--base-url http://localhost:8088]

Controls:
    Ctrl-C  Quit
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone

import httpx


# ─── Color helpers ────────────────────────────────────────────────────────────

# 6-high-intensity + 1-white palette, plus bold/dim modifiers
COLORS = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "black": "\033[30m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "white": "\033[37m",
    "hr": "\033[91m",   # high-intensity red
    "hg": "\033[92m",   # high-intensity green
    "hy": "\033[93m",   # high-intensity yellow
    "hb": "\033[94m",   # high-intensity blue
    "hm": "\033[95m",   # high-intensity magenta
    "hc": "\033[96m",   # high-intensity cyan
    "hw": "\033[97m",   # high-intensity white
}


def tty(text: str) -> str:
    """Wrap text with color. Safe to call even when stdout is not a TTY
    (ANSI codes are just passed through harmlessly)."""
    return text


def colorize(status: str, /, *, palette: dict | None = None) -> str:
    """Return *status* wrapped in the appropriate ANSI color code.

    Palette mapping (first match wins):
      "halt" or "live"  → high-intensity red
      "ok"  or "healthy" → high-intensity green
      "warn" or "dry" or "stale" → high-intensity yellow
      everything else → white (reset)
    """
    s = (status or "").lower()
    if not s:
        return f"{COLORS['reset']}--{COLORS['reset']}"
    mapping = {
        "halt": COLORS["hr"],
        "live": COLORS["hr"],
        "ok": COLORS["hg"],
        "healthy": COLORS["hg"],
        "warn": COLORS["hy"],
        "dry": COLORS["hy"],
        "stale": COLORS["hy"],
    }
    for key in mapping:
        if key in s:
            return f"{mapping[key]}{status}{COLORS['reset']}"
    return f"{COLORS['white']}{status}{COLORS['reset']}"


def fmt_money(n) -> str:
    """Format a dollar amount, e.g. +$9.04  or  -$12.50."""
    if n is None:
        return "--"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "--"
    return f"+${abs(n):.2f}" if n >= 0 else f"-${abs(n):.2f}"


def fmt_pct(n) -> str:
    """Format a percentage, e.g. +4.23%  or  -1.20%."""
    if n is None:
        return "--"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "--"
    return f"{'+' if n >= 0 else ''}{abs(n):.2f}%"


def fmt_cents(v) -> str:
    """Format a price in cents, e.g. 47.0¢  or  --."""
    if v is None:
        return "--"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "--"
    return f"{v:.1f}¢"


def fmt_dollars(n) -> str:
    """Format a dollar value without sign, e.g. $78.00."""
    if n is None:
        return "--"
    try:
        return f"${float(n):.2f}"
    except (TypeError, ValueError):
        return "--"


def num(v) -> float | None:
    """Parse *v* into a float; return None on failure."""
    if v is None or v == "" or v == "null":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ─── Terminal geometry ────────────────────────────────────────────────────────

TERM_WIDE = 100  # fallback column width; actual width derived at render time


# ─── TUI class ────────────────────────────────────────────────────────────────

class TUI:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(timeout=5.0)
        self.multicoin = {}
        self.live_kalshi = {}
        self.watchdog = {}
        self.brain_logs = []
        self.running = True
        self.last_update = None
        self.status = "CONNECTING"

    # ── API helpers ──────────────────────────────────────────────────

    def fetch_json(self, path: str):
        try:
            r = self.client.get(self.base_url + path, timeout=5.0)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return None

    def poll_once(self):
        mc = self.fetch_json("/api/multicoin/status")
        if mc:
            self.multicoin = mc
        live = self.fetch_json("/api/live/status")
        if live:
            self.live_kalshi = live
            self.watchdog = live.get("watchdog", {})
        self.last_update = self._now_str()
        self.status = "LIVE"

    def _now_str(self) -> str:
        return datetime.now(timezone.utc).strftime("%H:%M:%S")

    def run_sse(self):
        """Connect to the SSE stream and update internal state on each frame."""
        try:
            with self.client.stream("GET", self.base_url + "/api/stream", timeout=30.0) as r:
                for line in r.iter_lines():
                    if not self.running:
                        break
                    if not line or not line.startswith("data: "):
                        continue
                    try:
                        payload = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if payload.get("heartbeat"):
                        self.status = "LIVE"
                        continue
                    mc = payload.get("multicoin")
                    if mc:
                        self.multicoin = mc
                    live = payload.get("live_kalshi")
                    if live:
                        self.live_kalshi = live
                        self.watchdog = live.get("watchdog", {})
                    logs = payload.get("multicoin_brain_logs")
                    if logs:
                        self.brain_logs = logs
                    self.last_update = self._now_str()
                    self.status = "LIVE"
        except Exception as e:
            self.status = f"SSE ERROR: {e}"
            # Fallback to polling every 2 s
            while self.running:
                self.poll_once()
                time.sleep(2.0)

    # ── Rendering ────────────────────────────────────────────────────

    def render(self) -> str:
        """Build the full dashboard text using ANSI colors and box-drawing."""
        try:
            _, width = self._term_size()
        except Exception:
            width = TERM_WIDE

        # ── Header ──────────────────────────────────────────────────────
        header_line = f"┌{'─' * (width - 2)}┐"
        title = f"│ {colorize('KALSHI LIVE TRADING DASHBOARD', palette=None):^{width - 2}} │"
        status_line = (
            f"│ Status: {colorize(self.status, palette=None):<12} │ "
            f"Last update: {self.last_update or '--':>8} │"
        )
        footer_sep = f"├{'─' * (width - 2)}├"

        lines = []
        lines.append(header_line)
        lines.append(title)
        lines.append(status_line)
        lines.append(footer_sep)

        # ── Portfolio ───────────────────────────────────────────────────
        lines.append(self._boxed_section("PORTFOLIO", width, self._render_portfolio))

        # ── Live OMS ────────────────────────────────────────────────────
        lines.append(self._boxed_section("LIVE OMS", width, self._render_oms))

        # ── Watchdog ────────────────────────────────────────────────────
        lines.append(self._boxed_section("WATCHDOG", width, self._render_watchdog))

        # ── Strategy regime ─────────────────────────────────────────────
        lines.append(self._boxed_section("STRATEGY REGIME", width, self._render_regime))

        # ── Positions ───────────────────────────────────────────────────
        lines.append(self._boxed_section("POSITIONS", width, self._render_positions))

        # ── Recent trades ───────────────────────────────────────────────
        lines.append(self._boxed_section("RECENT TRADES", width, self._render_trades))

        # ── Brain logs ──────────────────────────────────────────────────
        lines.append(self._boxed_section("BRAIN LOGS (last 5)", width, self._render_brain_logs))

        # ── Footer ──────────────────────────────────────────────────────
        lines.append(f"└{'─' * (width - 2)}┘")
        lines.append(
            f"{'':<2}Press Ctrl-C to quit  |  Last frame: {self.last_update or '--'}  "
        )

        return "\n".join(lines)

    def _term_size(self):
        """Yield (rows, cols) by checking stdout/ioctl; fallback to TERM_WIDE."""
        import os, fcntl, struct, termios

        try:
            fd = os.open(os.path.stdout.fileno(), os.O_RDONLY)
            size = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"    ")
            os.close(fd)
            rows, cols = struct.unpack("HH", size)
            if cols > 0:
                return (rows, cols)
        except Exception:
            pass
        return None, TERM_WIDE

    # ── Individual render sub-sections ──────────────────────────────────

    def _boxed_section(self, title: str, width: int, render_fn) -> str:
        """Render a double-bordered section with a title bar."""
        inner = width - 2
        top_border = f"┌{'─' * inner}┐"
        bottom_border = f"└{'─' * inner}┘"
        # Title line with active color
        title_line = f"│ {colorize(title, palette=None):^{inner}} │"

        # Render the inner content; split into lines and pad/truncate
        inner_text = render_fn()
        inner_lines = inner_text.split("\n")
        # Pad or truncate to fit reasonable height (leave 2 rows margin)
        available = max(1, inner - 4)  # rough height estimate
        if len(inner_lines) > available:
            inner_lines = inner_lines[: available - 1] + ["… truncated"]

        # Assemble boxed block
        parts = [top_border, title_line]
        for line in inner_lines:
            # Truncate line if too long
            line = line[:inner]
            parts.append(f"│ {line:<{inner}} │")
        parts.append(bottom_border)
        return "\n".join(parts)

    def _render_portfolio(self) -> str:
        mc = self.multicoin or {}
        st = mc.get("status", {}) if isinstance(mc, dict) else {}
        lines = []
        equity = st.get("total_equity")
        pnl = st.get("total_pnl")
        win = st.get("overall_win_rate_pct")
        lines.append(f"  Equity:    {fmt_money(equity)}")
        lines.append(f"  PnL:       {fmt_money(pnl)}  ({fmt_pct(win)} win rate)")
        lines.append(f"  Cash:      {fmt_money(st.get('total_cash'))}")
        lines.append(f"  Trades:    {st.get('total_trades', '--')}")
        lines.append(f"  Win Rate:  {fmt_pct(st.get('overall_win_rate_pct'))}")
        return "\n".join(lines)

    def _render_oms(self) -> str:
        live = self.live_kalshi or {}
        lines = []
        mode = live.get("mode", "--")
        ws = live.get("ws_connected")
        balance = live.get("balance_dollars")
        lines.append(f"  Mode:      {colorize(str(mode), palette=None)}")
        lines.append(f"  WS Feed:   {'UP' if ws else 'DOWN'}")
        lines.append(f"  Balance:   {fmt_dollars(balance)}")
        # L2 depth
        l2 = live.get("l2_depth", {})
        if isinstance(l2, dict):
            y_bid = l2.get("best_bid")
            y_ask = l2.get("best_ask")
            lines.append(f"  BTC Bid:   {fmt_cents(y_bid)}   Ask: {fmt_cents(y_ask)}")
        return "\n".join(lines)

    def _render_watchdog(self) -> str:
        wd = self.watchdog or {}
        lines = []
        status = wd.get("status", "--")
        delta = wd.get("last_delta_ms", "--")
        halt = wd.get("halt_reason", "")
        lines.append(f"  Status:   {colorize(str(status), palette=None)}")
        lines.append(f"  Delta:    {delta}ms")
        lines.append(f"  Halt:     {halt if halt else 'none'}")
        return "\n".join(lines)

    def _render_regime(self) -> str:
        mc = self.multicoin or {}
        mc_matrix = mc.get("multicoin_matrix", {}) if isinstance(mc, dict) else {}
        lines = []
        for coin in ("BTC", "ETH"):
            m = mc_matrix.get(coin, {}) if isinstance(mc_matrix, dict) else {}
            action = m.get("action", "--")
            prob = m.get("prob_yes_pct")
            edge = m.get("edge_y")
            lines.append(f"  {coin}: action={action}  prob={fmt_pct(prob)}  edge={fmt_pct(edge)}")
        return "\n".join(lines)

    def _render_positions(self) -> str:
        mc = self.multicoin or {}
        positions = mc.get("active_positions", {}) if isinstance(mc, dict) else {}
        lines = []
        if not positions:
            lines.append("  None")
        else:
            lines.append(f"  {'Coin':<6} {'Side':<5} {'Entry':<8} {'Contracts':<10} {'ROI':<8}")
            for coin, pos in positions.items():
                pos = pos or {}
                entry = fmt_cents(pos.get("entry_price"))
                contracts = str(pos.get("contracts", ""))
                roi = fmt_pct(pos.get("roi_pct"))
                lines.append(f"  {coin:<6} {str(pos.get('side', '')):<5} {entry:<8} {contracts:<10} {roi:<8}")
        return "\n".join(lines)

    def _render_trades(self) -> str:
        mc = self.multicoin or {}
        trades = mc.get("recent_trades", []) if isinstance(mc, dict) else []
        lines = []
        if not trades:
            lines.append("  None")
        else:
            lines.append(f"  {'Time':<9} {'Coin':<5} {'Side':<5} {'Entry':<8} {'PnL':<10} {'Reason':<20}")
            for t in trades[::-1][: 6]:
                t = t or {}
                ts_val = t.get("timestamp")
                ts = datetime.fromtimestamp(ts_val).strftime("%H:%M:%S") if ts_val else "--"
                pnl = t.get("pnl_dollars")
                reason = str(t.get("exit_reason") or t.get("status", ""))[:20]
                lines.append(
                    f"  {ts:<9} {str(t.get('coin', '')):<5} {str(t.get('side', '')):<5} "
                    f"{fmt_cents(t.get('entry_price')):<8} {fmt_money(pnl):<10} {reason}"
                )
        return "\n".join(lines)

    def _render_brain_logs(self) -> str:
        logs = self.brain_logs or []
        lines = []
        if not logs:
            lines.append("  None")
        else:
            for l in logs[-5:]:
                lbl = l.get("time", "")
                txt = l.get("text", "")
                lines.append(f"  {lbl}  {txt}")
        return "\n".join(lines)

    # ── Runtime loop ────────────────────────────────────────────────────

    def run(self):
        import threading

        def sse_thread():
            self.run_sse()

        t = threading.Thread(target=sse_thread, daemon=True)
        t.start()

        try:
            while self.running:
                # Clear screen and move cursor home via ANSI escape
                sys.stdout.write("\033[2J\033[H")
                sys.stdout.write(self.render())
                sys.stdout.flush()
                time.sleep(2.0)
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Kalshi Live Trading TUI")
    parser.add_argument(
        "--base-url", default="http://localhost:8088", help="Dashboard base URL"
    )
    args = parser.parse_args()

    tui = TUI(args.base_url)
    try:
        tui.run()
    except KeyboardInterrupt:
        pass
    finally:
        tui.running = False
        # Ensure cursor is visible and terminal returns to normal
        sys.stdout.write("\033[?25h\033[0m\n")
        print(f"TUI stopped. Last status: {tui.status}")


if __name__ == "__main__":
    main()