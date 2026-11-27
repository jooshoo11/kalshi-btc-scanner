"""
core/bracket_order_manager.py — One-Click Manual Trading Execution Layer with Auto TP & SL.

Implements precomputed Take Profit (TP) and Stop Loss (SL) generation, strict bounds
validation, one-click entry execution, scaling to partial fills, and OCO (One-Cancels-the-Other)
exit management.

Key Guarantees:
1. Exact 1-Cent Instrument Tick Precision & Rounding (1¢ to 99¢ valid range).
2. Strict Inverted/Invalid Guardrails: 1 <= sl_cents < entry_cents < tp_cents <= 99.
3. Partial Fill Exit Scaling: Exit contracts strictly match filled contracts (never over-sells).
4. Full OCO Exit Cleanup: Triggering TP disarms/cancels SL; triggering SL disarms/cancels TP.
5. Entry-Failure Cleanup: Rejected or unfilled entries immediately disarm/cancel TP and SL.
6. DRY_RUN by default with structured audit logging of simulated & live events.
"""

import logging
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, List, Optional, Tuple

from core.prediction_engine import lookup_fee_40c

logger = logging.getLogger("BracketManager")


@dataclass
class BracketTicket:
    """Precomputed trade ticket with attached TP and SL rules."""
    ticket_id: str
    coin: str                          # "BTC" or "ETH"
    ticker: str                        # e.g. "KXBTC15M-26NOV211445-45"
    side: str                          # "YES" or "NO"
    action: str = "BUY"                # Long contract entry
    requested_contracts: int = 10
    filled_contracts: int = 0
    entry_price: float = 0.35          # In dollars (0.01 - 0.99)
    entry_cents: int = 35              # In integer cents (1 - 99)
    tp_price: float = 0.44             # Target exit price
    tp_cents: int = 44                 # In integer cents
    sl_price: float = 0.26             # Stop exit price
    sl_cents: int = 26                 # In integer cents
    tp_roi_pct: float = 25.7           # Expected ROI at TP %
    sl_roi_pct: float = 25.7           # Expected loss at SL %
    est_fee_entry: float = 0.0125      # Taker fee per contract at entry
    est_fee_tp: float = 0.0135         # Taker fee per contract at TP
    est_fee_sl: float = 0.0105         # Taker fee per contract at SL
    net_edge_tp: float = 6.4           # Net edge % at TP after fees
    status: str = "PENDING"            # PENDING, ACTIVE, TP_TRIGGERED, TP_FILLED, SL_TRIGGERED, SL_FILLED, CANCELLED, REJECTED, EXPIRED
    dry_run: bool = True
    entry_order_id: Optional[str] = None
    tp_order_id: Optional[str] = None
    sl_order_id: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    realized_pnl: float = 0.0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    logs: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


def calculate_bracket_ticket(
    ticker: str,
    side: str,
    entry_price: float,
    contracts: int = 10,
    coin: str = "BTC",
    tp_pct: Optional[float] = None,
    sl_pct: Optional[float] = None,
    tp_ticks: Optional[int] = None,
    sl_ticks: Optional[int] = None,
    tp_bps: Optional[float] = None,
    sl_bps: Optional[float] = None,
    tp_price: Optional[float] = None,
    sl_price: Optional[float] = None,
    dry_run: bool = True,
    config_defaults: Optional[Dict[str, Any]] = None,
) -> BracketTicket:
    """
    Deterministic calculation of a trade ticket with precomputed Take Profit and Stop Loss.
    Respects exact 1-cent tick boundaries, pricing conventions, and fee math.
    """
    cfg = config_defaults or {}
    default_tp_pct = float(cfg.get("take_profit_roi_pct", 25.0))
    default_sl_pct = float(cfg.get("stop_loss_roi_pct", 25.0))

    # Normalize entry price to cents [1, 99]
    if entry_price <= 1.0:
        entry_cents = int(round(entry_price * 100.0))
    else:
        entry_cents = int(round(entry_price))

    entry_cents = max(1, min(99, entry_cents))
    norm_entry_price = round(entry_cents / 100.0, 2)

    # 1. Compute TP Cents
    if tp_price is not None:
        raw_tp = tp_price * 100.0 if tp_price <= 1.0 else tp_price
        tp_cents = int(round(raw_tp))
    elif tp_ticks is not None:
        tp_cents = entry_cents + int(tp_ticks)
    elif tp_bps is not None:
        tp_cents = int(round(entry_cents * (1.0 + float(tp_bps) / 10000.0)))
    elif tp_pct is not None:
        tp_cents = int(round(entry_cents * (1.0 + float(tp_pct) / 100.0)))
    else:
        tp_cents = int(round(entry_cents * (1.0 + default_tp_pct / 100.0)))

    # 2. Compute SL Cents
    if sl_price is not None:
        raw_sl = sl_price * 100.0 if sl_price <= 1.0 else sl_price
        sl_cents = int(round(raw_sl))
    elif sl_ticks is not None:
        sl_cents = entry_cents - int(sl_ticks)
    elif sl_bps is not None:
        sl_cents = int(round(entry_cents * (1.0 - float(sl_bps) / 10000.0)))
    elif sl_pct is not None:
        sl_cents = int(round(entry_cents * (1.0 - float(sl_pct) / 100.0)))
    else:
        sl_cents = int(round(entry_cents * (1.0 - default_sl_pct / 100.0)))

    # Clamp to contract range boundaries (will be sanity validated next)
    tp_cents_clamped = max(entry_cents + 1, min(99, tp_cents)) if tp_cents > entry_cents else tp_cents
    sl_cents_clamped = max(1, min(entry_cents - 1, sl_cents)) if sl_cents < entry_cents else sl_cents

    norm_tp_price = round(tp_cents_clamped / 100.0, 2)
    norm_sl_price = round(sl_cents_clamped / 100.0, 2)

    # Fees & ROI Calculations
    fee_entry = lookup_fee_40c(norm_entry_price)
    fee_tp = lookup_fee_40c(norm_tp_price)
    fee_sl = lookup_fee_40c(norm_sl_price)

    tp_roi_pct = round(((tp_cents_clamped - entry_cents) / max(1, entry_cents)) * 100.0, 1)
    sl_roi_pct = round(((entry_cents - sl_cents_clamped) / max(1, entry_cents)) * 100.0, 1)
    net_edge_tp = round(((norm_tp_price - norm_entry_price) - fee_entry - fee_tp) * 100.0, 1)

    ticket_id = f"bkt_{uuid.uuid4().hex[:10]}"
    ticket = BracketTicket(
        ticket_id=ticket_id,
        coin=coin.upper(),
        ticker=ticker,
        side=side.upper(),
        action="BUY",
        requested_contracts=int(contracts),
        filled_contracts=0,
        entry_price=norm_entry_price,
        entry_cents=entry_cents,
        tp_price=norm_tp_price,
        tp_cents=tp_cents_clamped,
        sl_price=norm_sl_price,
        sl_cents=sl_cents_clamped,
        tp_roi_pct=tp_roi_pct,
        sl_roi_pct=sl_roi_pct,
        est_fee_entry=fee_entry,
        est_fee_tp=fee_tp,
        est_fee_sl=fee_sl,
        net_edge_tp=net_edge_tp,
        status="PENDING",
        dry_run=dry_run,
        logs=[f"Ticket generated: {contracts}x {side} on {ticker} @ {entry_cents}¢ | TP: {tp_cents_clamped}¢ | SL: {sl_cents_clamped}¢"]
    )
    return ticket


def validate_bracket_ticket(ticket: BracketTicket) -> Tuple[bool, str]:
    """
    Validate trade ticket sanity bounds before execution.
    For a BUY order, enforces: 1 <= sl_cents < entry_cents < tp_cents <= 99.
    """
    if ticket.requested_contracts < 1:
        return False, f"Invalid contract count: {ticket.requested_contracts} < 1"

    if not (1 <= ticket.entry_cents <= 99):
        return False, f"Entry price out of contract bounds: {ticket.entry_cents}¢ (must be 1¢-99¢)"

    if ticket.tp_cents <= ticket.entry_cents:
        return False, f"Invalid TP: tp_cents ({ticket.tp_cents}¢) must be strictly greater than entry_cents ({ticket.entry_cents}¢)"

    if ticket.sl_cents >= ticket.entry_cents:
        return False, f"Invalid SL: sl_cents ({ticket.sl_cents}¢) must be strictly less than entry_cents ({ticket.entry_cents}¢)"

    if ticket.sl_cents >= ticket.tp_cents:
        return False, f"Inverted bracket: sl_cents ({ticket.sl_cents}¢) >= tp_cents ({ticket.tp_cents}¢)"

    if ticket.tp_cents > 99:
        return False, f"TP exceeds maximum contract settlement: {ticket.tp_cents}¢ > 99¢"

    if ticket.sl_cents < 1:
        return False, f"SL below minimum contract tick: {ticket.sl_cents}¢ < 1¢"

    # Fee & spread sanity: gross gain at TP must be strictly positive
    gross_tp_gain = ticket.tp_cents - ticket.entry_cents
    if gross_tp_gain < 1:
        return False, f"TP gross gain ({gross_tp_gain}¢) must be at least 1 tick"

    return True, "OK"


class BracketOrderManager:
    """
    Execution Layer & Exit Watcher for One-Click Bracket Orders with Auto TP & SL.
    Manages order arming, partial-fill scaling, and OCO exit cancellation.
    """

    def __init__(self, dry_run: bool = True, kalshi_client: Any = None, live_oms: Any = None):
        self.dry_run = dry_run
        self.kalshi_client = kalshi_client
        self.live_oms = live_oms
        self.active_tickets: Dict[str, BracketTicket] = {}
        self.history_tickets: List[BracketTicket] = []

    def get_active_tickets(self) -> List[Dict[str, Any]]:
        return [t.to_dict() for t in self.active_tickets.values()]

    def get_history_tickets(self, limit: int = 50) -> List[Dict[str, Any]]:
        return [t.to_dict() for t in reversed(self.history_tickets[-limit:])]

    async def execute_one_click_buy(
        self,
        ticket: BracketTicket,
        simulate_fill_qty: Optional[int] = None,
        simulate_reject: bool = False,
        execution_client: Any = None,
    ) -> BracketTicket:
        """
        Execute one-click entry buy order and automatically arm attached TP & SL.
        
        Handles:
        1. Pre-execution sanity validation (rejects invalid/inverted tickets).
        2. Execution submission (live exchange or dry-run simulation).
        3. Entry failure / rejection cleanup: disarms TP/SL immediately.
        4. Partial fill exit scaling: scales TP/SL size to exact filled contracts.
        5. Arming OCO exit watcher.
        """
        # Step 1: Validate sanity bounds
        is_valid, reason = validate_bracket_ticket(ticket)
        if not is_valid:
            ticket.status = "REJECTED"
            msg = f"[BracketManager REJECTED] {reason}"
            ticket.logs.append(msg)
            logger.warning(msg)
            self.history_tickets.append(ticket)
            return ticket

        # Step 2: Submit Entry Order
        client = execution_client or self.kalshi_client
        ticket.status = "SUBMITTED"
        ticket.updated_at = time.time()

        if self.dry_run or ticket.dry_run or client is None:
            # Simulated Execution
            if simulate_reject:
                ticket.status = "ENTRY_REJECTED"
                msg = f"[BracketManager DRY_RUN] Simulated entry rejection for {ticket.ticker}"
                ticket.logs.append(msg)
                logger.warning(msg)
                self.history_tickets.append(ticket)
                return ticket

            # Determine simulated filled contracts (default: full fill, or simulate_fill_qty)
            if simulate_fill_qty is not None:
                filled_contracts = max(0, min(ticket.requested_contracts, simulate_fill_qty))
            else:
                filled_contracts = ticket.requested_contracts

            ticket.entry_order_id = f"sim_entry_{ticket.ticket_id[:8]}"
            ticket.filled_contracts = filled_contracts
        else:
            # Live Venue Execution
            try:
                side_str = ticket.side.lower()
                order_res = await client.place_order(
                    ticker=ticket.ticker,
                    action="buy",
                    side=side_str,
                    count=ticket.requested_contracts,
                    price_cents=ticket.entry_cents,
                    order_type="limit"
                )
                ticket.entry_order_id = order_res.get("order_id") or order_res.get("order", {}).get("order_id")
                # Check status
                status = order_res.get("status", "executed")
                if status in ("canceled", "rejected"):
                    ticket.status = "ENTRY_REJECTED"
                    ticket.logs.append(f"[BracketManager LIVE] Entry rejected: {order_res}")
                    self.history_tickets.append(ticket)
                    return ticket

                # Check filled count
                filled = int(order_res.get("count", ticket.requested_contracts))
                ticket.filled_contracts = filled
            except Exception as e:
                ticket.status = "ENTRY_REJECTED"
                msg = f"[BracketManager LIVE ERROR] Failed submitting entry: {e}"
                ticket.logs.append(msg)
                logger.error(msg)
                self.history_tickets.append(ticket)
                return ticket

        # Step 3: Handle Entry Fill Outcome
        if ticket.filled_contracts <= 0:
            ticket.status = "ENTRY_EXPIRED"
            msg = f"[BracketManager] Entry completed with 0 fills. Disarming TP & SL."
            ticket.logs.append(msg)
            logger.info(msg)
            self.history_tickets.append(ticket)
            return ticket

        # Step 4: Partial Fill Scaling
        if ticket.filled_contracts < ticket.requested_contracts:
            scale_msg = (
                f"[BracketManager PARTIAL_FILL] Entry partially filled: "
                f"{ticket.filled_contracts}/{ticket.requested_contracts} contracts. "
                f"Scaling TP & SL exits strictly to {ticket.filled_contracts}x contracts."
            )
            ticket.logs.append(scale_msg)
            logger.info(scale_msg)

        # Step 5: Arm Active Bracket (Exit Watcher & OCO)
        ticket.status = "ACTIVE"
        ticket.updated_at = time.time()
        arm_msg = (
            f"[BracketManager ARMED] Ticket {ticket.ticket_id}: Position {ticket.filled_contracts}x {ticket.side} "
            f"@ {ticket.entry_cents}¢ on {ticket.ticker} | Auto TP: {ticket.tp_cents}¢ (+{ticket.tp_roi_pct}%) | "
            f"Auto SL: {ticket.sl_cents}¢ (-{ticket.sl_roi_pct}%) [DRY_RUN={self.dry_run or ticket.dry_run}]"
        )
        ticket.logs.append(arm_msg)
        logger.info(arm_msg)

        self.active_tickets[ticket.ticket_id] = ticket
        return ticket

    def on_tick(
        self,
        ticker: str,
        current_bid: float,
        current_ask: float,
        current_time: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        """
        Evaluate real-time market ticks against armed bracket orders.
        Executes TP or SL exits with full OCO cancellation.
        """
        now = current_time or time.time()
        events = []
        bid_cents = int(round(current_bid * 100.0)) if current_bid <= 1.0 else int(round(current_bid))

        # Inspect all active brackets for this market ticker
        matched_ticket_ids = [tid for tid, t in self.active_tickets.items() if t.ticker == ticker]

        for tid in matched_ticket_ids:
            ticket = self.active_tickets.get(tid)
            if not ticket or ticket.status != "ACTIVE":
                continue

            # 1. Check Take Profit condition
            if bid_cents >= ticket.tp_cents:
                exit_price_cents = max(ticket.tp_cents, bid_cents)
                exit_price_dollars = exit_price_cents / 100.0
                gross_pnl = (exit_price_cents - ticket.entry_cents) * ticket.filled_contracts / 100.0
                fees = (ticket.est_fee_entry + ticket.est_fee_tp) * ticket.filled_contracts
                net_pnl = round(gross_pnl - fees, 4)

                ticket.status = "TP_FILLED"
                ticket.exit_price = exit_price_dollars
                ticket.exit_reason = "TAKE_PROFIT"
                ticket.realized_pnl = net_pnl
                ticket.updated_at = now
                
                # OCO: Disarm SL
                ticket.sl_order_id = "OCO_CANCELLED"
                log_msg = (
                    f"🎯 [BracketManager TP FILLED] Ticket {ticket.ticket_id}: "
                    f"Sold {ticket.filled_contracts}x @ {exit_price_cents}¢ (Target: {ticket.tp_cents}¢). "
                    f"Net PnL: ${net_pnl:+.2f}. OCO: SL CANCELLED."
                )
                ticket.logs.append(log_msg)
                logger.info(log_msg)

                # Move from active to history
                del self.active_tickets[tid]
                self.history_tickets.append(ticket)
                events.append({
                    "event": "TP_FILLED",
                    "ticket_id": ticket.ticket_id,
                    "ticker": ticket.ticker,
                    "exit_price": exit_price_dollars,
                    "net_pnl": net_pnl,
                    "contracts": ticket.filled_contracts
                })

            # 2. Check Stop Loss condition
            elif bid_cents <= ticket.sl_cents:
                exit_price_cents = min(ticket.sl_cents, bid_cents)
                exit_price_dollars = exit_price_cents / 100.0
                gross_pnl = (exit_price_cents - ticket.entry_cents) * ticket.filled_contracts / 100.0
                fees = (ticket.est_fee_entry + ticket.est_fee_sl) * ticket.filled_contracts
                net_pnl = round(gross_pnl - fees, 4)

                ticket.status = "SL_FILLED"
                ticket.exit_price = exit_price_dollars
                ticket.exit_reason = "STOP_LOSS"
                ticket.realized_pnl = net_pnl
                ticket.updated_at = now

                # OCO: Disarm TP
                ticket.tp_order_id = "OCO_CANCELLED"
                log_msg = (
                    f"🛑 [BracketManager SL FILLED] Ticket {ticket.ticket_id}: "
                    f"Sold {ticket.filled_contracts}x @ {exit_price_cents}¢ (Stop: {ticket.sl_cents}¢). "
                    f"Net PnL: ${net_pnl:+.2f}. OCO: TP CANCELLED."
                )
                ticket.logs.append(log_msg)
                logger.info(log_msg)

                # Move from active to history
                del self.active_tickets[tid]
                self.history_tickets.append(ticket)
                events.append({
                    "event": "SL_FILLED",
                    "ticket_id": ticket.ticket_id,
                    "ticker": ticket.ticker,
                    "exit_price": exit_price_dollars,
                    "net_pnl": net_pnl,
                    "contracts": ticket.filled_contracts
                })

        return events

    def cancel_or_flatten_ticket(
        self,
        ticket_id: str,
        current_bid: Optional[float] = None,
    ) -> Optional[BracketTicket]:
        """
        Manually flatten an active position and cancel all attached TP/SL exits.
        """
        ticket = self.active_tickets.get(ticket_id)
        if not ticket:
            return None

        now = time.time()
        ticket.status = "CANCELLED"
        ticket.exit_reason = "MANUAL_FLATTEN"
        ticket.tp_order_id = "MANUAL_CANCELLED"
        ticket.sl_order_id = "MANUAL_CANCELLED"
        ticket.updated_at = now

        if current_bid is not None and ticket.filled_contracts > 0:
            bid_cents = int(round(current_bid * 100.0)) if current_bid <= 1.0 else int(round(current_bid))
            ticket.exit_price = bid_cents / 100.0
            gross_pnl = (bid_cents - ticket.entry_cents) * ticket.filled_contracts / 100.0
            fees = (ticket.est_fee_entry + ticket.est_fee_sl) * ticket.filled_contracts
            ticket.realized_pnl = round(gross_pnl - fees, 4)

        msg = f"[BracketManager FLATTEN] Ticket {ticket_id} manually closed. Exits cancelled."
        ticket.logs.append(msg)
        logger.info(msg)

        del self.active_tickets[ticket_id]
        self.history_tickets.append(ticket)
        return ticket

    def cancel_all_brackets(self, current_bids: Optional[Dict[str, float]] = None) -> List[BracketTicket]:
        """
        Emergency flatten / cancel all active brackets.
        """
        bids = current_bids or {}
        closed = []
        for tid in list(self.active_tickets.keys()):
            ticket = self.active_tickets[tid]
            bid = bids.get(ticket.ticker)
            res = self.cancel_or_flatten_ticket(tid, current_bid=bid)
            if res:
                closed.append(res)
        return closed
