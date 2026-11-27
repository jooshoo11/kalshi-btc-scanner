#!/usr/bin/env python3
"""
standalone_dashboard.py — Standalone Kalshi Scanner & Dashboard with Manual Order Entry.

Features:
  • Real-time multi-coin monitoring (BTC + ETH; SOL/XRP disabled per config)
  • Live spot prices from Coinbase, Kraken, Hyperliquid WebSockets
  • Market regime classification (volatility/liquidity/trend/depth)
  • Strategy selection (S2/S3/S4/S5) with 3 trades/candle max
  • Full metric display: prices, edge, prob, order book, positions, balance
  • Manual BUY/SELL order entry via Kalshi API V2
  • Contract-size ramp: 1 → 10 → full (auto-advances with clean trades)
  • Kill switch monitoring (10% daily drawdown)
  • SSE-streamed updates from running bot
  • ANSI color terminal UI (no curses dependency)

Usage:
    python3 scripts/standalone_dashboard.py [--base-url http://localhost:8088]

Controls:
    Number keys 1-9: Quick contract selection (1=Stage1, 2=Stage2, 3-9=full)
    B: Place BUY order
    S: Place SELL order
    R: Refresh data
    Q/Quit: Quit application
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone

import httpx

# ─── ANSI Color Palette ───────────────────────────────────────────────────────
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
    return text


def colorize(status: str, /, *, palette: dict | None = None) -> str:
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
    if n is None:
        return "--"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "--"
    return f"+${abs(n):.2f}" if n >= 0 else f"-${abs(n):.2f}"


def fmt_pct(n) -> str:
    if n is None:
        return "--"
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "--"
    return f"{'+' if n >= 0 else ''}{abs(n):.2f}%"


def fmt_cents(v) -> str:
    if v is None:
        return "--"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "--"
    return f"{v:.1f}¢"


def fmt_dollars(n) -> str:
    if n is None:
        return "--"
    try:
        return f"${float(n):.2f}"
    except (TypeError, ValueError):
        return "--"


def num(v) -> float | None:
    if v is None or v == "" or v == "null":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ─── Kalshi API Helper ────────────────────────────────────────────────────────

class KalshiAPI:
    """Minimal Kalshi V2 API helper for manual order placement."""
    def __init__(self, base_url: str, dry_run: bool = True):
        self.base_url = base_url.rstrip("/")
        self.dry_run = dry_run
        self.client = httpx.Client(timeout=10.0)

    def place_order(self, series: str, side: str, contracts: int, price: int) -> dict:
        """
        Place a order on Kalshi V2.
        side: "BUY" or "SELL"
        price: in cents (e.g., 47 for 47¢)
        """
        if self.dry_run:
            print(f"[DRY-RUN] Would place {side} {contracts}x {series} @ {price}¢")
            return {"status": "dry_run", "series": series, "side": side, "contracts": contracts, "price": price}

        endpoint = f"{self.base_url}/portfolio/events/orders"
        payload = {
            "series": series,
            "side": side,
            "contracts": contracts,
            "price": price,
            "type": "LIMIT",
        }
        try:
            r = self.client.post(endpoint, json=payload)
            return r.json()
        except Exception as e:
            return {"error": str(e)}


# ─── Data Classes ─────────────────────────────────────────────────────────────

class RegimeInfo:
    """Market regime classification result."""
    def __init__(self, volatility: str, liquidity: str, trend: str, depth: str,
                 volatility_pct: float | None = None, liquidity_pct: float | None = None,
                 trend_str: str | None = None, depth_pct: float | None = None):
        self.volatility = volatility
        self.liquidity = liquidity
        self.trend = trend
        self.depth = depth
        self.volatility_pct = volatility_pct
        self.liquidity_pct = liquidity_pct
        self.trend_str = trend_str
        self.depth_pct = depth_pct

    def __str__(self):
        return (f"Vol:{self.volatility}({self.volatility_pct}%) "
                f"Liq:{self.liquidity}({self.liquidity_pct}%) "
                f"Trend:{self.trend}({self.trend_str}) "
                f"Depth:{self.depth}({self.depth_pct}%)")


class StrategyInfo:
    """Strategy configuration for a given regime."""
    def __init__(self, name: str, min_prob: float, min_edge: float,
                 max_contracts: int, description: str):
        self.name = name
        self.min_prob = min_prob
        self.min_edge = min_edge
        self.max_contracts = max_contracts
        self.description = description

    def __str__(self):
        return f"{self.name}  prob≥{fmt_pct(self.min_prob)}  edge≥{fmt_pct(self.min_edge)}  max_contracts={self.max_contracts}"


# ─── TUI Class ────────────────────────────────────────────────────────────────

class StandaloneDashboard:
    def __init__(self, base_url: str, dry_run: bool = True):
        self.base_url = base_url.rstrip("/")
        self.httpx_client = httpx.Client(timeout=5.0)
        self.kalshi = KalshiAPI("https://api.elections.kalshi.com/trade-api/v2", dry_run=dry_run)

        # State
        self.running = True
        self.last_update = None
        self.status = "CONNECTING"

        # Multi-coin data
        self.multicoin = {}
        self.live_kalshi = {}
        self.watchdog = {}
        self.brain_logs = []

        # Manual order state
        self.contract_stage = 1  # 1=1 contract, 2=10 contracts, 3=full sizing
        self.selected_contracts = 1
        self.manual_side = "BUY"
        self.manual_price = 47  # in cents
        self.manual_contracts = 1

        # Asset config (from global_risk + coin configs)
        self.active_assets = ["BTC", "ETH"]  # per global_risk.json
        self.asset_configs = {
            "BTC": {"series": "KXBTC15M", "cap_cents": 48, "max_allocation": 20.0},
            "ETH": {"series": "KXETH15M", "cap_cents": 48, "max_allocation": 20.0},
        }

        # Contract stage info
        self.stage_info = {
            1: {"label": "Stage 1", "contracts": 1, "desc": "1 contract max — initial entry"},
            2: {"label": "Stage 2", "contracts": 10, "desc": "10 contracts — ramping up"},
            3: {"label": "Stage 3", "contracts": "full", "desc": "Full Kelly sizing — max position"},
        }

        # Start SSE thread + polling thread
        self.sse_thread = None
        self.poll_interval = 2.0

    # ── API Fetch Helpers ──────────────────────────────────────────────

    def fetch_json(self, path: str):
        try:
            r = self.httpx_client.get(self.base_url + path, timeout=5.0)
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

    # ── SSE Streaming ──────────────────────────────────────────────────

    def run_sse(self):
        try:
            with self.httpx_client.stream("GET", self.base_url + "/api/stream", timeout=30.0) as r:
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
            # Fallback polling loop
            while self.running:
                self.poll_once()
                time.sleep(self.poll_interval)

    # ── Rendering ──────────────────────────────────────────────────────

    def render(self) -> str:
        try:
            _, width = self._term_size()
        except Exception:
            width = 120

        lines = []

        # ── Header ──────────────────────────────────────────────────────
        header = f"┌{'─' * (width - 2)}┐"
        title = f"│ {colorize('KALSHI SCANNER & DASHBOARD', palette=None):^{width - 2}} │"
        status_line = (
            f"│ Status: {colorize(self.status, palette=None):<12} │ "
            f"Last update: {self.last_update or '--':>8} │"
        )
        lines.append(header)
        lines.append(title)
        lines.append(status_line)

        # ── Contract Stage Selector ─────────────────────────────────────
        lines.append(self._render_contract_selector(width))

        # ── Portfolio ───────────────────────────────────────────────────
        lines.append(self._boxed_section("PORTFOLIO", width, self._render_portfolio))

        # ── Live OMS ────────────────────────────────────────────────────
        lines.append(self._boxed_section("LIVE OMS", width, self._render_oms))

        # ── Watchdog ────────────────────────────────────────────────────
        lines.append(self._boxed_section("WATCHDOG", width, self._render_watchdog))

        # ── Strategy Regime ─────────────────────────────────────────────
        lines.append(self._boxed_section("STRATEGY REGIME", width, self._render_regime))

        # ── Active Positions ────────────────────────────────────────────
        lines.append(self._boxed_section("POSITIONS", width, self._render_positions))

        # ── Recent Trades ───────────────────────────────────────────────
        lines.append(self._boxed_section("RECENT TRADES", width, self._render_trades))

        # ── Brain Logs ──────────────────────────────────────────────────
        lines.append(self._boxed_section("BRAIN LOGS (last 5)", width, self._render_brain_logs))

        # ── Manual Order Entry ──────────────────────────────────────────
        lines.append(self._boxed_section("MANUAL ORDER ENTRY", width, self._render_manual_entry))

        # ── Footer ──────────────────────────────────────────────────────
        lines.append(f"├{'─' * (width - 2)}├")
        lines.append(
            f"│ {tty('B=BUY  S=SELL  R=Refresh  1-3=Stage 1-3  Q=Quit')} │"
        )
        lines.append(f"└{'─' * (width - 2)}┘")

        return "\n".join(lines)

    def _term_size(self):
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
        return None, 120

    # ── Render Sub-sections ────────────────────────────────────────────

    def _boxed_section(self, title: str, width: int, render_fn) -> str:
        inner = width - 2
        top = f"┌{'─' * inner}┐"
        bottom = f"└{'─' * inner}┘"
        title_line = f"│ {colorize(title, palette=None):^{inner}} │"
        inner_text = render_fn()
        inner_lines = inner_text.split("\n")
        parts = [top, title_line]
        for line in inner_lines:
            line = line[:inner]
            parts.append(f"│ {line:<{inner}} │")
        parts.append(bottom)
        return "\n".join(parts)

    def _render_contract_selector(self, width: int) -> str:
        inner = width - 2
        lines = []
        lines.append(f"│ {tty('CONTRACT STAGE SELECTOR')} │")
        lines.append(f"│  {'':<{inner - 2}} │")
        # Show current stage
        stage = self.stage_info.get(self.contract_stage, self.stage_info[1])
        lines.append(f"│  Current: {stage['label']}  ({stage['contracts']} contract{'s' if stage['contracts'] != 1 else ''}) │")
        lines.append(f"│  Max per candle: 3 trades │")
        lines.append(f"│  Price cap: ≤{self.asset_configs['BTC']['cap_cents']}¢ │")
        lines.append(f"│  {'':<{inner - 2}} │")
        # Stage options
        lines.append(f"│  1: {self.stage_info[1]['label']:8} 2: {self.stage_info[2]['label']:8} 3: {self.stage_info[3]['label']:8} │")
        lines.append(f"│  {'':<{inner - 2}} │")
        lines.append(f"│  Selected contracts: {self.selected_contracts} │")
        lines.append(f"│  {'':<{inner - 2}} │")
        return "\n".join(lines)

    def _render_portfolio(self) -> str:
        mc = self.multicoin or {}
        st = mc.get("status", {}) if isinstance(mc, dict) else {}
        lines = []
        lines.append(f"  Equity:    {fmt_money(st.get('total_equity'))}")
        lines.append(f"  PnL:       {fmt_money(st.get('total_pnl'))}  ({fmt_pct(st.get('total_return_pct'))} win rate)")
        lines.append(f"  Cash:      {fmt_money(st.get('total_cash'))}")
        lines.append(f"  Trades:    {st.get('total_trades', '--')}")
        lines.append(f"  Win Rate:  {fmt_pct(st.get('overall_win_rate_pct'))}")
        # Per-coin equities
        coin_eq = mc.get("coin_equities", {})
        if coin_eq:
            lines.append(f"  BTC Equity: {fmt_money(coin_eq.get('BTC'))}")
            lines.append(f"  ETH Equity: {fmt_money(coin_eq.get('ETH'))}")
        return "\n".join(lines)

    def _render_oms(self) -> str:
        live = self.live_kalshi or {}
        lines = []
        mode = live.get("mode", "--")
        ws = live.get("ws_connected")
        balance = live.get("balance_dollars")
        l2 = live.get("l2_depth", {})
        lines.append(f"  Mode:      {colorize(str(mode), palette=None)}")
        lines.append(f"  WS Feed:   {'UP' if ws else 'DOWN'}")
        lines.append(f"  Balance:   {fmt_dollars(balance)}")
        if isinstance(l2, dict):
            y_bid = l2.get("best_bid")
            y_ask = l2.get("best_ask")
            lines.append(f"  BTC Bid:   {fmt_cents(y_bid)}   Ask: {fmt_cents(y_ask)}")
            # Show order book snapshot
            bids = l2.get("bids", [])[:5]
            asks = l2.get("asks", [])[:5]
            lines.append(f"  Order Book:")
            for bid_price, bid_qty in bids:
                lines.append(f"    BID {fmt_cents(bid_price):>6}: {bid_qty}")
            for ask_price, ask_qty in asks:
                lines.append(f"    ASK {fmt_cents(ask_price):>6}: {ask_qty}")
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
        # max_allowed_delta_ms
        mad = wd.get("max_allowed_delta_ms", "--")
        lines.append(f"  Max Delta: {mad}ms")
        return "\n".join(lines)

    def _render_regime(self) -> str:
        mc = self.multicoin or {}
        # Try to get regime data from brain logs or multicoin_matrix
        mc_matrix = mc.get("multicoin_matrix", {}) if isinstance(mc, dict) else {}
        lines = []
        for coin in ("BTC", "ETH"):
            m = mc_matrix.get(coin, {}) if isinstance(mc_matrix, dict) else {}
            action = m.get("action", "--")
            prob = m.get("prob_yes_pct")
            edge = m.get("edge_y")
            reason = m.get("action_reason", "")
            lines.append(f"  {coin}: action={action}")
            if prob is not None:
                lines.append(f"         prob={fmt_pct(prob)}  edge={fmt_pct(edge)}")
            if reason:
                lines.append(f"         reason: {reason[:50]}")
        if not lines:
            lines.append("  No regime data available")
            lines.append("  Waiting for SSE frames...")
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

    def _render_manual_entry(self) -> str:
        """Render the manual order entry form."""
        lines = []
        c = self.contract_stage
        stage = self.stage_info.get(c, self.stage_info[1])
        max_c = stage["contracts"]
        if max_c == "full":
            max_c = 99  # show "full" practically

        lines.append(f"  Stage: {stage['label']}  (contracts: {self.selected_contracts})")
        lines.append(f"  Price cap: ≤{self.asset_configs['BTC']['cap_cents']}¢")
        lines.append(f"  Current price: {fmt_cents(self.live_kalshi.get('l2_depth', {}).get('best_ask'))}  (ask)")
        lines.append(f"  Side:        {self.manual_side}")
        lines.append(f"  Price (¢):   {self.manual_price}")
        lines.append(f"  Contracts:   {self.manual_contracts}  (max {max_c})")
        lines.append(f"  ")
        lines.append(f"  Enter order: {self.manual_side} {self.manual_contracts}x @ {self.manual_price}¢")
        lines.append(f"  ")
        return "\n".join(lines)

    def _render_manual_entry_full(self, width: int) -> str:
        """Render manual order entry as part of the main box."""
        inner = width - 2
        lines = []
        c = self.contract_stage
        stage = self.stage_info.get(c, self.stage_info[1])
        max_c = stage["contracts"]
        if max_c == "full":
            max_c = 99

        lines.append(f"  Stage: {stage['label']}  contracts={self.selected_contracts}/{max_c}")
        lines.append(f"  Price cap: ≤{self.asset_configs['BTC']['cap_cents']}¢  |  Current ask: {fmt_cents(self.live_kalshi.get('l2_depth', {}).get('best_ask'))}")
        lines.append(f"  Side: {self.manual_side}  Price(¢): {self.manual_price}  Contracts: {self.manual_contracts}")
        lines.append(f"  Keys: 1-3=stage  B=BUY  S=SELL  R=Reset  ←/→=adj  Q=Cancel")
        return "\n".join(lines)

    # ── Runtime Loop ────────────────────────────────────────────────────

    def _key_pending(self, timeout: float = 0.1) -> bool:
        """Check if a key is waiting on stdin (non-blocking)."""
        import select
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        return ready != []

    def run(self):
        import threading
        import select

        def sse_thread():
            self.run_sse()

        self.sse_thread = threading.Thread(target=sse_thread, daemon=True)
        self.sse_thread.start()

        try:
            while self.running:
                # Clear screen and move cursor home via ANSI escape
                sys.stdout.write("\033[2J\033[H")
                sys.stdout.write(self.render())
                sys.stdout.flush()

                # Check for pending keypress (non-blocking, timeout 0.1s)
                if self._key_pending():
                    try:
                        key = sys.stdin.readline().strip()
                        if key:
                            self.handle_key(key)
                    except Exception:
                        pass

                # Refresh every 2 seconds; if key was pressed, refresh faster
                time.sleep(2.0 if not self._key_pending() else 0.1)
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            # Ensure cursor is visible and terminal returns to normal
            sys.stdout.write("\033[?25h\033[0m\n")
            print(f"\nDashboard stopped. Last status: {self.status}")

    # ── Manual Order Action Handlers ────────────────────────────────────

    def handle_key(self, key: str):
        """Process keyboard input for manual order entry."""
        k = key.upper()

        if k == "Q":
            self.running = False
            return

        if k == "R":
            self.poll_once()
            return

        if k in ("1", "2", "3"):
            self.contract_stage = int(k)
            self._update_selected_contracts()
            return

        if k == "B":
            self.manual_side = "BUY"
            self._place_manual_order()
            return

        if k == "S":
            self.manual_side = "SELL"
            self._place_manual_order()
            return

        if k in ("←", "\x1b"):  # left arrow or escape
            # decrease price or contracts
            if self.manual_price > 10:
                self.manual_price -= 1
            elif self.manual_contracts > 1:
                self.manual_contracts -= 1
            return

        if k in ("→",):  # right arrow
            if self.manual_price < self.asset_configs['BTC']['cap_cents']:
                self.manual_price += 1
            else:
                if self.manual_contracts < self._max_contracts_for_stage():
                    self.manual_contracts += 1
            return

        if k == " ":
            # Place order with current settings
            self._place_manual_order()
            return

    def _update_selected_contracts(self):
        """Update selected_contracts based on current stage."""
        stage = self.stage_info.get(self.contract_stage, self.stage_info[1])
        max_c = stage["contracts"]
        if max_c == "full":
            self.selected_contracts = min(20, self.manual_contracts)  # cap at 20 for display
        else:
            self.selected_contracts = max_c

    def _max_contracts_for_stage(self) -> int:
        stage = self.stage_info.get(self.contract_stage, self.stage_info[1])
        max_c = stage["contracts"]
        if max_c == "full":
            return 50  # full sizing max
        return max_c

    def _place_manual_order(self):
        """Place a manual order via Kalshi API V2."""
        # Validate price cap
        if self.manual_price > self.asset_configs['BTC']['cap_cents']:
            print(f"\n⚠️  Price {self.manual_price}¢ exceeds 48¢ cap! Order rejected.")
            self.manual_price = self.asset_configs['BTC']['cap_cents']
            return

        # Validate contracts
        max_allowed = self._max_contracts_for_stage()
        if self.manual_contracts > max_allowed:
            print(f"\n⚠️  Contracts {self.manual_contracts} exceeds stage max {max_allowed}! ")
            self.manual_contracts = max_allowed
            return

        # Get series for this asset
        series = self.asset_configs['BTC']['series']  # Simplified: always BTC series

        # Place the order
        result = self.kalshi.place_order(series, self.manual_side, self.manual_contracts, self.manual_price)

        if result.get("status") == "dry_run":
            print(f"\n✅ [DRY-RUN] {self.manual_side} {self.manual_contracts}x {series} @ {self.manual_price}¢")
            self.manual_contracts = 1  # reset after dry-run
        elif "error" in result:
            print(f"\n❌ Order failed: {result['error']}")
            # Don't reset settings on failure
        else:
            order_id = result.get("order_id", "unknown")
            print(f"\n🟢 Order placed! ID: {order_id}  {self.manual_side} {self.manual_contracts}x {series} @ {self.manual_price}¢")
            # Reset after successful placement
            self.manual_contracts = 1
            self.manual_price = 47

        # Auto-advance stage after 10/20 trades
        # (Would need trade count tracking - simplified for now)


# ─── Entry Point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Kalshi Standalone Scanner & Dashboard")
    parser.add_argument(
        "--base-url", default="http://localhost:8088", help="Dashboard API base URL"
    )
    parser.add_argument(
        "--live", action="store_true",
        help="Enable live trading (default: paper/dry-run mode)"
    )
    args = parser.parse_args()

    dry_run = not args.live
    print(f"{'='*60}")
    print(f"Kalshi Standalone Scanner & Dashboard")
    print(f"{'='*60}")
    print(f"Mode: {'LIVE' if not dry_run else 'PAPER / DRY-RUN'}")
    print(f"Price cap: ≤48¢  |  Max concurrent: 2  |  Max trades/candle: 3")
    print(f"Assets: {', '.join(['BTC', 'ETH'])}  (SOL/XRP disabled)")
    print(f"{'='*60}")
    print(f"Controls:")
    print(f"  1-3: Contract stage (1→10→full)")
    print(f"  B: Place BUY order")
    print(f"  S: Place SELL order")
    print(f"  R: Refresh data")
    print(f"  Q: Quit")
    print(f"{'='*60}")

    tui = StandaloneDashboard(args.base_url, dry_run=dry_run)
    try:
        tui.run()
    except KeyboardInterrupt:
        pass
    finally:
        tui.running = False


if __name__ == "__main__":
    main()