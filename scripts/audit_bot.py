#!/usr/bin/env python3
"""
audit_bot.py — Scheduled audit for Kalshi multi-coin trading system.

Runs every 60 minutes via cron. Checks:
  1. Pytest suite (all tests pass)
  2. Import integrity (core.* and coins.<asset>.*)
  3. Coin states (SOL/XRP disabled, BTC/ETH enabled)
  4. AGENTS.md phase status
  5. Database state (trade counts, rollback events)
  6. Running processes (bot alive?)
  7. .env absence (security)
  8. Coin logs (populated?)

Exits non-zero if any check fails — suitable for cron alerting.
"""

import sys
import subprocess
import json
import os
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

FAILURES = []
WARNINGS = []


def check(name, condition, detail=""):
    if condition:
        print(f"  [PASS] {name}")
    else:
        FAILURES.append(f"{name}: {detail}")
        print(f"  [FAIL] {name}: {detail}")


def warn(name, detail=""):
    WARNINGS.append(f"{name}: {detail}")
    print(f"  [WARN] {name}: {detail}")


def run_pytest():
    print("\n=== 1. Pytest Suite ===")
    result = subprocess.run(
        ["python3", "-m", "pytest", "core/tests/", "-q"],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=120
    )
    # Parse "N passed" from output
    import re
    match = re.search(r"(\d+) passed", result.stdout)
    passed = int(match.group(1)) if match else 0
    failed = result.returncode != 0
    check("All pytest tests pass", not failed, f"{passed} tests, return code {result.returncode}")


def run_imports():
    print("\n=== 2. Import Integrity ===")
    tests = [
        ("coins.btc.predictors", "BTCPredictor"),
        ("coins.eth.predictors", "ETHPredictor"),
        ("coins.sol.predictors", "SOLPredictor"),
        ("coins.xrp.predictors", "XRPPredictor"),
        ("core.rti_accumulator", "RtiAccumulator"),
        ("core.portfolio_manager", "PortfolioManager"),
        ("core.kalshi_client", "KalshiClient"),
        ("core.live_executor", "KalshiOrderManagementSystem"),
    ]
    for mod_name, class_name in tests:
        try:
            mod = __import__(mod_name, fromlist=[class_name])
            getattr(mod, class_name)
            print(f"  [PASS] {mod_name}.{class_name}")
        except Exception as e:
            check(f"Import {mod_name}.{class_name}", False, str(e))


def run_coin_states():
    print("\n=== 3. Coin States ===")
    for coin in ["btc", "eth", "sol", "xrp"]:
        config_path = REPO_ROOT / "coins" / coin / "config.json"
        if config_path.exists():
            cfg = json.load(open(config_path))
            enabled = cfg.get("enabled", False)
            if coin in ("btc", "eth"):
                check(f"{coin.upper()} enabled", enabled, f"enabled={enabled}")
            else:
                check(f"{coin.upper()} disabled", not enabled, f"enabled={enabled} (should be False)")
        else:
            check(f"{coin.upper()} config exists", False, "config.json missing")

    global_risk = json.load(open(REPO_ROOT / "global_risk.json"))
    active = global_risk.get("active_assets", [])
    check("active_assets = [BTC, ETH]", active == ["BTC", "ETH"], f"got {active}")
    check("max_hold_seconds = 120", global_risk.get("max_hold_seconds") == 120, f"got {global_risk.get('max_hold_seconds')}")


def run_agents_md():
    print("\n=== 4. AGENTS.md Phase Status ===")
    agents_md = REPO_ROOT / "AGENTS.md"
    if agents_md.exists():
        content = agents_md.read_text()
        check("Phase 1 marked [DONE]", any("Phase 1:" in line and "— [DONE]" in line for line in content.splitlines()))
        check("Phase 2 marked [DONE]", any("Phase 2:" in line and "— [DONE]" in line for line in content.splitlines()))
        check("Phase 3 marked [DONE]", any("Phase 3:" in line and "— [DONE]" in line for line in content.splitlines()))
        check("Phase 4 marked [COMPLETE]", any("Phase 4:" in line and "— [COMPLETE]" in line for line in content.splitlines()))
        check("Section 5 present", "## 5. Configuration Reference" in content)
        check("Section 6 present", "## 6. Extended Core Modules" in content)
        check("Section 7 present", "## 7. Forward Plan" in content)
    else:
        check("AGENTS.md exists", False, "file missing")


def run_database():
    print("\n=== 5. Database State ===")
    for db_name in ["multi_coin_paper.db", "kalshi_audit.db"]:
        db_path = REPO_ROOT / db_name
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [r[0] for r in cur.fetchall()]
            print(f"  {db_name}: tables={tables}")
            for t in tables:
                cur.execute(f"SELECT COUNT(*) FROM {t}")
                print(f"    {t}: {cur.fetchone()[0]} rows")
            conn.close()
        else:
            warn(f"{db_name} missing", "file not found")


def run_processes():
    print("\n=== 6. Running Processes ===")
    import psutil
    found_bot = False
    found_agy = False
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline = " ".join(proc.info["cmdline"] or [])
            if "run_live_bot" in cmdline:
                found_bot = True
                print(f"  [RUNNING] run_live_bot.py (PID {proc.info['pid']})")
            if "agy" in cmdline and "hub" in cmdline:
                found_agy = True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    check("run_live_bot.py running", found_bot, "no bot process found")
    check("agy hub processes running", found_agy, "no agy hub processes")


def run_security():
    print("\n=== 7. Security ===")
    env_exists = (REPO_ROOT / ".env").exists()
    check(".env does NOT exist (security)", not env_exists, ".env found — should not be committed")
    env_example = (REPO_ROOT / ".env.example").exists()
    check(".env.example exists", env_example, "template missing")


def run_logs():
    print("\n=== 8. Coin Logs ===")
    for coin in ["btc", "eth", "sol", "xrp"]:
        log_dir = REPO_ROOT / "coins" / coin / "logs"
        if log_dir.exists():
            count = len(list(log_dir.iterdir()))
            if coin in ("btc", "eth"):
                warn(f"{coin.upper()} logs populated", f"{count} files (expected > 0 for active trading)")
            else:
                check(f"{coin.upper()} logs empty (disabled)", count == 0, f"{count} files found")
        else:
            warn(f"{coin.upper()} logs dir missing", "directory not found")


def main():
    print("=" * 60)
    print("  KALSHI BOT AUDIT — Scheduled Run")
    print(f"  Time: {__import__('datetime').datetime.now().isoformat()}")
    print(f"  Repo: {REPO_ROOT}")
    print("=" * 60)

    run_pytest()
    run_imports()
    run_coin_states()
    run_agents_md()
    run_database()
    run_processes()
    run_security()
    run_logs()

    print("\n" + "=" * 60)
    print(f"  RESULT: {len(FAILURES)} failure(s), {len(WARNINGS)} warning(s)")
    print("=" * 60)

    if FAILURES:
        print("\nFAILURES:")
        for f in FAILURES:
            print(f"  - {f}")
    if WARNINGS:
        print("\nWARNINGS:")
        for w in WARNINGS:
            print(f"  - {w}")

    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
