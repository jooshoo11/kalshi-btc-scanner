#!/usr/bin/env python3
"""
blue_green_comparator.py — Blue/Green Dual Execution & Fill Comparison Engine.

Enables simultaneous shadow execution:
- Blue: Paper engine (simulated VWAP execution against L2 orderbook)
- Green: Live OMS (real exchange execution via Kalshi V2 REST / WS)

Tracks and compares:
1. Slippage delta (cents): Actual Live fill price vs simulated Paper VWAP
2. Execution latency delta (ms): Signal to live fill ACK vs paper simulated fill
3. Fee accuracy: Exchange non-linear taker fee vs theoretical calculation
4. Rejection & orphan rates
5. Automatic alerting & rollback triggers when live execution breaches SLAs:
   - Max allowable slippage: 2.0¢
   - Max allowable latency: 500ms
"""

import time
import json
import os
import logging
from typing import Dict, Any, List, Optional, Tuple
from collections import deque

logger = logging.getLogger("BlueGreenComparator")

AUDIT_LOG_FILE = "blue_green_audit.json"


class FillRecord:
    """Represents an execution fill record from either Blue (paper) or Green (live)."""
    def __init__(
        self,
        mode: str,  # 'BLUE' or 'GREEN'
        signal_id: str,
        ticker: str,
        side: str,  # 'YES' or 'NO'
        action: str,  # 'BUY' or 'SELL'
        contracts: int,
        price_cents: float,
        fee_dollars: float,
        timestamp: float,
        status: str = "FILLED",
        latency_ms: Optional[float] = None
    ):
        self.mode = mode.upper()
        self.signal_id = signal_id
        self.ticker = ticker
        self.side = side.upper()
        self.action = action.upper()
        self.contracts = contracts
        self.price_cents = price_cents
        self.fee_dollars = fee_dollars
        self.timestamp = timestamp
        self.status = status
        self.latency_ms = latency_ms

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "signal_id": self.signal_id,
            "ticker": self.ticker,
            "side": self.side,
            "action": self.action,
            "contracts": self.contracts,
            "price_cents": self.price_cents,
            "fee_dollars": round(self.fee_dollars, 4),
            "timestamp": self.timestamp,
            "status": self.status,
            "latency_ms": self.latency_ms
        }


class BlueGreenFillComparator:
    """
    Compares Blue (Paper) and Green (Live) execution fills in real time.
    """
    def __init__(
        self,
        max_slippage_cents: float = 2.0,
        max_latency_ms: float = 500.0,
        audit_file: str = AUDIT_LOG_FILE,
        history_len: int = 200
    ):
        self.max_slippage_cents = max_slippage_cents
        self.max_latency_ms = max_latency_ms
        self.audit_file = audit_file

        self._pending_blue: Dict[str, FillRecord] = {}
        self._pending_green: Dict[str, FillRecord] = {}
        self._paired_history: deque = deque(maxlen=history_len)

        # Cumulative metrics
        self.total_paired: int = 0
        self.slippage_breaches: int = 0
        self.latency_breaches: int = 0
        self.live_rejections: int = 0

    def record_blue_fill(
        self,
        signal_id: str,
        ticker: str,
        side: str,
        action: str,
        contracts: int,
        price_cents: float,
        fee_dollars: float,
        timestamp: Optional[float] = None,
        status: str = "FILLED"
    ) -> Optional[Dict[str, Any]]:
        """Record paper execution (Blue environment)."""
        ts = timestamp or time.time()
        record = FillRecord(
            mode="BLUE",
            signal_id=signal_id,
            ticker=ticker,
            side=side,
            action=action,
            contracts=contracts,
            price_cents=price_cents,
            fee_dollars=fee_dollars,
            timestamp=ts,
            status=status
        )

        if signal_id in self._pending_green:
            green_record = self._pending_green.pop(signal_id)
            return self._compare_and_log(record, green_record)
        else:
            self._pending_blue[signal_id] = record
            return None

    def record_green_fill(
        self,
        signal_id: str,
        ticker: str,
        side: str,
        action: str,
        contracts: int,
        price_cents: float,
        fee_dollars: float,
        timestamp: Optional[float] = None,
        status: str = "FILLED",
        latency_ms: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        """Record live exchange execution (Green environment)."""
        ts = timestamp or time.time()
        record = FillRecord(
            mode="GREEN",
            signal_id=signal_id,
            ticker=ticker,
            side=side,
            action=action,
            contracts=contracts,
            price_cents=price_cents,
            fee_dollars=fee_dollars,
            timestamp=ts,
            status=status,
            latency_ms=latency_ms
        )

        if record.status in ("REJECTED", "CANCELLED", "ERROR"):
            self.live_rejections += 1

        if signal_id in self._pending_blue:
            blue_record = self._pending_blue.pop(signal_id)
            return self._compare_and_log(blue_record, record)
        else:
            self._pending_green[signal_id] = record
            return None

    def _compare_and_log(self, blue: FillRecord, green: FillRecord) -> Dict[str, Any]:
        """Pair and evaluate Blue vs Green fill differences."""
        self.total_paired += 1

        # Price delta in cents (for buys, higher live price means slippage against us)
        if green.action == "BUY":
            slippage_cents = green.price_cents - blue.price_cents
        else:
            slippage_cents = blue.price_cents - green.price_cents

        fee_delta_dollars = green.fee_dollars - blue.fee_dollars

        # Latency delta
        latency_delta_ms = green.latency_ms
        if latency_delta_ms is None:
            latency_delta_ms = max(0.0, (green.timestamp - blue.timestamp) * 1000.0)

        # Check threshold SLA breaches
        slippage_breach = abs(slippage_cents) > self.max_slippage_cents
        latency_breach = latency_delta_ms > self.max_latency_ms

        if slippage_breach:
            self.slippage_breaches += 1
            logger.warning(
                f"[BlueGreen SLA Breach] High Slippage on {green.ticker}: "
                f"Live {green.price_cents:.1f}¢ vs Paper {blue.price_cents:.1f}¢ (Delta: {slippage_cents:+.1f}¢ > {self.max_slippage_cents}¢)"
            )

        if latency_breach:
            self.latency_breaches += 1
            logger.warning(
                f"[BlueGreen SLA Breach] High Execution Latency on {green.ticker}: "
                f"{latency_delta_ms:.1f}ms > {self.max_latency_ms}ms"
            )

        comparison = {
            "signal_id": green.signal_id,
            "ticker": green.ticker,
            "side": green.side,
            "action": green.action,
            "timestamp": green.timestamp,
            "blue": blue.to_dict(),
            "green": green.to_dict(),
            "slippage_cents": round(slippage_cents, 2),
            "fee_delta_dollars": round(fee_delta_dollars, 4),
            "latency_delta_ms": round(latency_delta_ms, 1),
            "slippage_breach": slippage_breach,
            "latency_breach": latency_breach,
            "status": "SLA_BREACH" if (slippage_breach or latency_breach) else "OK"
        }

        self._paired_history.append(comparison)
        self._append_audit_log(comparison)

        return comparison

    def _append_audit_log(self, record: Dict[str, Any]) -> None:
        """Persist comparison record to audit log JSON."""
        try:
            entries = []
            if os.path.exists(self.audit_file):
                with open(self.audit_file, "r") as f:
                    try:
                        entries = json.load(f)
                    except Exception:
                        entries = []
            entries.append(record)
            if len(entries) > 500:
                entries = entries[-500:]  # Keep last 500

            tmp = self.audit_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(entries, f, indent=2)
            os.replace(tmp, self.audit_file)
        except Exception as e:
            logger.error(f"[BlueGreenComparator] Failed to append audit log: {e}")

    def get_summary(self) -> Dict[str, Any]:
        """Return aggregate summary metrics for monitoring dashboard."""
        recent = list(self._paired_history)
        count = len(recent)

        if count == 0:
            return {
                "total_paired": self.total_paired,
                "recent_window_size": 0,
                "mean_slippage_cents": 0.0,
                "max_slippage_cents": 0.0,
                "mean_latency_ms": 0.0,
                "slippage_breaches": self.slippage_breaches,
                "latency_breaches": self.latency_breaches,
                "live_rejections": self.live_rejections,
                "sla_status": "HEALTHY",
                "recommendation": "PROCEED"
            }

        mean_slippage = sum(r["slippage_cents"] for r in recent) / count
        max_slippage = max(abs(r["slippage_cents"]) for r in recent)
        mean_latency = sum(r["latency_delta_ms"] for r in recent) / count

        # Determine SLA status
        if self.slippage_breaches > 3 or self.live_rejections > 2:
            sla_status = "CRITICAL_SLA_BREACH"
            recommendation = "REVERT_TO_PAPER"
        elif self.slippage_breaches > 0 or self.latency_breaches > 1:
            sla_status = "DEGRADED"
            recommendation = "MONITOR_CLOSELY"
        else:
            sla_status = "HEALTHY"
            recommendation = "PROCEED"

        return {
            "total_paired": self.total_paired,
            "recent_window_size": count,
            "mean_slippage_cents": round(mean_slippage, 2),
            "max_slippage_cents": round(max_slippage, 2),
            "mean_latency_ms": round(mean_latency, 1),
            "slippage_breaches": self.slippage_breaches,
            "latency_breaches": self.latency_breaches,
            "live_rejections": self.live_rejections,
            "sla_status": sla_status,
            "recommendation": recommendation
        }

    def get_recent_comparisons(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Return the most recent comparison records."""
        items = list(self._paired_history)
        return items[-limit:]


# Global Singleton
blue_green_comparator = BlueGreenFillComparator()
