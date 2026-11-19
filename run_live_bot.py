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
import json
import os
import signal
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent


def load_json(path: Path, default=None):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"[run_live_bot] WARNING: config not found at {path}, using defaults")
        return default or {}
    except json.JSONDecodeError as e:
        print(f"[run_live_bot] ERROR: invalid JSON in {path}: {e}")
        return default or {}


def main():
    parser = argparse.ArgumentParser(description="Kalshi multi-coin bot CLI (thin wrapper around app.py)")
    parser.add_argument("--mode", choices=["paper", "live"], default=None, help="trading mode: 'paper' (dry-run simulation) or 'live' (Kalshi OMS)")
    parser.add_argument("--serve", action="store_true", help="start the web dashboard on :8088 (same as app.py)")
    parser.add_argument("--profile", action="store_true", help="enable cProfile CPU profiling")
    parser.add_argument("--profile-mem", action="store_true", help="enable memory_profiler and tracemalloc memory profiling")
    parser.add_argument("--profile-duration", type=float, default=0.0, help="stop bot and dump profiles after N seconds")
    parser.add_argument("--profile-output", type=str, default="profiles", help="directory to store profile artifacts")
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv(os.path.join(REPO_ROOT, ".env"))

    # Load global risk config
    global_risk = load_json(REPO_ROOT / "global_risk.json")
    active_assets = global_risk.get("active_assets", ["BTC", "ETH"])

    # Load per-coin configs and build enabled asset list
    coin_configs = {}
    for asset in active_assets:
        cfg_path = REPO_ROOT / "coins" / asset.lower() / "config.json"
        cfg = load_json(cfg_path)
        if cfg.get("enabled", False):
            coin_configs[asset] = cfg
        else:
            print(f"[run_live_bot] {asset} is disabled in config, skipping")

    # Mode flag handling
    if args.mode == "live":
        os.environ["KALSHI_LIVE_TRADING"] = "1"
    elif args.mode == "paper":
        os.environ["KALSHI_LIVE_TRADING"] = "0"

    live_armed = os.environ.get("KALSHI_LIVE_TRADING", "0").strip().lower() in ("1", "true", "yes")
    print(f"[run_live_bot] Mode: {'LIVE OMS ARMED' if live_armed else 'PAPER mode (dry_run — no real orders will be sent)'} "
          f"(flag={args.mode or 'default'}, KALSHI_LIVE_TRADING={'1' if live_armed else '0'})")
    print(f"[run_live_bot] Active assets: {list(coin_configs.keys())}")
    print(f"[run_live_bot] Global risk: max_portfolio_allocation={global_risk.get('max_portfolio_allocation_pct')}%, "
          f"max_single_asset={global_risk.get('max_single_asset_allocation_pct')}%, "
          f"max_concurrent={global_risk.get('max_concurrent_positions')}")

    # ---- Config validation (AGENTS.md Section 5) ----
    max_portfolio = float(global_risk.get("max_portfolio_allocation_pct", 0))
    max_single = float(global_risk.get("max_single_asset_allocation_pct", 0))
    min_entry = float(global_risk.get("min_seconds_left_for_entry", 0))
    max_entry = float(global_risk.get("max_seconds_left_for_entry", 0))
    active_assets = global_risk.get("active_assets", [])

    allocation_sum = 0.0
    for asset in active_assets:
        cfg_path = REPO_ROOT / "coins" / asset.lower() / "config.json"
        if not cfg_path.exists():
            raise SystemExit(f"[run_live_bot] active_assets references '{asset}' but coins/{asset.lower()}/config.json is missing")
        cfg = load_json(cfg_path, {})
        if not cfg.get("enabled", False):
            print(f"[run_live_bot] NOTE: {asset} in active_assets but enabled=false in config.json; skipping")
            continue
        allocation_sum += float(cfg.get("max_allocation_pct", 0))
        asset_min_entry = float(cfg.get("min_seconds_left_for_entry", min_entry))
        if asset_min_entry < min_entry:
            print(f"[run_live_bot] WARNING: {asset} min_seconds_left_for_entry ({asset_min_entry}s) < global ({min_entry}s); using global")

    if allocation_sum > max_portfolio:
        raise SystemExit(f"[run_live_bot] CONFIG ERROR: Sum of active coin max_allocation_pct ({allocation_sum}%) exceeds global max_portfolio_allocation_pct ({max_portfolio}%)")

    for asset in active_assets:
        cfg_path = REPO_ROOT / "coins" / asset.lower() / "config.json"
        cfg = load_json(cfg_path, {})
        asset_alloc = float(cfg.get("max_allocation_pct", 0))
        if asset_alloc > max_single:
            raise SystemExit(f"[run_live_bot] CONFIG ERROR: {asset} max_allocation_pct ({asset_alloc}%) exceeds global max_single_asset_allocation_pct ({max_single}%)")

    if min_entry >= max_entry:
        raise SystemExit(f"[run_live_bot] CONFIG ERROR: min_seconds_left_for_entry ({min_entry}s) >= max_seconds_left_for_entry ({max_entry}s)")

    print(f"[run_live_bot] Config validation OK (allocation_sum={allocation_sum}% / {max_portfolio}%, single_asset_cap={max_single}%)")

    if args.serve:
        import uvicorn
        uvicorn.run("app:app", host="0.0.0.0", port=8088, log_level="info")
        return

    import app  # builds all engines; OMS dry_run is env-gated inside core/live_executor.py
    from core.profiler import HFTProfiler, GCTuner

    profiler = HFTProfiler(output_dir=args.profile_output)
    gc_tuner = GCTuner()
    gc_tuner.configure_for_hft()

    if args.profile:
        profiler.start_cpu_profiling()
    if args.profile_mem:
        profiler.start_memory_profiling()

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

        main_task = asyncio.current_task()
        shutdown_requested = False

        def handle_exit(sig=None, frame=None):
            nonlocal shutdown_requested
            if not shutdown_requested:
                shutdown_requested = True
                print("[run_live_bot] Shutdown signal — cancelling loops via TaskGroup "
                      "(dry_run: no real exchange cancels are sent).")
                if main_task and not main_task.done():
                    main_task.cancel()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, handle_exit)
            except (NotImplementedError, RuntimeError):
                signal.signal(sig, handle_exit)

        async def profile_watchdog(duration_sec: float):
            """Automatically triggers graceful shutdown after duration expires for profiling."""
            if duration_sec <= 0:
                return
            await asyncio.sleep(duration_sec)
            print(f"[run_live_bot] Profiling duration of {duration_sec}s reached — initiating clean teardown...")
            handle_exit()

        async def memory_checkpoint_loop():
            """Periodic memory checkpoint logger while memory profiling is active."""
            while not shutdown_requested:
                await asyncio.sleep(5.0)
                profiler.record_memory_checkpoint("5s_tick_cycle")

        try:
            async with asyncio.TaskGroup() as tg:
                if args.profile_duration > 0:
                    tg.create_task(profile_watchdog(args.profile_duration))
                if args.profile_mem:
                    tg.create_task(memory_checkpoint_loop())
                tg.create_task(app.live_ws.connect_and_listen())
                tg.create_task(app.fast_spot_loop())
                tg.create_task(app.full_scan_loop())
                tg.create_task(app.technicals_loop())
                tg.create_task(app.multi_coin_production_loop())
                tg.create_task(app.periodic_reconciliation_loop())
                tg.create_task(app.paper_reconcile_loop())
                tg.create_task(app.kill_switch_watcher())
        except (asyncio.CancelledError, ExceptionGroup) as eg:
            if isinstance(eg, asyncio.CancelledError) or any(isinstance(e, asyncio.CancelledError) for e in getattr(eg, "exceptions", [])):
                print("[run_live_bot] All engine loops successfully cancelled and completed teardown.")
            else:
                print(f"[run_live_bot] TaskGroup exited with exception: {eg}")
        finally:
            print("[run_live_bot] Teardown complete. Closing WebSocket and connections...")
            try:
                app.live_ws.stop()
            except Exception as e:
                print(f"[run_live_bot] Notice during WS stop: {e}")

            # Dump profiling results
            if args.profile or args.profile_mem:
                print("\n" + "=" * 80)
                print("GENERATING PROFILING ARTIFACTS...")
                print("=" * 80)
                if args.profile:
                    cpu_rep = profiler.stop_cpu_profiling()
                    print(cpu_rep[:1200] + "\n... (truncated, full report in "
                          f"{args.profile_output}/profile_cpu.txt and profile_cpu.prof)")
                if args.profile_mem:
                    mem_rep = profiler.stop_memory_profiling()
                    print(mem_rep)
            gc_tuner.restore_defaults()

    asyncio.run(runner())


if __name__ == "__main__":
    main()
