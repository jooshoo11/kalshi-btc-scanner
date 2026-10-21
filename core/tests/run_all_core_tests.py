#!/usr/bin/env python3
"""
core/tests/run_all_core_tests.py — Master Test Runner for Core Multi-Asset Framework.
"""

import os
import sys
import subprocess

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_MODULES = [
    "core/tests/test_portfolio_concurrency.py",
    "core/tests/test_brti_accumulator.py",
    "core/tests/test_live_oms.py",
    "core/tests/test_kalshi_client.py",
]

def main():
    print("=" * 80)
    print("       RUNNING FULL MULTI-ASSET CORE TEST SUITE")
    print("=" * 80)

    failed = []
    for test in TEST_MODULES:
        full_path = os.path.join(PROJECT_ROOT, test)
        print(f"\n[+] Executing: {test}")
        cmd = [sys.executable, full_path]
        res = subprocess.run(cmd, env={"PYTHONPATH": PROJECT_ROOT})
        if res.returncode != 0:
            failed.append(test)

    print("\n" + "=" * 80)
    if failed:
        print(f"FAILED TESTS ({len(failed)}): {failed}")
        sys.exit(1)
    else:
        print("ALL MULTI-ASSET CORE TESTS PASSED SUCCESSFULLY! (4/4 Suites 100% Green)")
        print("=" * 80)

if __name__ == "__main__":
    main()
