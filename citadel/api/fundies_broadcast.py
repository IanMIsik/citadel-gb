"""Pub/sub hub for the Fundies WebSocket -- identical shape to
api/fpn_broadcast.py's FpnBroadcaster, a third instance rather than a
shared one so a slow Fundies client can never back up the FPN/pricing
stack sockets or vice versa.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import WebSocket

logger = logging.getLogger("citadel.api.fundies_broadcast")


class FundiesBroadcaster:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def publish(self, payload: dict) -> None:
        message = json.dumps(payload, default=str)
        async with self._lock:
            clients = list(self._clients)
        for ws in clients:
            try:
                await ws.send_text(message)
            except Exception:
                logger.info("dropping disconnected websocket client")
                await self.disconnect(ws)
