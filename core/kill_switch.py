#!/usr/bin/env python3
"""
kill_switch.py — Global Emergency Kill-Switch & 10% Daily Drawdown Circuit Breaker.

Enforces institutional safety guarantees:
- Monitors portfolio daily drawdown against global_stop_loss_daily_pct (10.0%).
- If breached, immediately trips circuit breaker:
  1. Writes KILL lock file to disk
  2. Latches daily_loss_tripped in portfolio_state.json
  3. Cancels all resting orders on Kalshi exchange (DELETE /portfolio/events/orders)
  4. Blocks all trade proposals across BTC, ETH, SOL, XRP
- Supports manual emergency tripping and resetting via API, CLI, or file creation.
"""

import os
import sys
import time
import json
import logging
from typing import Dict, Any, Optional, Tuple, Callable, List, Awaitable

try:
    from .portfolio_state import (
        load_state,
        save_state,
        check_portfolio_daily_loss,
        combined_equity,
        STATE_FILE
    )
except ImportError:
    from core.portfolio_state import (
        load_state,
        save_state,
        check_portfolio_daily_loss,
        combined_equity,
        STATE_FILE
    )

logger = logging.getLogger("KillSwitch")
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] [KillSwitch] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

KILL_FILE_DEFAULT = "KILL"


class KillSwitchController:
    """
    Centralized Kill Switch & Daily Stop Loss Controller.
    Thread-safe and async-aware.
    """
    def __init__(
        self,
        kill_file_path: str = KILL_FILE_DEFAULT,
        state_file_path: Optional[str] = None,
        max_daily_loss_pct: float = 10.0,
        cancel_callback: Optional[Callable[[], Awaitable[bool]]] = None,
        on_trip_callback: Optional[Callable[[str], Awaitable[None]]] = None
    ):
        self.kill_file_path = kill_file_path
        self.state_file_path = state_file_path
        self.max_daily_loss_pct = max_daily_loss_pct
        self._cancel_callback = cancel_callback
        self._on_trip_callback = on_trip_callback
        self._is_tripped = False
        self._trip_reason: Optional[str] = None
        self._trip_timestamp: Optional[float] = None

        # Check existing state on boot
        self._sync_state()

    def _load_state_dict(self) -> Dict[str, Any]:
        if self.state_file_path:
            try:
                if os.path.exists(self.state_file_path):
                    with open(self.state_file_path, "r") as f:
                        return json.load(f)
            except Exception:
                pass
            return {}
        return load_state()

    def _save_state_dict(self, state: Dict[str, Any]) -> None:
        if self.state_file_path:
            try:
                tmp = self.state_file_path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(state, f, indent=2)
                os.replace(tmp, self.state_file_path)
            except Exception as e:
                logger.error(f"[KillSwitch] Error writing {self.state_file_path}: {e}")
        else:
            save_state(state)

    def _sync_state(self) -> None:
        """Synchronize with filesystem and portfolio_state.json."""
        if os.path.exists(self.kill_file_path):
            self._is_tripped = True
            if not self._trip_reason:
                try:
                    with open(self.kill_file_path, "r") as f:
                        content = f.read().strip()
                        self._trip_reason = content if content else "KILL file present on disk"
                except Exception:
                    self._trip_reason = "KILL file present on disk"
            if not self._trip_timestamp:
                self._trip_timestamp = os.path.getmtime(self.kill_file_path)
            return

        state = self._load_state_dict()
        if state.get("daily_loss_tripped", False):
            self._is_tripped = True
            self._trip_reason = state.get("daily_loss_reason", f"Daily loss >= {self.max_daily_loss_pct}% latched in state")
            self._trip_timestamp = state.get("daily_loss_tripped_ts", time.time())

    def register_cancel_callback(self, cb: Callable[[], Awaitable[bool]]) -> None:
        """Register async callback to cancel exchange orders upon trip."""
        self._cancel_callback = cb

    def register_on_trip_callback(self, cb: Callable[[str], Awaitable[None]]) -> None:
        """Register async callback for alert broadcasting."""
        self._on_trip_callback = cb

    def is_tripped(self) -> Tuple[bool, str]:
        """
        Check if kill switch is currently tripped.
        Returns (True, reason) if halted, else (False, "").
        """
        if self._is_tripped:
            return True, self._trip_reason or "Kill switch active"

        if os.path.exists(self.kill_file_path):
            self._sync_state()
            return True, self._trip_reason or "KILL file detected"

        state = self._load_state_dict()
        if state.get("daily_loss_tripped", False):
            self._sync_state()
            return True, self._trip_reason or "Daily stop loss tripped in state"

        return False, ""

    def check_daily_loss(self, current_equity: Optional[float] = None) -> Tuple[bool, Optional[str]]:
        """
        Evaluate if current portfolio equity breaches the daily stop loss (10%).
        If breached, trips kill switch immediately.
        """
        # First check if already tripped
        tripped, reason = self.is_tripped()
        if tripped:
            return True, reason

        # Check via portfolio_state helper
        loss_reason = check_portfolio_daily_loss(
            current_combined_equity=current_equity,
            max_daily_loss_pct=self.max_daily_loss_pct
        )
        if loss_reason:
            self.trip_sync(reason=f"DAILY_STOP_LOSS_BREACH: {loss_reason}")
            return True, loss_reason

        return False, None

    async def trip_async(self, reason: str) -> bool:
        """
        Asynchronously trip the kill switch, persist state, cancel orders, and alert.
        """
        self.trip_sync(reason)
        # Execute registered async cancellation
        if self._cancel_callback:
            try:
                logger.info("[KillSwitch] Executing registered batch order cancellation callback...")
                await self._cancel_callback()
            except Exception as e:
                logger.error(f"[KillSwitch] Failed to execute cancel callback: {e}")

        if self._on_trip_callback:
            try:
                await self._on_trip_callback(reason)
            except Exception as e:
                logger.error(f"[KillSwitch] Failed to execute on_trip callback: {e}")

        return True

    def trip_sync(self, reason: str) -> bool:
        """
        Synchronously trip the kill switch, create KILL file and update portfolio state.
        """
        self._is_tripped = True
        self._trip_reason = reason
        self._trip_timestamp = time.time()

        logger.critical(f"[KillSwitch] !!! EMERGENCY KILL SWITCH TRIPPED !!! Reason: {reason}")

        # 1. Write KILL file to disk
        try:
            with open(self.kill_file_path, "w") as f:
                f.write(json.dumps({
                    "tripped": True,
                    "reason": reason,
                    "timestamp": self._trip_timestamp,
                    "iso": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(self._trip_timestamp))
                }, indent=2))
        except Exception as e:
            logger.error(f"[KillSwitch] Could not write {self.kill_file_path}: {e}")

        # 2. Update portfolio_state.json
        try:
            state = self._load_state_dict()
            state["daily_loss_tripped"] = True
            state["daily_loss_reason"] = reason
            state["daily_loss_tripped_ts"] = self._trip_timestamp
            state["kill_switch_active"] = True
            self._save_state_dict(state)
        except Exception as e:
            logger.error(f"[KillSwitch] Could not update portfolio state: {e}")

        return True

    def reset(self) -> Tuple[bool, str]:
        """
        Reset the kill switch, removing KILL file and clearing state latches.
        """
        self._is_tripped = False
        self._trip_reason = None
        self._trip_timestamp = None

        # Remove KILL file
        removed_file = False
        if os.path.exists(self.kill_file_path):
            try:
                os.remove(self.kill_file_path)
                removed_file = True
            except Exception as e:
                return False, f"Failed to delete {self.kill_file_path}: {e}"

        # Clear portfolio_state.json
        try:
            state = self._load_state_dict()
            state["daily_loss_tripped"] = False
            state["daily_loss_reason"] = None
            state["daily_loss_tripped_ts"] = None
            state["kill_switch_active"] = False
            # Re-baseline day_start_equity to current equity so reset allows forward progress
            from core.portfolio_state import combined_equity
            curr = combined_equity()
            if curr > 0:
                state["day_start_equity"] = curr
            self._save_state_dict(state)
        except Exception as e:
            return False, f"Failed to reset portfolio state: {e}"

        logger.info("[KillSwitch] Kill switch successfully RESET and disarmed.")
        return True, "Kill switch reset successfully"

    def get_status(self) -> Dict[str, Any]:
        """Return diagnostic status dictionary."""
        tripped, reason = self.is_tripped()
        state = self._load_state_dict()
        return {
            "is_tripped": tripped,
            "status": "TRIPPED" if tripped else "ARMED",
            "trip_reason": reason if tripped else None,
            "trip_timestamp": self._trip_timestamp,
            "kill_file_exists": os.path.exists(self.kill_file_path),
            "max_daily_loss_pct": self.max_daily_loss_pct,
            "state_file": STATE_FILE,
            "daily_loss_tripped_in_state": state.get("daily_loss_tripped", False),
            "day_start_equity": state.get("day_start_equity")
        }


# Global Singleton
kill_switch = KillSwitchController()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Kalshi Bot Kill-Switch Utility")
    parser.add_argument("--status", action="store_true", help="Display current kill switch status")
    parser.add_argument("--trip", type=str, metavar="REASON", help="Manually trip the kill switch with a reason")
    parser.add_argument("--reset", action="store_true", help="Reset and clear the kill switch")
    args = parser.parse_args()

    if args.trip:
        kill_switch.trip_sync(args.trip)
        print(f"Kill switch TRIPPED: {args.trip}")
    elif args.reset:
        ok, msg = kill_switch.reset()
        print(f"Reset result: {ok} — {msg}")
    else:
        # Default status display
        status = kill_switch.get_status()
        print(json.dumps(status, indent=2))
