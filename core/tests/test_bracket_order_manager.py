"""
core/tests/test_bracket_order_manager.py — Unit Tests for One-Click Bracket Order Execution Layer.

Covers:
1. Precomputed TP & SL generation across % ROI, ticks, bps, and exact price with 1-cent tick rounding.
2. Inverted and invalid TP/SL rejection.
3. One-click entry execution with auto TP/SL arming in DRY_RUN mode.
4. Partial-fill exit sizing: exit size strictly scales to filled contracts (never over-sells).
5. OCO cancellation: TP hit cancels SL; SL hit cancels TP.
6. Entry failure & rejection cleanup: TP and SL disarmed immediately.
7. Manual flatten / cancel all active brackets.
"""

import pytest
from core.bracket_order_manager import (
    BracketTicket,
    BracketOrderManager,
    calculate_bracket_ticket,
    validate_bracket_ticket,
)


def test_calculate_bracket_ticket_defaults():
    """Verify default 25% TP and 25% SL calculation and 1-cent tick rounding."""
    ticket = calculate_bracket_ticket(
        ticker="KXBTC15M-TEST",
        side="YES",
        entry_price=0.40,
        contracts=10,
        coin="BTC",
        tp_pct=25.0,
        sl_pct=25.0,
        dry_run=True,
    )
    assert ticket.entry_cents == 40
    assert ticket.entry_price == 0.40
    # 40 * 1.25 = 50 cents
    assert ticket.tp_cents == 50
    assert ticket.tp_price == 0.50
    # 40 * 0.75 = 30 cents
    assert ticket.sl_cents == 30
    assert ticket.sl_price == 0.30
    assert ticket.requested_contracts == 10
    assert ticket.status == "PENDING"
    assert ticket.dry_run is True

    # Valid bounds
    is_valid, reason = validate_bracket_ticket(ticket)
    assert is_valid is True
    assert reason == "OK"


def test_calculate_bracket_ticket_ticks_and_bps():
    """Verify TP and SL overrides via ticks and basis points."""
    # Using ticks
    ticket_ticks = calculate_bracket_ticket(
        ticker="KXETH15M-TEST",
        side="NO",
        entry_price=0.35,
        contracts=5,
        coin="ETH",
        tp_ticks=8,
        sl_ticks=6,
    )
    assert ticket_ticks.entry_cents == 35
    assert ticket_ticks.tp_cents == 43
    assert ticket_ticks.sl_cents == 29
    is_valid, _ = validate_bracket_ticket(ticket_ticks)
    assert is_valid is True

    # Using basis points (e.g. 2000 bps = 20%)
    ticket_bps = calculate_bracket_ticket(
        ticker="KXBTC15M-TEST",
        side="YES",
        entry_price=0.50,
        contracts=8,
        tp_bps=2000.0,
        sl_bps=1500.0,
    )
    assert ticket_bps.entry_cents == 50
    assert ticket_bps.tp_cents == 60  # 50 * 1.20
    assert ticket_bps.sl_cents == 42 or ticket_bps.sl_cents == 43  # 50 * 0.85 = 42.5 -> 42 or 43
    is_valid, _ = validate_bracket_ticket(ticket_bps)
    assert is_valid is True


def test_calculate_bracket_ticket_exact_price():
    """Verify absolute price overrides."""
    ticket = calculate_bracket_ticket(
        ticker="KXBTC15M-TEST",
        side="YES",
        entry_price=0.28,
        tp_price=0.45,
        sl_price=0.18,
    )
    assert ticket.entry_cents == 28
    assert ticket.tp_cents == 45
    assert ticket.sl_cents == 18
    is_valid, _ = validate_bracket_ticket(ticket)
    assert is_valid is True


def test_invalid_and_inverted_rejections():
    """Verify rejection of inverted and invalid TP/SL tickets."""
    # 1. Inverted: TP <= Entry
    t1 = calculate_bracket_ticket(ticker="T", side="YES", entry_price=0.40, tp_price=0.38, sl_price=0.30)
    is_valid, msg = validate_bracket_ticket(t1)
    assert is_valid is False
    assert "Invalid TP" in msg

    # 2. Inverted: SL >= Entry
    t2 = calculate_bracket_ticket(ticker="T", side="YES", entry_price=0.40, tp_price=0.50, sl_price=0.42)
    is_valid, msg = validate_bracket_ticket(t2)
    assert is_valid is False
    assert "Invalid SL" in msg

    # 3. Inverted: SL >= TP
    t3 = BracketTicket(
        ticket_id="inv", coin="BTC", ticker="T", side="YES", requested_contracts=5,
        entry_price=0.40, entry_cents=40, tp_price=0.35, tp_cents=35, sl_price=0.45, sl_cents=45
    )
    is_valid, msg = validate_bracket_ticket(t3)
    assert is_valid is False

    # 4. Out of bounds: TP > 99
    t4 = BracketTicket(
        ticket_id="oob_tp", coin="BTC", ticker="T", side="YES", requested_contracts=5,
        entry_price=0.85, entry_cents=85, tp_price=1.05, tp_cents=105, sl_price=0.70, sl_cents=70
    )
    is_valid, msg = validate_bracket_ticket(t4)
    assert is_valid is False
    assert "exceeds maximum" in msg

    # 5. Out of bounds: SL < 1
    t5 = BracketTicket(
        ticket_id="oob_sl", coin="BTC", ticker="T", side="YES", requested_contracts=5,
        entry_price=0.10, entry_cents=10, tp_price=0.20, tp_cents=20, sl_price=0.00, sl_cents=0
    )
    is_valid, msg = validate_bracket_ticket(t5)
    assert is_valid is False
    assert "below minimum" in msg

    # 6. Zero or negative contracts
    t6 = calculate_bracket_ticket(ticker="T", side="YES", entry_price=0.40, contracts=0)
    is_valid, msg = validate_bracket_ticket(t6)
    assert is_valid is False
    assert "contract count" in msg


@pytest.mark.asyncio
async def test_one_click_buy_full_fill_arming():
    """Verify one-click buy executes entry and arms TP and SL in active tickets."""
    mgr = BracketOrderManager(dry_run=True)
    ticket = calculate_bracket_ticket(
        ticker="KXBTC15M-MOCK",
        side="YES",
        entry_price=0.32,
        contracts=15,
        tp_pct=25.0,
        sl_pct=25.0,
    )
    res = await mgr.execute_one_click_buy(ticket)
    assert res.status == "ACTIVE"
    assert res.filled_contracts == 15
    assert res.ticket_id in mgr.active_tickets
    assert mgr.active_tickets[res.ticket_id].tp_cents > 32
    assert mgr.active_tickets[res.ticket_id].sl_cents < 32


@pytest.mark.asyncio
async def test_partial_fill_exit_scaling():
    """Verify that if an entry partially fills, TP and SL exit sizes scale strictly to filled count."""
    mgr = BracketOrderManager(dry_run=True)
    ticket = calculate_bracket_ticket(
        ticker="KXETH15M-MOCK",
        side="NO",
        entry_price=0.25,
        contracts=40,  # requested 40
        tp_pct=30.0,
        sl_pct=20.0,
    )
    # Simulate partial fill of 14 contracts
    res = await mgr.execute_one_click_buy(ticket, simulate_fill_qty=14)
    assert res.status == "ACTIVE"
    assert res.requested_contracts == 40
    assert res.filled_contracts == 14
    # The active bracket must only hold 14 contracts to prevent over-selling
    assert mgr.active_tickets[res.ticket_id].filled_contracts == 14


@pytest.mark.asyncio
async def test_entry_rejection_cleanup():
    """Verify that an entry rejection disarms and cleans up TP & SL immediately."""
    mgr = BracketOrderManager(dry_run=True)
    ticket = calculate_bracket_ticket(
        ticker="KXBTC15M-MOCK",
        side="YES",
        entry_price=0.30,
        contracts=10,
    )
    res = await mgr.execute_one_click_buy(ticket, simulate_reject=True)
    assert res.status == "ENTRY_REJECTED"
    assert res.filled_contracts == 0
    # Must NOT be in active tickets
    assert res.ticket_id not in mgr.active_tickets
    assert len(mgr.history_tickets) == 1


@pytest.mark.asyncio
async def test_entry_zero_fills_expiration():
    """Verify entry that fills 0 contracts is expired and disarms exits."""
    mgr = BracketOrderManager(dry_run=True)
    ticket = calculate_bracket_ticket(
        ticker="KXBTC15M-MOCK",
        side="YES",
        entry_price=0.30,
        contracts=10,
    )
    res = await mgr.execute_one_click_buy(ticket, simulate_fill_qty=0)
    assert res.status == "ENTRY_EXPIRED"
    assert res.ticket_id not in mgr.active_tickets


@pytest.mark.asyncio
async def test_oco_take_profit_triggers_and_cancels_sl():
    """Verify that when current bid reaches TP, position is closed and SL is cancelled (OCO)."""
    mgr = BracketOrderManager(dry_run=True)
    ticket = calculate_bracket_ticket(
        ticker="KXBTC15M-OCO",
        side="YES",
        entry_price=0.40,
        contracts=10,
        tp_price=0.50,
        sl_price=0.30,
    )
    res = await mgr.execute_one_click_buy(ticket)
    assert res.status == "ACTIVE"
    tid = res.ticket_id

    # Market tick: Bid moves to 0.45 (between SL and TP) -> no action
    events1 = mgr.on_tick(ticker="KXBTC15M-OCO", current_bid=0.45, current_ask=0.46)
    assert len(events1) == 0
    assert tid in mgr.active_tickets

    # Market tick: Bid jumps to 0.51 (>= TP 0.50) -> TP triggers!
    events2 = mgr.on_tick(ticker="KXBTC15M-OCO", current_bid=0.51, current_ask=0.52)
    assert len(events2) == 1
    assert events2[0]["event"] == "TP_FILLED"
    assert events2[0]["ticket_id"] == tid

    # Verify OCO state: ticket removed from active, marked TP_FILLED, SL cancelled
    assert tid not in mgr.active_tickets
    hist = mgr.history_tickets[-1]
    assert hist.ticket_id == tid
    assert hist.status == "TP_FILLED"
    assert hist.sl_order_id == "OCO_CANCELLED"
    assert hist.exit_price == 0.51
    assert hist.realized_pnl > 0


@pytest.mark.asyncio
async def test_oco_stop_loss_triggers_and_cancels_tp():
    """Verify that when current bid drops to SL, position is stopped out and TP is cancelled (OCO)."""
    mgr = BracketOrderManager(dry_run=True)
    ticket = calculate_bracket_ticket(
        ticker="KXETH15M-OCO",
        side="NO",
        entry_price=0.35,
        contracts=20,
        tp_price=0.45,
        sl_price=0.25,
    )
    res = await mgr.execute_one_click_buy(ticket)
    assert res.status == "ACTIVE"
    tid = res.ticket_id

    # Market tick: Bid drops to 0.24 (<= SL 0.25) -> SL triggers!
    events = mgr.on_tick(ticker="KXETH15M-OCO", current_bid=0.24, current_ask=0.25)
    assert len(events) == 1
    assert events[0]["event"] == "SL_FILLED"

    # Verify OCO state: ticket removed from active, marked SL_FILLED, TP cancelled
    assert tid not in mgr.active_tickets
    hist = mgr.history_tickets[-1]
    assert hist.ticket_id == tid
    assert hist.status == "SL_FILLED"
    assert hist.tp_order_id == "OCO_CANCELLED"
    assert hist.exit_price == 0.24
    assert hist.realized_pnl < 0


@pytest.mark.asyncio
async def test_manual_flatten_cancels_all_exits():
    """Verify manual flatten closes position and cancels attached TP and SL exits."""
    mgr = BracketOrderManager(dry_run=True)
    ticket = calculate_bracket_ticket(
        ticker="KXBTC15M-FLAT",
        side="YES",
        entry_price=0.40,
        contracts=10,
        tp_price=0.50,
        sl_price=0.30,
    )
    res = await mgr.execute_one_click_buy(ticket)
    tid = res.ticket_id
    assert tid in mgr.active_tickets

    # User manually flattens at current market bid 0.42
    flat_res = mgr.cancel_or_flatten_ticket(ticket_id=tid, current_bid=0.42)
    assert flat_res is not None
    assert flat_res.status == "CANCELLED"
    assert flat_res.exit_reason == "MANUAL_FLATTEN"
    assert flat_res.tp_order_id == "MANUAL_CANCELLED"
    assert flat_res.sl_order_id == "MANUAL_CANCELLED"
    assert flat_res.exit_price == 0.42
    assert tid not in mgr.active_tickets


def test_api_trade_bracket_endpoints():
    """Verify FastAPI endpoints for one-click bracket trading, preview, and cancellation."""
    from starlette.testclient import TestClient
    from app import app

    client = TestClient(app)

    # 1. Preview ticket endpoint
    prev_resp = client.get("/api/trade/ticket/preview?ticker=KXBTC15M-TEST&side=YES&entry_price=0.36&contracts=10&tp_pct=25&sl_pct=25")
    assert prev_resp.status_code == 200
    prev_data = prev_resp.json()
    assert prev_data["entry_cents"] == 36
    assert prev_data["tp_cents"] == 45
    assert prev_data["sl_cents"] == 27
    assert prev_data["is_valid"] is True

    # 2. Submit one-click buy with auto TP/SL
    buy_resp = client.post("/api/trade/bracket", json={
        "ticker": "KXBTC15M-TEST",
        "side": "YES",
        "coin": "BTC",
        "entry_price": 0.36,
        "contracts": 10,
        "tp_pct": 25.0,
        "sl_pct": 25.0,
        "dry_run": True
    })
    assert buy_resp.status_code == 200
    buy_data = buy_resp.json()
    assert buy_data["status"] == "success"
    t = buy_data["ticket"]
    tid = t["ticket_id"]
    assert t["status"] == "ACTIVE"
    assert t["entry_cents"] == 36
    assert t["tp_cents"] == 45
    assert t["sl_cents"] == 27

    # 3. Retrieve brackets
    list_resp = client.get("/api/trade/brackets")
    assert list_resp.status_code == 200
    list_data = list_resp.json()
    active_ids = [b["ticket_id"] for b in list_data["active"]]
    assert tid in active_ids

    # 4. Inverted rejection endpoint check
    bad_resp = client.post("/api/trade/bracket", json={
        "ticker": "KXBTC15M-TEST",
        "side": "YES",
        "coin": "BTC",
        "entry_price": 0.40,
        "tp_price": 0.30,  # invalid TP < entry
        "sl_price": 0.20,
        "dry_run": True
    })
    assert bad_resp.status_code == 400
    bad_data = bad_resp.json()
    assert bad_data["status"] == "rejected"
    assert "Invalid TP" in bad_data["ticket"]["logs"][1]

    # 5. Cancel / Flatten active ticket
    cancel_resp = client.post("/api/trade/bracket/cancel", json={"ticket_id": tid})
    assert cancel_resp.status_code == 200
    cancel_data = cancel_resp.json()
    assert cancel_data["status"] == "success"
    assert cancel_data["ticket"]["status"] == "CANCELLED"

