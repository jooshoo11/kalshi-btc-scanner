#!/usr/bin/env python3
"""
run_live_bot.py — THIN CLI entrypoint (AGENT.md: multi-asset execution orchestrator).

All trading logic lives in app.py + core/ (single source of truth):
  - MultiCoinPaperEngine (core/multi_coin_trader.py) is the ONLY trading engine
    (BTC/ETH 50/50 pinned, SOL/XRP disabled, TIME_EXIT after max_hold_seconds=120s —
    positions are never held to expiry).
  - SAFETY (agreed condition 1): real order placement is armed ONLY with
    KALSHI_LIVE_TRADING=1 in the environment, enforced inside
    core/live_executor.py __init__. Without it the OMS is dry_run and NO real
    orders/cancels are ever sent to the exchange.

Usage:
  python3 run_live_bot.py             # headless paper mode (no web server)
  python3 run_live_bot.py --serve     # same as `python3 app.py` (dashboard on :8088)
  KALSHI_LIVE_TRADING=1 python3 run_live_bot.py   # arm the live OMS (use with caution)
"""
import argparse
import asyncio
import os
import signal


def main():
    parser = argparse.ArgumentParser(description="Kalshi multi-coin bot CLI (thin wrapper around app.py)")
    parser.add_argument("--serve", action="store_true", help="start the web dashboard on :8088 (same as app.py)")
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

    live_armed = os.environ.get("KALSHI_LIVE_TRADING", "0").strip().lower() in ("1", "true", "yes")
    print(f"[run_live_bot] KALSHI_LIVE_TRADING={'1' if live_armed else 'unset'} -> "
          f"{'LIVE OMS ARMED' if live_armed else 'PAPER mode (dry_run — no real orders will be sent)'}")

    if args.serve:
        import uvicorn
        uvicorn.run("app:app", host="0.0.0.0", port=8088, log_level="info")
        return

    import app  # builds all engines; OMS dry_run is env-gated inside core/live_executor.py

    async def runner():
        # Cold-boot reconciliation (fail-closed: orphans block entries, never auto-cancel)
        try:
            report = await app.live_oms.reconcile_state_on_startup(
                db_path_or_db=app.live_db, cancel_resting_orders=False
            )
            print(f"[run_live_bot] Cold-boot reconcile: {report.get('status')}")
        except Exception as e:
            print(f"[run_live_bot] Reconcile notice: {e}")
        # Portfolio-wide baseline from confirmed settlements only (condition 5)
        try:
            pstate = app.init_portfolio_baseline(app._portfolio_engine_dbs())
            print(f"[run_live_bot] Portfolio day-start baseline ${pstate.get('day_start_equity')}")
        except Exception as e:
            print(f"[run_live_bot] Portfolio baseline error: {e}")

        tasks = [
            asyncio.create_task(app.live_ws.connect_and_listen()),
            asyncio.create_task(app.fast_spot_loop()),
            asyncio.create_task(app.full_scan_loop()),
            asyncio.create_task(app.technicals_loop()),
            asyncio.create_task(app.multi_coin_production_loop()),
            asyncio.create_task(app.periodic_reconciliation_loop()),
            asyncio.create_task(app.paper_reconcile_loop()),
            asyncio.create_task(app.kill_switch_watcher()),
        ]

        def handle_exit(sig, frame):
            print("[run_live_bot] Shutdown signal — cancelling loops "
                  "(dry_run: no real exchange cancels are sent).")
            for t in tasks:
                t.cancel()

        signal.signal(signal.SIGINT, handle_exit)
        signal.signal(signal.SIGTERM, handle_exit)
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(runner())


if __name__ == "__main__":
    main()
