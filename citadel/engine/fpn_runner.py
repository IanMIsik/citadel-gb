"""Orchestrates the FPN Analytics pipeline -- same shape as
engine/runner.py's Runner (rolling window, debounced recompute, persist,
broadcast), sharing that Runner's own BOALF/PN/MEL/MIL/DISBSAD buffers by
reference instead of fetching them a second time, and owning a second,
slower REST poll loop for the datasets unique to this dashboard (FUELINST,
demand forecasts, NESO National Grid trades -- all lower-frequency than BM
actions).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import httpx
import pandas as pd

from ..config import Settings
from ..ingest import elexon_rest, neso
from ..settlement import rolling_window, sp_start_utc
from ..storage import db
from . import fpn as fpn_engine

if TYPE_CHECKING:
    from ..api.fpn_broadcast import FpnBroadcaster
    from .runner import WindowBuffers

logger = logging.getLogger("citadel.engine.fpn_runner")

DEBOUNCE_SECONDS = 2.0
GENERATION_BY_FUEL_RETENTION = timedelta(hours=6)


class FpnBuffers:
    """Raw records for the datasets unique to this dashboard -- always
    wholesale-`replace()`d on each REST cycle (these datasets don't have
    an IRIS push feed, so there's no incremental `add()` path to support).
    """

    def __init__(self) -> None:
        self.fuelinst: list[dict] = []
        self.ndf: list[dict] = []
        self.tsdf: list[dict] = []
        self.indo: list[dict] = []
        self.itsdo: list[dict] = []
        self.da_ndf: list[dict] = []
        self.neso_trades: list[dict] = []


def _rows_for(df: pd.DataFrame, rename: dict[str, str]) -> list[dict]:
    if df.empty:
        return []
    renamed = df.rename(columns=rename)
    renamed = renamed.where(pd.notnull(renamed), None)
    rows = renamed.to_dict("records")
    for row in rows:
        sd = row.get("settlement_date")
        if hasattr(sd, "date"):
            row["settlement_date"] = sd.date()
    return rows


class FpnRunner:
    def __init__(self, settings: Settings, pool, broadcaster: FpnBroadcaster, shared_buffers: WindowBuffers, process_pool) -> None:
        self.settings = settings
        self.pool = pool
        self.broadcaster = broadcaster
        self.shared_buffers = shared_buffers
        self.process_pool = process_pool
        self.fpn_buffers = FpnBuffers()
        self.bm_unit_reference = pd.DataFrame()
        self._demand_window: tuple[pd.Timestamp, pd.Timestamp] | None = None
        self._dirty = asyncio.Event()
        self._stop = asyncio.Event()
        self._http = httpx.AsyncClient()

    async def _refresh_bm_unit_reference(self) -> None:
        rows = await db.bm_unit_reference_all(self.pool)
        self.bm_unit_reference = pd.DataFrame([dict(r) for r in rows])

    async def _rest_refresh_once(self) -> None:
        periods = rolling_window()
        now = datetime.now(timezone.utc)
        demand_from = (sp_start_utc(*periods[0]) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%MZ")
        demand_to = (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%MZ")
        today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        da_from = (today_midnight - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%MZ")
        da_to = today_midnight.strftime("%Y-%m-%dT%H:%MZ")

        bundle = await elexon_rest.fetch_fpn_window(self._http, periods, (demand_from, demand_to), (da_from, da_to))

        neso_from = today_midnight.strftime("%Y-%m-%dT%H:%M:%S")
        neso_to = (today_midnight + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")
        try:
            trades = await neso.fetch_national_grid_trades(self._http, neso_from, neso_to)
        except Exception:
            logger.warning("NESO national grid trades fetch failed", exc_info=True)
            trades = []

        self.fpn_buffers.fuelinst = bundle.fuelinst
        self.fpn_buffers.ndf = bundle.ndf
        self.fpn_buffers.tsdf = bundle.tsdf
        self.fpn_buffers.indo = bundle.indo
        self.fpn_buffers.itsdo = bundle.itsdo
        self.fpn_buffers.da_ndf = bundle.da_ndf
        self.fpn_buffers.neso_trades = trades
        self._demand_window = (sp_start_utc(*periods[0]), pd.Timestamp(now) + timedelta(hours=2))

        await self._refresh_bm_unit_reference()
        await db.log_refresh(self.pool, "rest", "fpn_window", True, note=f"{len(periods)} periods")
        self._dirty.set()

    async def _rest_poll_loop(self) -> None:
        # FUELINST/demand-forecast/NESO data moves on a slower cadence than
        # BM actions, so this polls at a fraction of the pricing stack's
        # own rate rather than matching it minute-for-minute.
        interval = self.settings.rest_poll_interval_seconds * 4
        while not self._stop.is_set():
            try:
                await self._rest_refresh_once()
            except Exception:
                logger.exception("FPN REST refresh cycle failed, will retry next interval")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _recompute_loop(self) -> None:
        while not self._stop.is_set():
            await self._dirty.wait()
            await asyncio.sleep(DEBOUNCE_SECONDS)
            self._dirty.clear()
            try:
                await self._recompute_and_persist()
            except Exception:
                logger.exception("FPN recompute cycle failed")

    async def _recompute_and_persist(self) -> None:
        pn = self.shared_buffers.frame("pn")
        if pn.empty or self.bm_unit_reference.empty or self._demand_window is None:
            return
        boalf = self.shared_buffers.frame("boalf")
        mel = self.shared_buffers.frame("mel")
        mil = self.shared_buffers.frame("mil")
        disbsad = self.shared_buffers.frame("disbsad")

        loop = asyncio.get_running_loop()
        # Runs in its own OS process (see api/app.py's shared
        # ProcessPoolExecutor) so this pandas-heavy recompute -- like the
        # pricing stack's own -- never blocks the event loop that's also
        # serving WebSocket broadcasts.
        results = await loop.run_in_executor(
            self.process_pool, fpn_engine.compute,
            pn, boalf, mel, mil, disbsad,
            self.fpn_buffers.fuelinst, self.fpn_buffers.ndf, self.fpn_buffers.tsdf,
            self.fpn_buffers.indo, self.fpn_buffers.itsdo, self.fpn_buffers.da_ndf,
            self.fpn_buffers.neso_trades, self.bm_unit_reference, self._demand_window,
        )
        if not results:
            return
        await self._persist(results)
        await self.broadcaster.publish({"type": "fpn_update"})

    async def _persist(self, results: dict[str, pd.DataFrame]) -> None:
        tables = (
            ("fpn_by_fuel", db.FPN_BY_FUEL_COLUMNS, _rows_for(
                results.get("by_fuel", pd.DataFrame()),
                {"settlementDate": "settlement_date", "settlementPeriod": "settlement_period", "FT": "fuel_type"},
            )),
            ("fpn_worst_deviants", db.FPN_WORST_DEVIANTS_COLUMNS, _rows_for(
                results.get("worst_deviants", pd.DataFrame()),
                {"bmUnit": "bm_unit", "settlementDate": "settlement_date", "settlementPeriod": "settlement_period", "FT": "fuel_type"},
            )),
            ("fpn_aggregated", db.FPN_AGGREGATED_COLUMNS, _rows_for(
                results.get("aggregated", pd.DataFrame()),
                {"settlementDate": "settlement_date", "settlementPeriod": "settlement_period"},
            )),
        )
        for table, columns, rows in tables:
            by_period: dict[tuple, list[dict]] = {}
            for row in rows:
                by_period.setdefault((row["settlement_date"], row["settlement_period"]), []).append(row)
            for (sd, sp), period_rows in by_period.items():
                await db.replace_fpn_period_rows(self.pool, table, (), sd, int(sp), period_rows, columns)

        generation_rows = _rows_for(results.get("generation_by_fuel", pd.DataFrame()), {"TS": "ts"})
        if generation_rows:
            await db.upsert_fpn_generation_by_fuel(self.pool, generation_rows)
            await db.prune_fpn_generation_by_fuel(self.pool, datetime.now(timezone.utc) - GENERATION_BY_FUEL_RETENTION)

    async def run(self) -> None:
        await asyncio.gather(self._rest_poll_loop(), self._recompute_loop())

    async def stop(self) -> None:
        self._stop.set()
        await self._http.aclose()
