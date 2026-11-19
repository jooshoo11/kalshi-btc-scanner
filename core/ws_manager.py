"""
core/ws_manager.py — Real-Time WebSocket Connection Manager for Frontend Streaming.

Manages active WebSocket client connections, ping/pong heartbeats, and non-blocking
broadcasts of multi-coin spot prices, L2 book sweeps, and risk updates.
"""

import asyncio
import logging
from typing import List, Dict, Any
from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger("WebSocketManager")


class WebSocketConnectionManager:
    """
    Manages active client WebSocket connections for real-time frontend dashboard streaming.
    """

    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        """Accept incoming client connection and register into active pool."""
        await websocket.accept()
        async with self._lock:
            self.active_connections.append(websocket)
        logger.info(f"[WS Manager] Client connected. Active clients: {len(self.active_connections)}")

    async def disconnect(self, websocket: WebSocket) -> None:
        """Unregister client upon disconnection."""
        async with self._lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)
        logger.info(f"[WS Manager] Client disconnected. Active clients: {len(self.active_connections)}")

    async def broadcast_json(self, data: Dict[str, Any]) -> None:
        """Broadcast JSON payload to all connected clients; prune disconnected sockets."""
        async with self._lock:
            clients = list(self.active_connections)

        dead_connections = []
        for client in clients:
            try:
                await client.send_json(data)
            except Exception as e:
                logger.debug(f"[WS Manager] Error sending to client: {e}")
                dead_connections.append(client)

        if dead_connections:
            async with self._lock:
                for dead in dead_connections:
                    if dead in self.active_connections:
                        self.active_connections.remove(dead)

    @property
    def connection_count(self) -> int:
        return len(self.active_connections)
