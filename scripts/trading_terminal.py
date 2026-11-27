#!/usr/bin/env python3
"""
scripts/trading_terminal.py — Institutional CLI Trading Terminal for Kalshi BTC & ETH 15M Markets.

Features:
- High-frequency live streaming of BTC (KXBTC15M / BRTI) and ETH (KXETH15M / ETHUSD_RTI)
- 60s RTI Settlement Engine monitor:
  * [T-60, T] countdown, samples count (m/60)
  * Elapsed sample mean M_elapsed vs remaining path M_rem
  * Time-consistent projected settlement mu_settle converging to CF Benchmarks mean
- Regime-Aware Z-Score & Probability Diagnostics:
  * Diagnostic Z_cf vs Effective Z_eff
  * Clamped Lead-Lag / OFI Z-shift ([-0.35, +0.35])
  * Microstructure noise-cleaned realized volatility vs base dollar volatility
- Orderbook Sanity Guards & Signal Hysteresis:
  * Quote freshness, spread guard (<= 5¢), top-of-book depth guard (>= 10 contracts)
  * Hysteresis-stabilized hypothesis (BULLISH_YES, BEARISH_NO, NEUTRAL_HOLD)
  * Precomputed 40-contract batch taker fee (Fee_Y, Fee_N) and net edges
- Interactive hotkeys:
  [k] Emergency Kill-Switch TRIP     [r] Kill-Switch RESET / Rearm
  [c] Panic Cancel / Close Orders    [b] Focus BTC   [e] Focus ETH   [a] All Coins
  [q] Quit

Usage:
    python3 scripts/trading_terminal.py [--url http://localhost:8088]
"""

import argparse
import json
import os
import select
import sys
import termios
import time
import tty
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

# ─── ANSI Palette ────────────────────────────────────────────────────────────
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_RED = "\033[91m"
C_GREEN = "\033[92m"
C_YELLOW = "\033[93m"
C_BLUE = "\033[94m"
C_MAGENTA = "\033[95m"
C_CYAN = "\033[96m"
C_WHITE = "\033[97m"
C_BG_RED = "\033[41m"
C_BG_GREEN = "\033[42m"
C_BG_BLUE = "\033[44m"
C_BG_GRAY = "\033[100m"


def clr(text: str, color: str) -> str:
    return f"{color}{text}{C_RESET}"


def fmt_cents(val: Any) -> str:
    if val is None:
        return "--"
    try:
        n = float(val)
        return f"{n * 100:.1f}¢" if n <= 1.0 else f"{n:.1f}¢"
    except (TypeError, ValueError):
        return "--"


def fmt_money(val: Any) -> str:
    if val is None:
        return "--"
    try:
        n = float(val)
        sign = "+" if n > 0 else ("-" if n < 0 else " ")
        return f"{sign}${abs(n):.2f}"
    except (TypeError, ValueError):
        return "--"


def fmt_pct(val: Any, with_sign: bool = True) -> str:
    if val is None:
        return "--"
    try:
        n = float(val)
        sign = ("+" if n > 0 else "") if with_sign else ""
        return f"{sign}{n:.1f}%"
    except (TypeError, ValueError):
        return "--"


def progress_bar(val: float, max_val: float, width: int = 20, fill_char: str = "█", empty_char: str = "░") -> str:
    if max_val <= 0:
        return empty_char * width
    ratio = max(0.0, min(1.0, val / max_val))
    filled = int(round(ratio * width))
    return fill_char * filled + empty_char * (width - filled)


class TradingTerminal:
    def __init__(self, base_url: str = "http://localhost:8088"):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(timeout=4.0)
        self.running = True
        self.view_filter = "ALL"  # "ALL", "BTC", "ETH"
        self.status = "CONNECTING"
        self.last_update = "--:--:--"
        self.notice_msg = ""
        self.notice_time = 0.0

        # Cached state
        self.matrix: Dict[str, Dict[str, Any]] = {}
        self.portfolio: Dict[str, Any] = {}
        self.live_kalshi: Dict[str, Any] = {}
        self.kill_switch_tripped: bool = False
        self.brain_logs: List[Dict[str, Any]] = []
        self.active_brackets: List[Dict[str, Any]] = []
        self.bracket_history: List[Dict[str, Any]] = []

    def set_notice(self, msg: str):
        self.notice_msg = msg
        self.notice_time = time.time()

    def get_term_size(self):
        try:
            cols, rows = os.get_terminal_size()
            return max(80, cols), max(24, rows)
        except Exception:
            return 110, 35

    def poll_state(self):
        """Fetch full snapshot via REST API."""
        try:
            r = self.client.get(f"{self.base_url}/api/scan", timeout=2.0)
            if r.status_code == 200:
                data = r.json()
                self._update_data(data)
                self.status = "LIVE_REST"
                self.last_update = datetime.now(timezone.utc).strftime("%H:%M:%S")
        except Exception as e:
            self.status = f"ERR_CONNECTING ({type(e).__name__})"

    def _update_data(self, data: Dict[str, Any]):
        if not data:
            return
        m = data.get("multicoin_matrix")
        if m and isinstance(m, dict):
            self.matrix = m
        pf = data.get("multicoin")
        if pf and isinstance(pf, dict):
            self.portfolio = pf
        lk = data.get("live_kalshi")
        if lk and isinstance(lk, dict):
            self.live_kalshi = lk
        logs = data.get("multicoin_brain_logs")
        if logs and isinstance(logs, list):
            self.brain_logs = logs
        ks = data.get("kill_switch")
        if isinstance(ks, dict):
            self.kill_switch_tripped = ks.get("tripped", False)
        elif os.path.exists("KILL"):
            self.kill_switch_tripped = True
        else:
            self.kill_switch_tripped = False
        bkts = data.get("active_brackets")
        if bkts is not None and isinstance(bkts, list):
            self.active_brackets = bkts
        bkt_h = data.get("bracket_history")
        if bkt_h is not None and isinstance(bkt_h, list):
            self.bracket_history = bkt_h

    def trigger_kill_switch(self):
        try:
            r = self.client.post(f"{self.base_url}/api/kill-switch/trip")
            if r.status_code == 200:
                self.set_notice("KILL SWITCH TRIPPED! All orders halted.")
            else:
                self.set_notice(f"Kill switch failed: HTTP {r.status_code}")
        except Exception as e:
            self.set_notice(f"Kill switch error: {e}")

    def reset_kill_switch(self):
        try:
            r = self.client.post(f"{self.base_url}/api/kill-switch/reset")
            if r.status_code == 200:
                self.set_notice("Kill switch RESET and re-armed.")
            else:
                self.set_notice(f"Reset failed: HTTP {r.status_code}")
        except Exception as e:
            self.set_notice(f"Reset error: {e}")

    def panic_cancel_all(self):
        try:
            r = self.client.post(f"{self.base_url}/api/live/cancel_all")
            if r.status_code == 200:
                self.set_notice("PANIC: All resting exchange orders cancelled.")
            else:
                self.set_notice(f"Cancel failed: HTTP {r.status_code}")
        except Exception as e:
            self.set_notice(f"Cancel error: {e}")

    def execute_one_click_buy(self, coin: str, side: str, contracts: int = 10, tp_pct: float = 25.0, sl_pct: float = 20.0):
        """Execute one-click Buy with precomputed Auto TP & SL."""
        try:
            m = self.matrix.get(coin, {})
            ticker = m.get("ticker", f"KX{coin}15M")
            entry_price = m.get("yes_ask", 0.35) if side == "YES" else round(1.0 - m.get("yes_bid", 0.65), 2)
            if entry_price <= 0:
                entry_price = 0.35

            payload = {
                "coin": coin,
                "ticker": ticker,
                "side": side,
                "contracts": contracts,
                "entry_price": float(entry_price),
                "tp_pct": float(tp_pct),
                "sl_pct": float(sl_pct),
            }
            r = self.client.post(f"{self.base_url}/api/trade/bracket", json=payload, timeout=3.0)
            if r.status_code == 200:
                res = r.json()
                t = res.get("ticket", {})
                tid = t.get("ticket_id", "")[:8]
                ec = t.get("entry_cents")
                tpc = t.get("tp_cents")
                slc = t.get("sl_cents")
                self.set_notice(f"BUY {side} {coin} ARMED: #{tid} @ {ec}¢ | TP: {tpc}¢ (+{tp_pct:.0f}%) | SL: {slc}¢ (-{sl_pct:.0f}%)")
            else:
                self.set_notice(f"BUY {side} {coin} REJECTED: HTTP {r.status_code} - {r.text[:60]}")
        except Exception as e:
            self.set_notice(f"BUY error: {e}")

    def flatten_active_brackets(self):
        """Flatten / cancel all active bracket orders."""
        try:
            r = self.client.post(f"{self.base_url}/api/trade/bracket/cancel", json={"all": True}, timeout=3.0)
            if r.status_code == 200:
                res = r.json()
                cnt = res.get("cancelled_count", 0)
                self.set_notice(f"FLATTEN: Disarmed {cnt} active bracket exit(s).")
            else:
                self.set_notice(f"Flatten failed: HTTP {r.status_code}")
        except Exception as e:
            self.set_notice(f"Flatten error: {e}")

    # ── Render Terminal View ──────────────────────────────────────────────
    def render(self) -> str:
        width, height = self.get_term_size()
        lines: List[str] = []

        # 1. Header Bar
        mode = self.live_kalshi.get("mode", "PAPER")
        ws_conn = self.live_kalshi.get("ws_connected", False)
        ws_badge = clr("● WS UP", C_GREEN) if ws_conn else clr("○ WS DOWN", C_RED)
        ks_badge = clr(" [KILL TRIPPED] ", C_BG_RED + C_WHITE + C_BOLD) if self.kill_switch_tripped else clr(" [ARMED] ", C_BG_GREEN + C_WHITE)

        title = f" KALSHI PREDICTION TERMINAL • BTC/ETH 15M "
        top_line = f"┌{'─' * (width - 2)}┐"
        lines.append(clr(top_line, C_CYAN))

        hdr = (
            f"│ {clr(title, C_BOLD + C_WHITE)}"
            f"Mode: {clr(mode, C_YELLOW)} {ks_badge} {ws_badge} "
            f"Filter: {clr(self.view_filter, C_CYAN)} "
            f"Status: {clr(self.status, C_GREEN if 'LIVE' in self.status else C_RED)} "
            f"UTC: {self.last_update} │"
        )
        lines.append(hdr)

        # Portfolio Summary Strip
        eq = self.portfolio.get("total_equity", 100.0)
        cash = self.portfolio.get("total_cash", 100.0)
        pnl = self.portfolio.get("total_pnl", 0.0)
        wr = self.portfolio.get("overall_win_rate_pct", 0.0)
        trades_cnt = self.portfolio.get("total_trades", 0)
        active_cnt = len(self.portfolio.get("active_positions", {}))

        pnl_clr = C_GREEN if pnl >= 0 else C_RED
        pnl_str = clr(fmt_money(pnl), pnl_clr)
        port_strip = (
            f"│ Equity: {clr('$' + f'{float(eq):.2f}', C_BOLD + C_WHITE)}  "
            f"Cash: ${float(cash):.2f}  "
            f"Net PnL: {pnl_str}  "
            f"Win Rate: {float(wr):.1f}% ({trades_cnt} trades)  "
            f"Active Pos: {clr(str(active_cnt), C_YELLOW)} │"
        )
        lines.append(port_strip)
        lines.append(clr(f"├{'─' * (width - 2)}┤", C_CYAN))

        # 2. Coin Telemetry Panels
        coins_to_show = ["BTC", "ETH"] if self.view_filter == "ALL" else [self.view_filter]
        for coin in coins_to_show:
            m = self.matrix.get(coin, {})
            self._render_coin_panel(coin, m, lines, width)

        # 3. Active Positions Strip
        lines.append(clr(f"├{'─' * (width - 2)}┤", C_CYAN))
        lines.append(f"│ {clr('ACTIVE POSITIONS & RISK ENFORCEMENT', C_BOLD + C_WHITE):<{width - 4}} │")
        positions = self.portfolio.get("active_positions", {})
        if not positions:
            lines.append(f"│   {clr('No open positions. Scanner evaluating entry conditions...', C_DIM):<{width - 6}} │")
        else:
            hdr_pos = f"│   {'COIN':<6} {'TICKER':<14} {'SIDE':<5} {'CONTRACTS':<10} {'ENTRY':<8} {'CURRENT':<8} {'UNREALIZED':<12} {'ROI %':<8} │"
            lines.append(clr(hdr_pos, C_DIM))
            for c, pos in positions.items():
                p_side = pos.get("side", "")
                p_c = pos.get("contracts", 0)
                p_entry = pos.get("entry_price", 0.0)
                p_cur = pos.get("current_bid", p_entry)
                p_roi = ((p_cur - p_entry) / max(0.01, p_entry)) * 100.0 if p_entry > 0 else 0.0
                p_u_pnl = (p_cur - p_entry) * p_c
                p_roi_clr = C_GREEN if p_roi >= 0 else C_RED
                pos_row = (
                    f"│   {c:<6} {pos.get('ticker', ''):<14} {clr(p_side, C_BOLD):<5} "
                    f"{p_c:<10} {fmt_cents(p_entry):<8} {fmt_cents(p_cur):<8} "
                    f"{clr(fmt_money(p_u_pnl), p_roi_clr):<12} {clr(fmt_pct(p_roi), p_roi_clr):<8} │"
                )
                lines.append(pos_row)

        # 3b. Active Bracket Orders (Auto TP / SL Watcher)
        lines.append(clr(f"├{'─' * (width - 2)}┤", C_CYAN))
        lines.append(f"│ {clr('ONE-CLICK BRACKET EXITS (AUTO TP / SL WATCHER)', C_BOLD + C_WHITE):<{width - 4}} │")
        if not self.active_brackets:
            lines.append(f"│   {clr('No active brackets. Hotkeys: [y] Buy YES, [n] Buy NO (auto TP/SL attached)', C_DIM):<{width - 6}} │")
        else:
            hdr_bkt = f"│   {'TICKET':<10} {'COIN':<5} {'SIDE':<5} {'QTY':<5} {'ENTRY':<7} {'AUTO TP':<15} {'AUTO SL':<15} {'STATUS':<10} │"
            lines.append(clr(hdr_bkt, C_DIM))
            for bkt in self.active_brackets:
                tid = bkt.get("ticket_id", "")[:8]
                bc = bkt.get("coin", "")
                bs = bkt.get("side", "")
                filled_sz = bkt.get("filled_contracts", bkt.get("requested_contracts", 0))
                ec = bkt.get("entry_cents", 0)
                tpc = bkt.get("tp_cents", 0)
                slc = bkt.get("sl_cents", 0)
                tp_pct = bkt.get("tp_pct", 0)
                sl_pct = bkt.get("sl_pct", 0)
                st = bkt.get("status", "")
                st_clr = C_GREEN if st in ("ACTIVE", "SUBMITTED") else (C_YELLOW if "TRIGGERED" in st else C_WHITE)
                tp_lbl = f"{tpc}¢ (+{tp_pct:.0f}%)"
                sl_lbl = f"{slc}¢ (-{sl_pct:.0f}%)"
                bkt_row = (
                    f"│   {tid:<10} {bc:<5} {clr(bs, C_BOLD):<5} {filled_sz:<5} "
                    f"{ec}¢     {clr(tp_lbl, C_GREEN):<15} {clr(sl_lbl, C_RED):<15} "
                    f"{clr(st, st_clr):<10} │"
                )
                lines.append(bkt_row)

        # 4. Recent Trades & Brain Logs
        lines.append(clr(f"├{'─' * (width - 2)}┤", C_CYAN))
        lines.append(f"│ {clr('EVENT STREAM & RECENT BRAIN LOGS', C_BOLD + C_WHITE):<{width - 4}} │")
        if self.brain_logs:
            for log in self.brain_logs[-4:]:
                t_str = log.get("time", "--:--:--")
                txt = log.get("text", "")
                lines.append(f"│   [{clr(t_str, C_DIM)}] {txt[:width - 16]:<{width - 16}} │")
        else:
            lines.append(f"│   {clr('Awaiting brain log events...', C_DIM):<{width - 6}} │")

        # 5. Hotkeys Footer
        lines.append(clr(f"├{'─' * (width - 2)}┤", C_CYAN))
        if self.notice_msg and (time.time() - self.notice_time < 5.0):
            lines.append(f"│ {clr('>>> ' + self.notice_msg, C_YELLOW + C_BOLD):<{width - 4}} │")
            lines.append(clr(f"├{'─' * (width - 2)}┤", C_CYAN))

        controls = (
            f" [y] BUY YES  [n] BUY NO  [x] FLATTEN ALL  "
            f"[k] TRIP Kill  [r] RESET Kill  [c] PANIC Cancel  "
            f"[b] BTC  [e] ETH  [a] ALL  [q] Quit"
        )
        lines.append(f"│ {clr(controls, C_DIM + C_WHITE):<{width - 4}} │")
        lines.append(clr(f"└{'─' * (width - 2)}┘", C_CYAN))

        return "\n".join(lines)

    def _render_coin_panel(self, coin: str, m: Dict[str, Any], lines: List[str], width: int):
        spot = m.get("spot", 0.0)
        open_p = m.get("open", 0.0)
        delta = m.get("delta", 0.0)
        sec_left = m.get("seconds_left", 0)
        mu_settle = m.get("mu_settle", spot)
        z_cf = m.get("z_cf", 0.0)
        z_eff = m.get("z_eff", m.get("z_score", 0.0))
        prob_yes = m.get("prob_yes_pct", 50.0)
        prob_no = m.get("prob_no_pct", 50.0)
        y_ask = m.get("yes_ask", 0.0)
        y_bid = m.get("yes_bid", 0.0)
        spread_c = m.get("spread_cents", 0)
        fee_y = m.get("fee_y", 0.0)
        fee_n = m.get("fee_n", 0.0)
        edge_y = m.get("edge_y", 0.0)
        edge_n = m.get("edge_n", 0.0)
        hypothesis = m.get("hypothesis", "NEUTRAL_HOLD")
        action = m.get("action", "HOLD")
        action_reason = m.get("action_reason", "")
        ticker = m.get("ticker", f"KX{coin}15M")

        # Visual highlights
        delta_clr = C_GREEN if delta >= 0 else C_RED
        hypo_clr = C_GREEN if "BULLISH" in hypothesis else (C_RED if "BEARISH" in hypothesis else C_YELLOW)
        action_clr = C_BG_GREEN + C_WHITE if "BUY" in action else (C_BG_BLUE + C_WHITE if "IN_POSITION" in action else C_YELLOW)

        # RTI 60s progress
        in_window = 0 <= sec_left <= 60
        w_elapsed = max(0.0, min(1.0, (60.0 - sec_left) / 60.0)) if in_window else (1.0 if sec_left == 0 else 0.0)
        rti_bar = progress_bar(w_elapsed, 1.0, width=15)
        rti_phase = clr("ACCUMULATING", C_YELLOW) if in_window else (clr("PRE-WINDOW", C_DIM) if sec_left > 60 else clr("SETTLED", C_GREEN))

        # Box Header for Coin
        coin_hdr = f"[{clr(coin, C_BOLD + C_CYAN)}] {ticker}  |  Exp in: {clr(str(int(sec_left)) + 's', C_BOLD)}  |  Action: {clr(' ' + action + ' ', action_clr)}"
        lines.append(f"│ {coin_hdr:<{width + 10}} │")

        # Line 1: Spot & Delta vs RTI Settlement mu_settle
        l1 = (
            f"  Spot: {clr(f'{spot:,.2f}', C_BOLD)}   "
            f"Strike: {open_p:,.2f}   "
            f"Delta: {clr(f'{delta:+,.2f}', delta_clr)}   "
            f"│  60s RTI μ_settle: {clr(f'{mu_settle:,.2f}', C_BOLD + C_WHITE)}  "
            f"[{rti_bar}] ({rti_phase})"
        )
        lines.append(f"│ {l1:<{width + 18}} │")

        # Line 2: Z-Scores (Z_cf vs Z_eff) & Model Probabilities
        l2 = (
            f"  Z_cf (raw): {z_cf:+.2f}   "
            f"Z_eff (regime): {clr(f'{z_eff:+.2f}', C_BOLD)}   "
            f"│  Prob(YES): {clr(f'{prob_yes:.1f}%', C_GREEN if prob_yes >= 60 else C_WHITE)}   "
            f"Prob(NO): {clr(f'{prob_no:.1f}%', C_RED if prob_no >= 60 else C_WHITE)}   "
            f"Hypothesis: {clr(hypothesis, hypo_clr)}"
        )
        lines.append(f"│ {l2:<{width + 18}} │")

        # Line 3: Order Book Sanity, Spread, O(1) 40c Fees & Net Edges
        l3 = (
            f"  Book: {fmt_cents(y_bid)} / {fmt_cents(y_ask)} (spd: {spread_c}¢)   "
            f"│  Fee(40c): Y {fee_y*100:.2f}¢ / N {fee_n*100:.2f}¢   "
            f"Edge: Y {clr(fmt_pct(edge_y), C_GREEN if edge_y >= 4 else C_DIM)} / N {clr(fmt_pct(edge_n), C_GREEN if edge_n >= 4 else C_DIM)}"
        )
        lines.append(f"│ {l3:<{width + 14}} │")

        # Line 4: Diagnostics & Rationales
        if action_reason:
            l4 = f"  Gate Rationale: {clr(action_reason, C_DIM)}"
            lines.append(f"│ {l4:<{width + 8}} │")

    def run_cli_loop(self):
        """Run terminal in raw mode handling async keyboard inputs and periodic polling."""
        old_settings = None
        try:
            if sys.stdin.isatty():
                old_settings = termios.tcgetattr(sys.stdin)
                tty.setcbreak(sys.stdin.fileno())

            # Clear screen and hide cursor
            sys.stdout.write("\033[2J\033[?25l")
            sys.stdout.flush()

            last_poll = 0.0
            while self.running:
                now = time.time()

                # Poll every 0.8s
                if now - last_poll >= 0.8:
                    self.poll_state()
                    last_poll = now
                    # Render frame
                    frame = self.render()
                    sys.stdout.write("\033[H" + frame)
                    sys.stdout.flush()

                # Check for keyboard input (non-blocking)
                if sys.stdin.isatty():
                    r, _, _ = select.select([sys.stdin], [], [], 0.1)
                    if r:
                        ch = sys.stdin.read(1)
                        if ch in ('q', 'Q', '\x03'):  # q or Ctrl-C
                            break
                        elif ch in ('k', 'K'):
                            self.trigger_kill_switch()
                        elif ch in ('r', 'R'):
                            self.reset_kill_switch()
                        elif ch in ('c', 'C'):
                            self.panic_cancel_all()
                        elif ch in ('x', 'X'):
                            self.flatten_active_brackets()
                        elif ch in ('y', 'Y'):
                            target_coin = "ETH" if self.view_filter == "ETH" else "BTC"
                            self.execute_one_click_buy(coin=target_coin, side="YES")
                        elif ch in ('n', 'N'):
                            target_coin = "ETH" if self.view_filter == "ETH" else "BTC"
                            self.execute_one_click_buy(coin=target_coin, side="NO")
                        elif ch in ('b', 'B'):
                            self.view_filter = "BTC"
                            self.set_notice("Filtered view: BTC")
                        elif ch in ('e', 'E'):
                            self.view_filter = "ETH"
                            self.set_notice("Filtered view: ETH")
                        elif ch in ('a', 'A'):
                            self.view_filter = "ALL"
                            self.set_notice("View: ALL (BTC + ETH)")
                else:
                    time.sleep(0.5)

        finally:
            if old_settings and sys.stdin.isatty():
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
            # Restore cursor and newline
            sys.stdout.write("\033[?25h\n")
            sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(description="Kalshi BTC/ETH 15M Quantitative Trading Terminal")
    parser.add_argument("--url", default="http://localhost:8088", help="Kalshi bot API URL (default: http://localhost:8088)")
    args = parser.parse_args()

    term = TradingTerminal(base_url=args.url)
    try:
        term.run_cli_loop()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
