"""A minimal pub/sub hub for WebSocket clients -- every connected client
receives every settlement-period update; the browser side decides whether
it's currently showing that period. Simple enough for a single-process
deployment; a multi-process one would need this backed by e.g. Postgres
LISTEN/NOTIFY or Redis instead, not needed yet.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date

from fastapi import WebSocket

from ..engine.view import split_and_sort
from ..settlement import current_period

logger = logging.getLogger("citadel.api.broadcast")


class Broadcaster:
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

    async def publish(self, settlement_date: date, settlement_period: int, records: list[dict]) -> None:
        view = split_and_sort(records)
        # The runner recomputes and broadcasts its whole rolling window,
        # which includes settlement periods *after* the real current one
        # (advance-notified FPNs/BOD already exist for them) -- clients
        # need this flag to tell "the period actually delivering right
        # now" apart from "a future period the engine happens to have
        # partial data for", rather than guessing from recency.
        cur = current_period()
        is_current = (settlement_date, settlement_period) == (cur.settlement_date, cur.settlement_period)
        message = json.dumps({
            "type": "stack_update",
            "settlement_date": settlement_date.isoformat(),
            "settlement_period": settlement_period,
            "is_current": is_current,
            **view,
        })
        async with self._lock:
            clients = list(self._clients)
        for ws in clients:
            try:
                await ws.send_text(message)
            except Exception:
                logger.info("dropping disconnected websocket client")
                await self.disconnect(ws)
