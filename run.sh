#!/usr/bin/env bash
cd "$(dirname "$0")"
echo "Starting Kalshi Quant Scanner on http://localhost:8088 ..."
exec python3 app.py
