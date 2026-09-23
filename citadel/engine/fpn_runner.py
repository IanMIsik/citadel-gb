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
from ..settlement import current_period, rolling_window, sp_start_utc
from ..storage import db
from . import fpn as fpn_engine

if TYPE_CHECKING:
    from ..api.fpn_broadcast import FpnBroadcaster
    from .runner import WindowBuffers

logger = logging.getLogger("citadel.engine.fpn_runner")

DEBOUNCE_SECONDS = 2.0
GENERATION_BY_FUEL_RETENTION = timedelta(hours=6)
# Same reasoning as engine/runner.py's own PERSIST_CONCURRENCY: this module
# persists up to 3 tables x ~8 periods per cycle (up to 24 delete+insert
# transactions), previously one full round-trip at a time. Each (table, sd,
# sp) key is disjoint, so they can run concurrently -- bounded so this
# doesn't compete with the pricing-stack Runner's own concurrent persist for
# every connection in the shared pool at once (storage/db.py's create_pool,
# max_size=10).
PERSIST_CONCURRENCY = 4


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
    # `.where(notnull, None)` on a float64 column is a no-op -- pandas
    # can't hold Python None in a float64 Series, so it silently coerces
    # None right back to NaN. Confirmed live: this meant NaN was being
    # written into Postgres as a literal NaN (not NULL) for any minute a
    # dataset genuinely has no value for yet (e.g. FUELINST hasn't
    # published the current, still-in-progress settlement period's latest
    # 5-minute block) -- and a stored NaN poisons every AVG()/SUM() that
    # touches it, which is why the current settlement period's own
    # average showed up blank even though most of its minutes had real
    # data. `.astype(object)` first lets None actually stick per-cell, so
    # Postgres gets a real NULL and its aggregates correctly skip it.
    renamed = renamed.astype(object).where(pd.notnull(renamed), None)
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

        # The real pricing stack's own already-computed accepted-volume
        # figure (`pricing_stack_rows`, written by the sibling Runner) --
        # `market_gen` needs this, not this module's own delta, see
        # engine/fpn.py's module docstring. Read fresh each cycle, same
        # window as everything else, since Runner's own recompute cadence
        # can differ from this one's.
        cur = current_period()
        same_date_periods = [sp for sd, sp in rolling_window() if sd == cur.settlement_date]
        pricing_stack_delta_rows = [
            dict(r) for r in await db.pricing_stack_delta_by_bm_unit(self.pool, cur.settlement_date, same_date_periods)
        ]
        pricing_stack_niv_rows = [
            dict(r) for r in await db.pricing_stack_niv_by_period(self.pool, cur.settlement_date, same_date_periods)
        ]
        # The pricing stack's own genuinely per-minute NIV trajectory (see
        # engine/stack.py's spot_time_niv()/SPOT_NIV_COLUMNS docstring) --
        # feeds the Delta Chart's `delta` line with real sub-period
        # resolution instead of `pricing_stack_niv_rows`'s own flat
        # once-per-period figure (that one still feeds `delta_av`, the
        # period average -- see engine/fpn.py's build_aggregated()).
        pricing_stack_niv_spot_time_rows = [
            dict(r) for r in await db.pricing_stack_niv_spot_time_by_period(self.pool, cur.settlement_date, same_date_periods)
        ]
        # Same real, reversal/CADL-aware delta as pricing_stack_delta_rows
        # above, but kept per-5-minute-bucket instead of collapsed to one
        # period-total figure -- feeds the Real Time Generation table's `_d`
        # column with an actual per-bucket value instead of the same
        # period-total repeated across every row of a period (see
        # engine/stack.py's spot_time_bm_unit_delta_5min() docstring).
        pricing_stack_unit_delta_5min_rows = [
            dict(r) for r in await db.pricing_stack_unit_delta_5min_by_period(self.pool, cur.settlement_date, same_date_periods)
        ]

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
            pricing_stack_delta_rows, None, pricing_stack_niv_rows, pricing_stack_niv_spot_time_rows,
            pricing_stack_unit_delta_5min_rows,
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
        semaphore = asyncio.Semaphore(PERSIST_CONCURRENCY)

        async def _write(table: str, columns: list[str], sd, sp: int, period_rows: list[dict]) -> None:
            async with semaphore:
                await db.replace_fpn_period_rows(self.pool, table, (), sd, sp, period_rows, columns)

        writes = []
        for table, columns, rows in tables:
            by_period: dict[tuple, list[dict]] = {}
            for row in rows:
                by_period.setdefault((row["settlement_date"], row["settlement_period"]), []).append(row)
            for (sd, sp), period_rows in by_period.items():
                writes.append(_write(table, columns, sd, int(sp), period_rows))
        if writes:
            await asyncio.gather(*writes)

        generation_rows = _rows_for(results.get("generation_by_fuel", pd.DataFrame()), {"TS": "ts"})
        if generation_rows:
            await db.upsert_fpn_generation_by_fuel(self.pool, generation_rows)
            await db.prune_fpn_generation_by_fuel(self.pool, datetime.now(timezone.utc) - GENERATION_BY_FUEL_RETENTION)

    async def run(self) -> None:
        await asyncio.gather(self._rest_poll_loop(), self._recompute_loop())

    async def stop(self) -> None:
        self._stop.set()
        await self._http.aclose()
