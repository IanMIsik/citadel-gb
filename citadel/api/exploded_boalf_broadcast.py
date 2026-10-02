"""Pub/sub hub for the "All Plants Exploded BOALF" WebSocket -- identical
shape to api/fundies_broadcast.py's FundiesBroadcaster, a fourth instance
rather than a shared one so a slow client here can never back up the
stack/FPN/Fundies sockets or vice versa.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import WebSocket

logger = logging.getLogger("citadel.api.exploded_boalf_broadcast")


class ExplodedBoalfBroadcaster:
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
