"""Pub/sub hub for the FPN Analytics WebSocket -- same connect/disconnect
shape as api/broadcast.py's `Broadcaster`, but publishes an already-shaped
payload dict as-is instead of running it through `split_and_sort`, which
is specific to the pricing stack's offer/bid split and has nothing to do
with the FPN dashboard's payload shape.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import WebSocket

logger = logging.getLogger("citadel.api.fpn_broadcast")


class FpnBroadcaster:
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
