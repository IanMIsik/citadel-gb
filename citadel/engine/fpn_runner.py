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
from concurrent.futures import ProcessPoolExecutor
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import httpx
import pandas as pd

from ..config import Settings
from ..ingest import elexon_rest, neso, remit
from ..settlement import current_period, rolling_window, sp_start_utc
from ..storage import db
from . import fpn as fpn_engine

if TYPE_CHECKING:
    from ..api.fpn_broadcast import FpnBroadcaster
    from ..api.trip_broadcast import TripBroadcaster
    from .runner import WindowBuffers

logger = logging.getLogger("citadel.engine.fpn_runner")

# compute() outputs that are built from the pricing stack's saved rows; FpnRunner._recompute_tail()
# is what saves them (see _run_full).
_STACK_DEPENDENT_RESULTS = ("by_fuel", "aggregated", "generation_by_fuel")

DEBOUNCE_SECONDS = 0.5  # was 2.0: only needs to absorb a burst of near-simultaneous triggers
GENERATION_BY_FUEL_RETENTION = timedelta(hours=6)
# REMIT revisions/publishes arrive far less often than MEL/MIL, so this polls
# on its own much slower cadence, independent of the FUELINST/demand loop.
REMIT_POLL_INTERVAL_SECONDS = 60
# A trip's matching REMIT eventStartTime is expected close to when it was
# detected -- Elexon's own publish can lag a genuine trip by minutes, but a
# candidate hours away in either direction is almost certainly a different,
# unrelated outage for the same unit.
REMIT_MATCH_WINDOW_HOURS = 6


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def best_remit_match(
    candidates: list[dict], detected_at: datetime, window_hours: float = REMIT_MATCH_WINDOW_HOURS,
    mel_now: float | None = None, drop_mw: float | None = None,
) -> str | None:
    """`candidates` are full REMIT message details (assetId already filtered server-side by the
    caller). Two ways to match, in this order:

    1. A notice whose `eventStartTime` is within `window_hours` of `detected_at` -- the closest
       wins. This is a trip that was detected as it happened. A candidate with no eventStartTime,
       or one outside the window, is never picked this way.
    2. Otherwise, a notice that is ACTIVE at `detected_at` (started before it, not yet ended),
       chosen by how well it explains what the plant is doing. This is a plant that was already
       down when we first saw it, or one whose outage notice has been running for weeks (and
       revised over a hundred times, so its start is nowhere near our detection). A unit can have
       several such notices at once -- a long full outage next to partial derates -- so the choice
       is made on capacity: the notice's available capacity against the plant's own MEL now
       (`mel_now`), or failing that its unavailable capacity against the size of the trip
       (`drop_mw`). A notice that does not fit within a tolerance is rejected: better to show "no
       REMIT match yet" than a stale or unrelated outage for the same unit.
    """
    best_mrid, best_delta = None, None
    for detail in candidates:
        start = _parse_iso(detail.get("eventStartTime")) if detail else None
        if start is None:
            continue
        delta = abs((start - detected_at).total_seconds())
        if delta <= window_hours * 3600 and (best_delta is None or delta < best_delta):
            best_mrid, best_delta = detail["mrid"], delta
    if best_mrid is not None or (mel_now is None and drop_mw is None):
        return best_mrid

    best_key = None
    for detail in candidates:
        if not detail:
            continue
        start, end = _parse_iso(detail.get("eventStartTime")), _parse_iso(detail.get("eventEndTime"))
        if start is None or start > detected_at or (end is not None and end < detected_at):
            continue
        if detail.get("eventStatus") not in (None, "Active"):
            continue
        normal = detail.get("normalCapacity") or 0.0
        if mel_now is not None and detail.get("availableCapacity") is not None:
            err, tolerance = abs(detail["availableCapacity"] - mel_now), max(25.0, 0.1 * normal)
        elif drop_mw is not None and detail.get("unavailableCapacity") is not None:
            err, tolerance = abs(detail["unavailableCapacity"] - drop_mw), max(25.0, 0.15 * normal)
        else:
            continue
        if err > tolerance:
            continue
        key = (err, -(_parse_iso(detail.get("publishTime")) or detected_at).timestamp())   # best fit, then most recently revised
        if best_key is None or key < best_key:
            best_mrid, best_key = detail["mrid"], key
    return best_mrid


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
    def __init__(
        self, settings: Settings, pool, broadcaster: FpnBroadcaster, shared_buffers: WindowBuffers, process_pool,
        trip_broadcaster: TripBroadcaster | None = None,
    ) -> None:
        self.settings = settings
        self.pool = pool
        self.broadcaster = broadcaster
        self.trip_broadcaster = trip_broadcaster
        self.shared_buffers = shared_buffers
        self.process_pool = process_pool
        # compute() keeps an exploded-input cache in module state (see
        # engine/fpn.py's _EXPLODE_CACHE), which only helps if every call lands in
        # the SAME process -- the shared pool hands calls to whichever worker is
        # free -- so the FPN compute gets one dedicated long-lived worker.
        self.compute_pool = ProcessPoolExecutor(max_workers=1) if process_pool is not None else None
        # State the last full compute left for fpn.compute_tail(), and whether the next
        # recompute has to be a full one (set by every REST refresh).
        self._tail_state: dict | None = None
        self._full_dirty = True
        self._stack_dirty = False
        self._full_task: asyncio.Task | None = None
        self._tail_lock = asyncio.Lock()
        self.fpn_buffers = FpnBuffers()
        self.bm_unit_reference = pd.DataFrame()
        self._demand_window: tuple[pd.Timestamp, pd.Timestamp] | None = None
        self._dirty = asyncio.Event()
        self._stop = asyncio.Event()
        self._http = httpx.AsyncClient()
        # The trip edge-detector's own memory (last-seen MIL/MEL drop per
        # bmUnit) -- lives here, not in the DB, since it's purely about
        # deciding whether the *next* cycle's value is a fresh crossing.
        self._trip_state: dict[str, float] = {}
        # BM Stack (engine/bm_stack.py) -- in-memory only, same by-design
        # choice as Runner.latest_exploded_boalf: it's a rolling snapshot
        # re-derived every cycle, read by GET /api/bm-stack.
        # First cycle after a start only *records* which units are already
        # tripped (no alerts), so a restart doesn't re-announce old trips.
        self._trip_seeded = False
        # Worst-behaviour graph data (engine/fpn.py's worst_behaviour_series).
        self.latest_wb_series: list[dict] = []
        self.wb_computed_at: datetime | None = None
        self.latest_bm_stack: list[dict] = []
        self.bm_stack_computed_at: datetime | None = None

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
            trades = await neso.national_grid_trades_cached(self.pool, self._http, neso_from, neso_to, store=False)
        except Exception:
            logger.warning("NESO national grid trades fetch failed, keeping the last fetched trades", exc_info=True)
            trades = self.fpn_buffers.neso_trades

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
        self._full_dirty = True
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

    async def _read_stack_rows(self) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
        """The pricing stack's saved rows for the rolling window (what the decision table is built from)."""
        cur = current_period()
        periods = [sp for sd, sp in rolling_window() if sd == cur.settlement_date]
        return (
            [dict(r) for r in await db.pricing_stack_delta_by_bm_unit(self.pool, cur.settlement_date, periods)],
            [dict(r) for r in await db.pricing_stack_niv_by_period(self.pool, cur.settlement_date, periods)],
            [dict(r) for r in await db.pricing_stack_niv_spot_time_by_period(self.pool, cur.settlement_date, periods)],
            [dict(r) for r in await db.pricing_stack_unit_delta_5min_by_period(self.pool, cur.settlement_date, periods)],
        )

    async def _recompute_tail(self) -> None:
        """Fast path for a pricing-stack update: redo only the stack-dependent end of the
        pipeline (fpn.compute_tail) from the last full compute's saved state, in a thread
        so it never waits behind a full compute in the worker process."""
        # One tail at a time: two overlapping ones (the loop's and a full recompute's closing
        # one) would delete+insert the same period rows against each other.
        async with self._tail_lock:
            t0 = time.perf_counter()
            rows = await self._read_stack_rows()
            t1 = time.perf_counter()
            results = await asyncio.get_running_loop().run_in_executor(None, fpn_engine.compute_tail, self._tail_state, *rows)
            t2 = time.perf_counter()
            await self._persist(results)
            t3 = time.perf_counter()
            await self.broadcaster.publish({"type": "fpn_update", "kind": "tail"})
            logger.info("FPN tail stages: db reads %.1fs, compute %.1fs, persist %.1fs", t1 - t0, t2 - t1, t3 - t2)

    def on_stack_persisted(self) -> None:
        """Called by the pricing-stack Runner the moment a cycle has saved its
        rows. The decision table is built from those rows, so recompute now
        rather than on the next REST timer -- that is what keeps the two in
        step as actions are accepted. A burst coalesces into one recompute."""
        self._stack_dirty = True
        self._dirty.set()

    async def _recompute_loop(self) -> None:
        while not self._stop.is_set():
            await self._dirty.wait()
            await asyncio.sleep(DEBOUNCE_SECONDS)
            self._dirty.clear()
            # A REST refresh (new demand/FUELINST/PN data) needs the full per-unit recompute
            # (8-13 s). It runs in the background so it never holds up the fast path: a
            # pricing-stack save only needs fpn.compute_tail, which reuses the last full
            # compute's state and takes 1-2 s.
            if (self._full_dirty or self._tail_state is None) and (self._full_task is None or self._full_task.done()):
                self._full_dirty = False
                self._full_task = asyncio.create_task(self._run_full())
            if self._stack_dirty and self._tail_state is not None:
                self._stack_dirty = False
                started = time.perf_counter()
                try:
                    await self._recompute_tail()
                    logger.info("FPN tail recompute took %.1fs", time.perf_counter() - started)
                except Exception:
                    logger.exception("FPN tail recompute failed")

    async def _run_full(self) -> None:
        started = time.perf_counter()
        try:
            await self._recompute_and_persist()
            if self._tail_state is not None:
                # The full compute's own stack-dependent output was computed from rows read
                # when it started, so it is not saved; one fresh tail does that instead, and
                # the saved table can never fall back to older stack values.
                await self._recompute_tail()
            logger.info("FPN full recompute took %.1fs", time.perf_counter() - started)
        except Exception:
            logger.exception("FPN full recompute failed")
        finally:
            if self._full_dirty or self._stack_dirty:
                self._dirty.set()

    async def _recompute_and_persist(self) -> None:
        t0 = time.perf_counter()
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
        (pricing_stack_delta_rows, pricing_stack_niv_rows, pricing_stack_niv_spot_time_rows,
         pricing_stack_unit_delta_5min_rows) = await self._read_stack_rows()

        t1 = time.perf_counter()
        loop = asyncio.get_running_loop()
        # Runs in its own OS process (see api/app.py's shared
        # ProcessPoolExecutor) so this pandas-heavy recompute -- like the
        # pricing stack's own -- never blocks the event loop that's also
        # serving WebSocket broadcasts.
        results = await loop.run_in_executor(
            self.compute_pool or self.process_pool, fpn_engine.compute,
            pn, boalf, mel, mil, disbsad,
            self.fpn_buffers.fuelinst, self.fpn_buffers.ndf, self.fpn_buffers.tsdf,
            self.fpn_buffers.indo, self.fpn_buffers.itsdo, self.fpn_buffers.da_ndf,
            self.fpn_buffers.neso_trades, self.bm_unit_reference, self._demand_window,
            pricing_stack_delta_rows, None, pricing_stack_niv_rows, pricing_stack_niv_spot_time_rows,
            pricing_stack_unit_delta_5min_rows, self.settings.fpn_other_fallback_enabled,
            self._trip_state,
            # Only ship the BOD frame across to the worker when the BM Stack
            # is actually on -- no pickling cost where it's disabled.
            self.shared_buffers.frame("bod") if self.settings.bm_stack_enabled else None,
            self.settings.bm_stack_enabled,
            not self._trip_seeded,
        )
        t2 = time.perf_counter()
        if not results:
            return
        self._tail_state = results.get("tail_state", self._tail_state)
        await self._persist({k: v for k, v in results.items() if k not in _STACK_DEPENDENT_RESULTS})
        t3 = time.perf_counter()
        logger.info("FPN stages: db reads %.1fs, compute %.1fs, persist %.1fs", t1 - t0, t2 - t1, t3 - t2)
        logger.info("FPN compute steps: %s", results.get("timings"))
        self._trip_state = results.get("trip_state", self._trip_state)
        bm_stack = results.get("bm_stack")
        if self.settings.bm_stack_enabled and bm_stack is not None:
            self.latest_bm_stack = bm_stack.to_dict("records")
            self.bm_stack_computed_at = datetime.now(timezone.utc)
        wb = results.get("wb_series")
        if wb is not None:
            self.latest_wb_series = wb.to_dict("records")
            self.wb_computed_at = datetime.now(timezone.utc)
            try:
                await db.upsert_trip_telemetry(self.pool, self.latest_wb_series)
            except Exception:
                logger.warning("saving trip telemetry failed", exc_info=True)
        await self._handle_trips(results.get("trips") or [], results.get("trip_recoveries") or [])
        await self._adopt_ongoing_trips()
        self._trip_seeded = True
        await self.broadcaster.publish({"type": "fpn_update", "kind": "full"})

    async def _adopt_ongoing_trips(self) -> None:
        """Keep the trip table honest about what is ongoing. A trip is ongoing only while the
        plant is actually still tripped (detect_trips() state), so:
          * a plant already tripped when this process started is only *seeded* (no event, so no
            pop-up on every restart) -- give it a quiet row if it has no open trip;
          * a plant with several open rows keeps the newest; the rest are closed;
          * an open row for a plant that is no longer tripped, more than 12h old, is a leftover
            (e.g. from the old repeated-alert bug) and is closed.
        The stale-row clean-up waits for the second cycle so a half-loaded first cycle cannot
        close a real trip."""
        tripped = {u: s for u, s in (self._trip_state or {}).items() if s.get("peak") is not None}
        open_rows = await db.fetch_open_trips(self.pool)  # newest first
        now = datetime.now(timezone.utc)
        newest: dict[str, dict] = {}
        to_close: list[int] = []
        for t in open_rows:
            if t["bm_unit"] in tripped and t["bm_unit"] not in newest:
                newest[t["bm_unit"]] = t
            elif t["bm_unit"] in tripped:
                to_close.append(t["id"])                       # an older duplicate of a still-open trip
            elif self._trip_seeded and t["detected_at"] < now - timedelta(hours=12):
                to_close.append(t["id"])                       # the plant is no longer tripped
        await db.close_trip_rows(self.pool, to_close)

        fuel = {r["bm_unit"]: r.get("fuel_type") for r in self.latest_wb_series}
        cur = current_period(now)
        adopted = [
            fpn_engine.TripEvent(u, fuel.get(u), float(s.get("drop") or s["peak"]), cur.settlement_date, cur.settlement_period, now)
            for u, s in tripped.items() if u not in newest
        ]
        if adopted:
            await db.insert_trip_events(self.pool, adopted)
            logger.info("adopted %d plants that were already tripped: %s", len(adopted), [t.bm_unit for t in adopted])
        if to_close:
            logger.info("closed %d leftover open trip rows", len(to_close))

    async def _handle_trips(self, trips: list, recoveries: list) -> None:
        # A unit that already has an open trip (recent enough to still be
        # real) never gets a second row or pop-up for it -- belt and braces
        # on top of detect_trips()'s own once-per-trip state.
        open_cutoff = datetime.now(timezone.utc) - timedelta(hours=12)
        open_trips = {t["bm_unit"]: t for t in await db.fetch_open_trips(self.pool) if t["detected_at"] >= open_cutoff}
        trips = [t for t in trips if t.bm_unit not in open_trips]
        if trips:
            await db.insert_trip_events(self.pool, trips)
        if not self.trip_broadcaster:
            return
        for t in trips:
            await self.trip_broadcaster.publish({
                "type": "trip",
                "bm_unit": t.bm_unit,
                "fuel_type": t.fuel_type,
                "drop_mw": t.drop_mw,
                "settlement_date": t.settlement_date,
                "settlement_period": t.settlement_period,
                "detected_at": t.detected_at,
            })
        for r in recoveries:
            known = open_trips.get(r.bm_unit)
            expected_back = None
            if known and known["remit_mrid"]:
                revs = await db.fetch_remit_revisions(self.pool, known["remit_mrid"])
                expected_back = revs[-1]["event_end_time"] if revs else None
            if r.kind == "full":
                await db.resolve_open_trips_for_unit(self.pool, r.bm_unit, r.detected_at)
            await self.trip_broadcaster.publish({
                "type": "trip_recovery",
                "kind": r.kind,
                "bm_unit": r.bm_unit,
                "fuel_type": r.fuel_type,
                "peak_mw": r.peak_mw,
                "drop_mw": r.drop_mw,
                "settlement_date": r.settlement_date,
                "settlement_period": r.settlement_period,
                "expected_back": expected_back,
            })

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

    def _remit_window(self, detected_at: datetime, now: datetime) -> tuple[str, str]:
        # REMIT's own `from`/`to` window is capped at 7 days -- start a day
        # before the trip (publish can lag) but never open a window wider
        # than 6 days back from now, so this always stays valid even for a
        # trip that's been open a long time.
        from_dt = max(detected_at - timedelta(days=1), now - timedelta(days=6))
        return from_dt.strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%dT%H:%M:%SZ")

    async def _latest_mel(self, bm_unit: str, now: datetime) -> float | None:
        """The plant's own MEL as of now (from the saved trip telemetry), None if there is none yet."""
        rows = await db.trip_telemetry_between(self.pool, bm_unit, now - timedelta(minutes=30), now)
        known = [r for r in rows if r["mel"] is not None and r["mel"] == r["mel"]]
        return float(known[-1]["mel"]) if known else None

    async def _poll_trip_remit(self, trip, now: datetime) -> None:
        bm_unit = trip["bm_unit"]
        detected_at = trip["detected_at"]
        from_iso, to_iso = self._remit_window(detected_at, now)
        mrid = trip["remit_mrid"]

        if mrid is None:
            candidates_idx = await remit.fetch_remit_by_event(self._http, from_iso, to_iso, latest_revision_only=True, asset_id=bm_unit)
            details = [d for d in [await remit.fetch_remit_message(self._http, ev["id"]) for ev in candidates_idx] if d]
            mrid = best_remit_match(details, detected_at, mel_now=await self._latest_mel(bm_unit, now), drop_mw=trip["drop_mw"])
            if mrid is None:
                return
            await db.set_trip_remit_match(self.pool, trip["id"], mrid)
            if self.trip_broadcaster:
                await self.trip_broadcaster.publish({
                    "type": "remit_match", "trip_id": trip["id"], "bm_unit": bm_unit,
                    "fuel_type": trip["fuel_type"], "drop_mw": trip["drop_mw"], "mrid": mrid,
                })

        all_events = await remit.fetch_remit_by_event(self._http, from_iso, to_iso, latest_revision_only=False, asset_id=bm_unit)
        same_outage = [e for e in all_events if e["mrid"] == mrid]
        if not same_outage:
            return
        prior_revisions = {r["revision_number"] for r in await db.fetch_remit_revisions(self.pool, mrid)}
        new_events = [e for e in same_outage if e["revisionNumber"] not in prior_revisions]
        if not new_events:
            return
        details = [d for d in [await remit.fetch_remit_message(self._http, e["id"]) for e in new_events] if d]
        if not details:
            return
        rows = [{
            "mrid": d["mrid"], "revision_number": d["revisionNumber"], "message_id": d["id"],
            "asset_id": d.get("assetId"), "fuel_type": d.get("fuelType"), "event_status": d.get("eventStatus"),
            "event_start_time": _parse_iso(d.get("eventStartTime")), "event_end_time": _parse_iso(d.get("eventEndTime")),
            "normal_capacity": d.get("normalCapacity"), "available_capacity": d.get("availableCapacity"),
            "unavailable_capacity": d.get("unavailableCapacity"), "publish_time": _parse_iso(d.get("publishTime")),
            "cause": d.get("cause"),
        } for d in details]
        await db.upsert_remit_revisions(self.pool, rows)

        latest = max(details, key=lambda d: d["revisionNumber"])
        if not self.trip_broadcaster:
            return
        if latest.get("eventStatus") == "Dismissed":
            await db.resolve_trip(self.pool, trip["id"], now)
            await self.trip_broadcaster.publish({"type": "resolved", "trip_id": trip["id"], "bm_unit": bm_unit, "mrid": mrid})
        else:
            await self.trip_broadcaster.publish({
                "type": "remit_revision", "trip_id": trip["id"], "bm_unit": bm_unit, "mrid": mrid,
                "event_end_time": latest.get("eventEndTime"), "unavailable_capacity": latest.get("unavailableCapacity"),
            })

    async def _remit_poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                now = datetime.now(timezone.utc)
                for trip in await db.fetch_open_trips(self.pool):
                    try:
                        await self._poll_trip_remit(trip, now)
                    except Exception:
                        logger.exception("REMIT poll failed for trip id=%s", trip["id"])
            except Exception:
                logger.exception("REMIT poll cycle failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=REMIT_POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        await asyncio.gather(self._rest_poll_loop(), self._recompute_loop(), self._remit_poll_loop())

    async def stop(self) -> None:
        self._stop.set()
        await self._http.aclose()
        if self.compute_pool is not None:
            self.compute_pool.shutdown(wait=False, cancel_futures=True)
