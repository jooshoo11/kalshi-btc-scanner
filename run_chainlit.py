#!/usr/bin/env python3
"""
run_chainlit.py — Direct ASGI launcher for Chainlit 4-Coin Brain Dashboard.
Bypasses nest_asyncio on Python 3.14 to ensure flawless anyio/uvicorn static file and WebSocket serving.
"""

import os
import sys
from chainlit.config import load_module, init_config
from chainlit.server import app
import uvicorn

if __name__ == "__main__":
    app_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(app_dir)
    sys.path.insert(0, app_dir)

    init_config()
    target_module = os.path.join(app_dir, "chainlit_agent.py")
    load_module(target_module)

    print("=" * 80)
    print("   STARTING CHAINLIT 4-COIN BRAIN & AGENT DASHBOARD (Port 8089)")
    print("   Access via: http://localhost:8089 or http://10.0.0.50:8089")
    print("=" * 80)

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8089,
        log_level="info",
        ws="websockets",
        ws_per_message_deflate=True
    )
