#!/usr/bin/env bash
# ==============================================================================
# pre_deploy_check.sh — Automated Pre-Deployment Quality & Safety Pipeline
#
# Pipeline Stages:
#   Stage 1: Lint & Static Syntax Validation (py_compile)
#   Stage 2: Fast Unit Test Suite (pytest)
#   Stage 3: Quantitative Backtest Regression (scripts/backtest_regression.py)
#   Stage 4: End-to-End Paper Trading Smoke Test (scripts/smoke_test.py)
#
# Exit Code: 0 on success, 1 on any stage failure (Fail-Closed)
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

echo -e "${CYAN}====================================================================${NC}"
echo -e "${CYAN}    KALSHI MULTI-COIN QUANT ENGINE — PRE-DEPLOY CI/CD PIPELINE    ${NC}"
echo -e "${CYAN}====================================================================${NC}"

# ------------------------------------------------------------------------------
# STAGE 1: LINT & STATIC SYNTAX CHECK
# ------------------------------------------------------------------------------
echo -e "\n${BLUE}[STAGE 1/4] Running Lint and Syntax Compilation...${NC}"

python3 -m py_compile \
    app.py \
    run_live_bot.py \
    paper_trader.py \
    btc_metrics.py \
    core/*.py \
    scripts/*.py

echo -e "${GREEN}✓ Stage 1 Passed: All Python source files compiled with valid syntax.${NC}"

# ------------------------------------------------------------------------------
# STAGE 2: UNIT TEST SUITE (PYTEST)
# ------------------------------------------------------------------------------
echo -e "\n${BLUE}[STAGE 2/4] Running Fast Unit & Concurrency Test Suite (pytest)...${NC}"

if pytest core/tests/ -q --tb=short; then
    echo -e "${GREEN}✓ Stage 2 Passed: All core unit tests passed.${NC}"
else
    echo -e "${RED}✗ Stage 2 Failed: Pytest suite encountered failures!${NC}"
    exit 1
fi

# ------------------------------------------------------------------------------
# STAGE 3: QUANTITATIVE BACKTEST REGRESSION
# ------------------------------------------------------------------------------
echo -e "\n${BLUE}[STAGE 3/4] Running Quantitative & Pricing Regression Suite...${NC}"

if python3 scripts/backtest_regression.py; then
    echo -e "${GREEN}✓ Stage 3 Passed: Quantitative invariants & pricing models verified.${NC}"
else
    echo -e "${RED}✗ Stage 3 Failed: Backtest regression invariant breach!${NC}"
    exit 1
fi

# ------------------------------------------------------------------------------
# STAGE 4: END-TO-END PAPER TRADE SMOKE TEST
# ------------------------------------------------------------------------------
echo -e "\n${BLUE}[STAGE 4/4] Running End-to-End Paper Trading Smoke Test...${NC}"

if python3 scripts/smoke_test.py; then
    echo -e "${GREEN}✓ Stage 4 Passed: Paper trading smoke test completed without errors.${NC}"
else
    echo -e "${RED}✗ Stage 4 Failed: Smoke test pipeline failure!${NC}"
    exit 1
fi

echo -e "\n${GREEN}====================================================================${NC}"
echo -e "${GREEN}    ✅ ALL PRE-DEPLOY GATES PASSED — SAFE TO PROCEED TO DEPLOY    ${NC}"
echo -e "${GREEN}====================================================================${NC}"
exit 0
