"""FastAPI app: REST snapshot + WebSocket push for the live pricing stack,
plus a minimal static page. Run with `citadel serve` (see cli.py) or
directly: `uvicorn citadel.api.app:app --reload`.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone

import httpx
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..config import settings
from ..engine.fpn_runner import FpnRunner
from ..engine.fundies_runner import FundiesRunner
from ..engine.natgrid import build_ladder
from ..engine.trip_chart import build_trip_chart, mel_evidence
from ..ingest.natgrid_store import reconcile
from ..ingest.neso import gtma_blocks_to_sp_rows
from ..engine.runner import Runner
from ..engine.view import split_and_sort
from ..ingest import gbpw_import, misco_fuel_type_reference
from ..ingest.elexon_rest import fetch_bmu_reference
from ..settlement import current_period, rolling_window, sp_start_utc, utc_to_settlement, window_around
from ..storage import db
from .broadcast import Broadcaster
from .exploded_boalf_broadcast import ExplodedBoalfBroadcaster
from .fpn_broadcast import FpnBroadcaster
from .fundies_broadcast import FundiesBroadcaster
from .trip_broadcast import TripBroadcaster

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("citadel.api.app")

WEB_DIR = __import__("pathlib").Path(__file__).resolve().parents[1] / "web"


def _finite(v):
    """`v`, or None if it is NaN or infinite."""
    return None if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))) else v


def _clean_record(record) -> dict:
    """asyncpg Record -> plain dict, with NaN/Infinity floats mapped to
    None -- the FPN engine's rolling-window math (rolling means, period-
    over-period joins with no earlier period yet) legitimately produces
    NaN for cells with no data yet, but the standard JSONResponse encoder
    rejects it outright ("Out of range float values are not JSON
    compliant"), so this has to happen before a record ever reaches one.
    """
    row = dict(record)
    for k, v in row.items():
        if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
            row[k] = None
    return row


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await db.create_pool(settings.database_url)
    app.state.pool = pool

    async with httpx.AsyncClient() as client:
        try:
            raw = await fetch_bmu_reference(client)
            rows = [
                db.BmUnitReferenceRow(
                    national_grid_bm_unit=r.get("nationalGridBmUnit") or r.get("bmUnit"),
                    elexon_bm_unit=r.get("elexonBmUnit"),
                    lead_party_name=r.get("leadPartyName"),
                    bm_unit_type=r.get("bmUnitType"),
                    # Elexon's own API returns this as a numeric-looking
                    # string (e.g. "15.400"), not a JSON number -- confirmed
                    # live (asyncpg rejected it outright: "must be real
                    # number, not str").
                    generation_capacity_mw=float(r["generationCapacity"]) if r.get("generationCapacity") not in (None, "") else None,
                    fuel_type=r.get("fuelType"),
                )
                for r in raw
                if r.get("nationalGridBmUnit") or r.get("bmUnit")
            ]
            await db.upsert_bm_unit_reference(pool, rows)
            logger.info("loaded %d BM unit reference rows from Elexon's live API", len(rows))
        except Exception:
            logger.exception("bm unit reference load failed -- continuing without it")

        try:
            curated_rows = gbpw_import.read_bm_unit_reference()
            if curated_rows:
                await db.upsert_bm_unit_reference(pool, curated_rows, overwrite_fuel_type=True)
                logger.info(
                    "applied %d curated fuel-type overrides from the gbpw project's own database", len(curated_rows)
                )
            else:
                logger.info("gbpw database not found at %s -- fuel_type stays whatever Elexon's live API returned", gbpw_import.DEFAULT_GBPW_DB_PATH)
        except Exception:
            logger.exception("gbpw fuel-type import failed -- continuing with Elexon's live fuel_type only")

        try:
            misco_fuel_type_rows = misco_fuel_type_reference.read_bmu_fuel_types()
            if misco_fuel_type_rows:
                # The interconnector rows in this file have no real
                # national_grid_bm_unit of their own (see that module's
                # docstring) -- resolve them onto whatever real key Elexon's
                # live fetch/gbpw import already gave that elexon_bm_unit,
                # so this upsert corrects the existing row instead of
                # leaving a dead duplicate beside it (see
                # resolve_national_grid_bm_units()'s own docstring).
                existing_rows = await db.bm_unit_reference_all(pool)
                existing_by_elexon = misco_fuel_type_reference.preferred_national_grid_bm_unit_map(existing_rows)
                misco_fuel_type_rows = misco_fuel_type_reference.resolve_national_grid_bm_units(misco_fuel_type_rows, existing_by_elexon)
                await db.upsert_bm_unit_reference(pool, misco_fuel_type_rows, overwrite_fuel_type=True)
                deleted = await db.delete_stale_bm_unit_reference_duplicates(pool)
                logger.info(
                    "applied %d fuel-type overrides from the user's own misco BM unit references "
                    "(final say -- see ingest/misco_fuel_type_reference.py), removed %d stale duplicate rows",
                    len(misco_fuel_type_rows), deleted,
                )
        except Exception:
            logger.exception("misco BM unit fuel type reference import failed -- fuel_type stays whatever the earlier steps set")

        # Dev-only fuel-type corrections not yet validated enough to apply
        # to prod -- see data/misco_bmu_fuel_type_dev_overrides.csv. Loaded
        # the same way as the main misco file, just gated on
        # environment_label (config.py) so prod's bm_unit_reference (and
        # its FPN Analytics fuel-by-fuel breakdown) is never touched by
        # these until they're promoted into the main file.
        if settings.environment_label != "prod":
            try:
                dev_override_path = misco_fuel_type_reference.DEFAULT_PATH.with_name("misco_bmu_fuel_type_dev_overrides.csv")
                dev_override_rows = misco_fuel_type_reference.read_bmu_fuel_types(dev_override_path)
                if dev_override_rows:
                    existing_rows = await db.bm_unit_reference_all(pool)
                    existing_by_elexon = misco_fuel_type_reference.preferred_national_grid_bm_unit_map(existing_rows)
                    dev_override_rows = misco_fuel_type_reference.resolve_national_grid_bm_units(dev_override_rows, existing_by_elexon)
                    await db.upsert_bm_unit_reference(pool, dev_override_rows, overwrite_fuel_type=True)
                    logger.info("applied %d dev-only fuel-type overrides (environment_label=%s)", len(dev_override_rows), settings.environment_label)
            except Exception:
                logger.exception("dev-only fuel type override import failed -- fuel_type stays whatever the earlier steps set")

    # Shared by both engines' per-cycle recompute (see engine/runner.py and
    # engine/fpn_runner.py) so neither one's pandas-heavy work ever blocks
    # this event loop -- the two recomputes are independent (same raw
    # input, no shared mutable state) and already triggered concurrently,
    # so running them in separate OS processes is genuine parallel CPU
    # work, not a cosmetic addition.
    process_pool = ProcessPoolExecutor(max_workers=settings.process_pool_workers or None)
    app.state.process_pool = process_pool

    broadcaster = Broadcaster()
    app.state.broadcaster = broadcaster
    exploded_boalf_broadcaster = ExplodedBoalfBroadcaster()
    app.state.exploded_boalf_broadcaster = exploded_boalf_broadcaster
    runner = Runner(settings, pool, broadcaster, process_pool=process_pool, exploded_boalf_broadcaster=exploded_boalf_broadcaster)
    app.state.runner = runner
    runner_task = asyncio.create_task(runner.run())

    fpn_broadcaster = FpnBroadcaster()
    app.state.fpn_broadcaster = fpn_broadcaster
    trip_broadcaster = TripBroadcaster()
    app.state.trip_broadcaster = trip_broadcaster
    fpn_runner = FpnRunner(settings, pool, fpn_broadcaster, runner.buffers, process_pool, trip_broadcaster=trip_broadcaster)
    app.state.fpn_runner = fpn_runner
    # The decision table reads the stack's saved rows, so it recomputes the moment they land.
    runner.add_stack_persisted_listener(fpn_runner.on_stack_persisted)
    fpn_runner_task = asyncio.create_task(fpn_runner.run())

    fundies_broadcaster = FundiesBroadcaster()
    app.state.fundies_broadcaster = fundies_broadcaster
    fundies_runner = FundiesRunner(settings, pool, fundies_broadcaster, process_pool)
    app.state.fundies_runner = fundies_runner
    fundies_runner_task = asyncio.create_task(fundies_runner.run())

    yield

    await runner.stop()
    runner_task.cancel()
    await fpn_runner.stop()
    fpn_runner_task.cancel()
    await fundies_runner.stop()
    fundies_runner_task.cancel()
    process_pool.shutdown(wait=False, cancel_futures=True)
    await pool.close()


app = FastAPI(title="Citadel Pricing Stack", lifespan=lifespan)


@app.get("/api/health")
async def health():
    return {"ok": True, "iris_configured": settings.iris_configured, "environment_label": settings.environment_label}


@app.get("/api/stack/current")
async def stack_current():
    cur = current_period()
    return await stack_for_period(cur.settlement_date, cur.settlement_period)


@app.get("/api/stack/recent")
async def stack_recent(count: int = 4):
    """The current settlement period, the `count - 1` immediately
    preceding it (all "completed" -- i.e. in the past), plus the 2
    immediately following it -- Gate Closure (1 hour before delivery) has
    already passed for both of those by the time they're this close to
    "current", so their data is final, not speculative (same reasoning as
    the engine's own rolling window, see settlement.rolling_window).
    Newest-first; periods with nothing computed yet (e.g. right after a
    fresh startup) are omitted rather than returned empty.
    """
    cur = current_period()
    periods = window_around(cur.settlement_date, cur.settlement_period, before=count - 1, after=2)
    results = []
    for sd, sp in reversed(periods):  # newest (current) first
        records = await db.stack_for_period(app.state.pool, sd, sp)
        if not records:
            continue
        view = split_and_sort([dict(r) for r in records])
        is_current = (sd, sp) == (cur.settlement_date, cur.settlement_period)
        results.append({"settlement_date": sd.isoformat(), "settlement_period": sp, "is_current": is_current, **view})
    return {"periods": results}


@app.get("/api/stack/{settlement_date}/{settlement_period}")
async def stack_for_period(settlement_date: date, settlement_period: int):
    records = await db.stack_for_period(app.state.pool, settlement_date, settlement_period)
    if not records:
        return {
            "settlement_date": settlement_date.isoformat(),
            "settlement_period": settlement_period,
            "total_misik_price_gbp_mwh": None,
            "niv_mwh": None,
            "so_flagged": [],
            "unflagged": [],
        }
    view = split_and_sort([dict(r) for r in records])
    return {"settlement_date": settlement_date.isoformat(), "settlement_period": settlement_period, **view}


@app.get("/api/accuracy/{settlement_date}/{settlement_period}")
async def accuracy_for_period(settlement_date: date, settlement_period: int):
    computed = await db.stack_for_period(app.state.pool, settlement_date, settlement_period)
    actual = await db.settlement_price_for_period(app.state.pool, settlement_date, settlement_period)
    if not computed:
        raise HTTPException(status_code=404, detail="no computed stack for that period yet")
    computed_price = float(computed[0]["total_misik_price"])
    actual_price = float(actual["system_sell_price"]) if actual and actual["system_sell_price"] is not None else None
    return {
        "settlement_date": settlement_date.isoformat(),
        "settlement_period": settlement_period,
        "computed_price_gbp_mwh": round(computed_price, 2),
        "actual_system_price_gbp_mwh": actual_price,
        "delta_gbp_mwh": round(computed_price - actual_price, 2) if actual_price is not None else None,
    }


@app.websocket("/ws/stack")
async def ws_stack(websocket: WebSocket):
    broadcaster: Broadcaster = app.state.broadcaster
    await broadcaster.connect(websocket)
    try:
        while True:
            await websocket.receive_text()  # clients don't send anything meaningful; just keeps the connection open
    except WebSocketDisconnect:
        pass
    finally:
        await broadcaster.disconnect(websocket)


@app.get("/api/fpn/current")
async def fpn_current():
    cur = current_period()
    return await fpn_for_period(cur.settlement_date, cur.settlement_period)


@app.get("/api/fpn/worst-deviants")
async def fpn_worst_deviants():
    records = await db.fpn_worst_deviants_current(app.state.pool, rolling_window())
    return {"worst_deviants": [_clean_record(r) for r in records]}


@app.get("/api/fpn/generation-by-fuel")
async def fpn_generation_by_fuel(hours: float = 3.0):
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    records = await db.fpn_generation_by_fuel_recent(app.state.pool, since)
    return {"generation_by_fuel": [_clean_record(r) for r in records]}


@app.get("/api/fpn/dashboard")
async def fpn_dashboard():
    """Everything the FPN Analytics page needs in one call: per-fuel-type
    FPN/MEL-MIL-drop/market-gen-vs-adjusted-FPN across the current window's
    settlement periods (fpn_delta is derived client-side from consecutive
    periods' own fpn_spot_vol), the Delta table, the niv_estimate decision
    table -- all "for relevant SPs" (this engine's own rolling window, same
    one the recompute itself uses) -- and `aggregated_window`, the same
    window's per-minute rows for the Generation Chart (every SP in the
    window, not just the current one).
    """
    cur = current_period()
    periods = window_around(cur.settlement_date, cur.settlement_period)
    same_date_periods = [sp for sd, sp in periods if sd == cur.settlement_date]

    by_fuel_sp = await db.fpn_by_fuel_sp_summary(app.state.pool, cur.settlement_date, same_date_periods)
    decision_drivers_sp = await db.fpn_decision_drivers_sp(app.state.pool, cur.settlement_date, same_date_periods)
    market_gen_vs_adj_fpn_series = await db.fpn_market_gen_vs_adj_fpn_series(app.state.pool, cur.settlement_date, same_date_periods)
    aggregated_window = await db.fpn_aggregated_for_periods(app.state.pool, cur.settlement_date, same_date_periods)
    return {
        "settlement_date": cur.settlement_date.isoformat(),
        "current_settlement_period": cur.settlement_period,
        "market_gen_vs_adj_fpn_series": [_clean_record(r) for r in market_gen_vs_adj_fpn_series],
        "by_fuel_sp": [_clean_record(r) for r in by_fuel_sp],
        "decision_drivers_sp": [_clean_record(r) for r in decision_drivers_sp],
        "aggregated_window": [_clean_record(r) for r in aggregated_window],
    }


@app.get("/api/fpn/{settlement_date}/{settlement_period}")
async def fpn_for_period(settlement_date: date, settlement_period: int):
    aggregated_records = await db.fpn_aggregated_for_period(app.state.pool, settlement_date, settlement_period)
    by_fuel_records = await db.fpn_by_fuel_for_period(app.state.pool, settlement_date, settlement_period)
    return {
        "settlement_date": settlement_date.isoformat(),
        "settlement_period": settlement_period,
        "aggregated": [_clean_record(r) for r in aggregated_records],
        "by_fuel": [_clean_record(r) for r in by_fuel_records],
    }


@app.websocket("/ws/fpn")
async def ws_fpn(websocket: WebSocket):
    broadcaster: FpnBroadcaster = app.state.fpn_broadcaster
    await broadcaster.connect(websocket)
    try:
        while True:
            await websocket.receive_text()  # clients don't send anything meaningful; just keeps the connection open
    except WebSocketDisconnect:
        pass
    finally:
        await broadcaster.disconnect(websocket)


@app.websocket("/ws/trips")
async def ws_trips(websocket: WebSocket):
    broadcaster: TripBroadcaster = app.state.trip_broadcaster
    await broadcaster.connect(websocket)
    try:
        while True:
            await websocket.receive_text()  # clients don't send anything meaningful; just keeps the connection open
    except WebSocketDisconnect:
        pass
    finally:
        await broadcaster.disconnect(websocket)


def _with_affected_sps(d: dict, end_dt) -> dict:
    """The trip's own settlement_date/settlement_period (already on the
    record) is the first SP it affects; the "expected/actual back" instant
    (REMIT's eventEndTime, or resolved_at once truly resolved) converts to
    the last one via the same settlement.py utc_to_settlement() every other
    Elexon-time-to-SP conversion in this codebase uses.
    """
    if end_dt is not None:
        end_sd, end_sp = utc_to_settlement(end_dt)
        d["end_settlement_date"] = end_sd.isoformat()
        d["end_settlement_period"] = end_sp
    else:
        d["end_settlement_date"] = None
        d["end_settlement_period"] = None
    return d


@app.get("/api/trips/recent")
async def trips_recent(limit: int = 200, days: int = 2):
    """The Plant Trips table: every trip that is still ongoing, plus resolved trips from the
    last `days` days (by when they resolved, falling back to when they were detected)."""
    records = await db.fetch_recent_trips(app.state.pool, 1000)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    kept = [r for r in records if r["status"] != "resolved" or (r["resolved_at"] or r["detected_at"]) >= cutoff][:limit]
    trips = [_with_affected_sps(_clean_record(r), r["resolved_at"] or r["event_end_time"]) for r in kept]

    # A plant can be unavailable without ever publishing a REMIT notice. For the open trips REMIT has not
    # matched, say what the plant's own MEL shows (a cut, since when, and when it is due back).
    now = datetime.now(timezone.utc)
    unmatched = [t["bm_unit"] for t in trips if t["status"] != "resolved" and not t.get("remit_mrid")]
    by_unit: dict[str, list[dict]] = {}
    for r in await db.trip_telemetry_for_units(app.state.pool, sorted(set(unmatched)), now - timedelta(hours=3), now + timedelta(hours=3)):
        by_unit.setdefault(r["bm_unit"], []).append(dict(r))
    for t in trips:
        if t["status"] != "resolved" and not t.get("remit_mrid"):
            t["mel_evidence"] = mel_evidence(by_unit.get(t["bm_unit"], []), now)
    return {"trips": trips}


@app.get("/api/trips/{trip_id}/revisions")
async def trip_revisions(trip_id: int):
    trips = await db.fetch_recent_trips(app.state.pool, 500)
    trip = next((t for t in trips if t["id"] == trip_id), None)
    if trip is None or not trip["remit_mrid"]:
        return {"trip_id": trip_id, "revisions": []}
    records = await db.fetch_remit_revisions(app.state.pool, trip["remit_mrid"])
    revisions = [_with_affected_sps(_clean_record(r), r["event_end_time"]) for r in records]
    return {
        "trip_id": trip_id, "mrid": trip["remit_mrid"],
        "start_settlement_date": trip["settlement_date"].isoformat(), "start_settlement_period": trip["settlement_period"],
        "revisions": revisions,
    }


@app.get("/api/trips/{trip_id}/telemetry")
async def trip_telemetry(trip_id: int):
    """GridTrip-style chart data for one trip: the unit's FPN, adjusted FPN and
    MEL from 1.5h before its settlement period to 3h past now (its published
    plan), plus the headline loss figures. Fed from trip_telemetry, which the
    FPN runner fills every cycle, so it works for any trip, not just the live
    ones -- the REMIT side stays on /api/trips/{id}/revisions."""
    trips = await db.fetch_recent_trips(app.state.pool, 500)
    trip = next((t for t in trips if t["id"] == trip_id), None)
    if trip is None:
        raise HTTPException(status_code=404, detail="unknown trip")
    now = datetime.now(timezone.utc)
    ongoing = trip["status"] != "resolved"
    if ongoing:
        # Still tripped: chart the last 3 hours and the published plan for the next 3, with the
        # headline figures for the period it is in now -- not for the period it first tripped in,
        # which may be days ago and long out of the telemetry window.
        cur = current_period(now)
        sp_start = sp_start_utc(cur.settlement_date, cur.settlement_period)
        start, end = now - timedelta(hours=3), now + timedelta(hours=3)
    else:
        sp_start = sp_start_utc(trip["settlement_date"], trip["settlement_period"])
        start, end = sp_start - timedelta(hours=1, minutes=30), min(now + timedelta(hours=3), sp_start + timedelta(hours=14))
    records = await db.trip_telemetry_between(app.state.pool, trip["bm_unit"], start, end)
    chart = build_trip_chart([dict(r) for r in records], sp_start, now)
    return {
        "trip": _clean_record(trip), "sp_start": sp_start.isoformat(), "detected_at": trip["detected_at"].isoformat(),
        "ongoing": ongoing, "window_start": start.isoformat(), "has_data": bool(records), **chart,
    }


@app.get("/api/trips/worst-behaviour")
async def trips_worst_behaviour_graph():
    """reference-app-style "Worst Behaviour Plants" data: for each tripped (or
    just-recovered) unit, its effective MW (`vol`), FPN and MEL across the
    last-3 .. next-2 settlement periods -- the future part of `vol` is the
    plant's published plan for coming back. Built by engine/fpn.py's
    worst_behaviour_series() inside every FPN recompute.
    """
    runner: FpnRunner = app.state.fpn_runner
    units: dict[str, dict] = {}
    for r in runner.latest_wb_series:
        u = units.setdefault(r["bm_unit"], {"bm_unit": r["bm_unit"], "fuel_type": r["fuel_type"], "status": r["status"], "series": []})
        # NaN/inf are not valid JSON (the encoder raises, i.e. a 500) -- a unit with no usable
        # value in one minute must show as a gap, not take the whole endpoint down.
        u["series"].append({"t": r["spot_time"], "sp": r["settlement_period"], "fpn": _finite(r["fpn"]),
                            "mel": _finite(r["mel"]), "vol": _finite(r["vol"])})
    cur = current_period()
    return {
        "computed_at": runner.wb_computed_at.isoformat() if runner.wb_computed_at else None,
        "current_period": cur.settlement_period,
        "units": sorted(units.values(), key=lambda u: (u["status"] != "tripped", u["bm_unit"])),
    }


@app.get("/api/trips/mel-plan")
async def trips_mel_plan(bm_unit: str):
    """How `bm_unit`'s MEL (its plan for coming back) has changed with each
    notification since it tripped -- see engine/mel_plan.py."""
    from ..engine.mel_plan import mel_vintages

    runner: FpnRunner = app.state.fpn_runner
    now = datetime.now(timezone.utc)
    trips = [t for t in await db.fetch_recent_trips(app.state.pool, 200) if t["bm_unit"] == bm_unit]
    trip_at = trips[0]["detected_at"] if trips else now - timedelta(hours=3)
    vintages = mel_vintages(runner.shared_buffers.frame("mel"), bm_unit, trip_at)
    fpn = [
        {"t": r["spot_time"], "fpn": r["fpn"]}
        for r in runner.latest_wb_series if r["bm_unit"] == bm_unit
    ]
    return {"bm_unit": bm_unit, "trip_at": trip_at.strftime("%Y-%m-%dT%H:%M:%SZ"), "vintages": vintages, "fpn": fpn}


@app.get("/api/trips/worst-behavior")
async def trips_worst_behavior():
    records = await db.fetch_worst_behavior_profile(app.state.pool)
    return {"units": [_clean_record(r) for r in records]}


@app.get("/api/fundies/real-time")
async def fundies_real_time(settlement_date: date | None = None):
    sd = settlement_date or current_period().settlement_date
    records = await db.fundies_real_time_for_date(app.state.pool, sd)
    return {"settlement_date": sd.isoformat(), "rows": [_clean_record(r) for r in records]}


@app.get("/api/fundies/day-ahead")
async def fundies_day_ahead(settlement_date: date | None = None):
    sd = settlement_date or current_period().settlement_date
    records = await db.fundies_day_ahead_for_date(app.state.pool, sd)
    return {"settlement_date": sd.isoformat(), "rows": [_clean_record(r) for r in records]}


@app.get("/api/fundies/interconnectors")
async def fundies_interconnectors(settlement_date: date | None = None):
    """Per-pair SCHEDULED flow series (ENTSO-E's 7 pairs + SEMO's 3) plus
    the REAL (FUELHH-metered) flow for the same pairs on `settlement_date`
    (defaults to today) -- backs the interconnector graphs page's
    scheduled-vs-real overlay.
    """
    sd = settlement_date or current_period().settlement_date
    entsoe_rows = await db.fundies_cache_rows(app.state.pool, "fundies_entsoe_flows", [sd])
    semo_rows = await db.fundies_cache_rows(app.state.pool, "fundies_semo_flows", [sd])
    real_rows = await db.fundies_real_time_for_date(app.state.pool, sd)
    return {
        "settlement_date": sd.isoformat(),
        "entsoe": [_clean_record(r) for r in entsoe_rows],
        "semo": [_clean_record(r) for r in semo_rows],
        "real": [_clean_record(r) for r in real_rows],
    }


@app.websocket("/ws/fundies")
async def ws_fundies(websocket: WebSocket):
    broadcaster: FundiesBroadcaster = app.state.fundies_broadcaster
    await broadcaster.connect(websocket)
    try:
        while True:
            await websocket.receive_text()  # clients don't send anything meaningful; just keeps the connection open
    except WebSocketDisconnect:
        pass
    finally:
        await broadcaster.disconnect(websocket)


@app.get("/api/all-plants-boalf")
async def all_plants_boalf():
    """The current in-memory rolling window (no DB table -- see
    engine/stack.py's exploded_boalf_by_unit() docstring) for a fresh
    page's first paint, before its own WebSocket delivers the first push.
    """
    cur = current_period()
    df = app.state.runner.latest_exploded_boalf
    if df.empty:
        return {"rows": [], "live_settlement_date": cur.settlement_date.isoformat(), "live_settlement_period": cur.settlement_period}
    rows = [
        {
            "settlement_date": r.settlementDate.isoformat() if hasattr(r.settlementDate, "isoformat") else str(r.settlementDate),
            "settlement_period": int(r.settlementPeriod),
            "bmUnit": r.bmUnit,
            "spot_time": r.spot_time.isoformat(),
            "delta": float(r.delta),
            "fuel_bucket": r.fuel_bucket,
        }
        for r in df.itertuples()
    ]
    return {"rows": rows, "live_settlement_date": cur.settlement_date.isoformat(), "live_settlement_period": cur.settlement_period}


@app.websocket("/ws/all-plants-boalf")
async def ws_all_plants_boalf(websocket: WebSocket):
    broadcaster: ExplodedBoalfBroadcaster = app.state.exploded_boalf_broadcaster
    await broadcaster.connect(websocket)
    try:
        while True:
            await websocket.receive_text()  # clients don't send anything meaningful; just keeps the connection open
    except WebSocketDisconnect:
        pass
    finally:
        await broadcaster.disconnect(websocket)


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
async def index():
    # FPN Analytics is the default landing page (per user request) -- the
    # original pricing-stack page moved to its own path below rather than
    # disappearing.
    return FileResponse(str(WEB_DIR / "fpn.html"))


@app.get("/fpn")
async def fpn_page():
    return FileResponse(str(WEB_DIR / "fpn.html"))


@app.get("/pricing-stack")
async def pricing_stack_page():
    return FileResponse(str(WEB_DIR / "index.html"))


@app.get("/fundies")
async def fundies_page():
    return FileResponse(str(WEB_DIR / "fundies.html"))


@app.get("/fundies-graphs")
async def fundies_graphs_page():
    return FileResponse(str(WEB_DIR / "fundies-graphs.html"))


@app.get("/interconnectors")
async def interconnectors_page():
    return FileResponse(str(WEB_DIR / "interconnectors.html"))


@app.get("/all-plants-boalf")
async def all_plants_boalf_page():
    return FileResponse(str(WEB_DIR / "all-plants-boalf.html"))


@app.get("/trips")
async def trips_page():
    return FileResponse(str(WEB_DIR / "trips.html"))


@app.get("/natgrid")
async def natgrid_page():
    return FileResponse(str(WEB_DIR / "natgrid.html"))


async def _stored_natgrid_trades(days: int) -> tuple[list[dict], list[dict]]:
    """NESO trade rows and DISBSAD actions from the local database for the last
    `days` settlement days through tomorrow (see ingest/natgrid_store.py)."""
    today = current_period().settlement_date
    first, last = today - timedelta(days=days - 1), today + timedelta(days=1)
    midnight = datetime(first.year, first.month, first.day, tzinfo=timezone.utc)
    blocks = await db.natgrid_blocks_between(
        app.state.pool, midnight - timedelta(days=1), datetime(last.year, last.month, last.day, tzinfo=timezone.utc) + timedelta(days=1))
    neso_rows = [r for r in gtma_blocks_to_sp_rows(blocks) if first.isoformat() <= r["Date"] <= last.isoformat()]
    return neso_rows, await db.disbsad_between(app.state.pool, first, last)


@app.get("/api/natgrid")
async def natgrid(days: int = Query(2, ge=1, le=60)):
    """National Grid's balancing trades per settlement period as a price
    ladder (see engine/natgrid.py), from the locally stored NESO trades with
    stored DISBSAD filling periods NESO has none for. `days` is how many
    settlement days back to include (today counts as one).
    Shape: {data: {date: {sp: [{side, disc_cumm, price, ...}]}}}.
    """
    neso_rows, disbsad = await _stored_natgrid_trades(days)
    return {"data": build_ladder(neso_rows, disbsad)}


@app.get("/api/natgrid/reconcile")
async def natgrid_reconcile(days: int = Query(2, ge=1, le=60)):
    """NESO trades vs Elexon DISBSAD, period by period. They describe the same
    actions so should agree; periods where they do not are listed first.
    `neso_only` just means DISBSAD has not been published for that period yet."""
    neso_rows, disbsad = await _stored_natgrid_trades(days)
    periods = reconcile(neso_rows, disbsad)
    counts: dict[str, int] = {}
    for p in periods:
        counts[p["status"]] = counts.get(p["status"], 0) + 1
    return {"counts": counts, "mismatches": [p for p in periods if p["status"] == "mismatch"], "periods": periods}


@app.get("/bm-stack")
async def bm_stack_page():
    return FileResponse(str(WEB_DIR / "bm-stack.html"))


@app.get("/api/bm-stack")
async def bm_stack():
    """The current in-memory BM Stack snapshot (see engine/bm_stack.py):
    untouched bids/offers for the last 3 .. next 2 settlement periods.
    `enabled: false` where BM_STACK_ENABLED is off (prod until promoted).
    """
    if not settings.bm_stack_enabled:
        return {"enabled": False, "computed_at": None, "periods": [], "rows": []}
    runner: FpnRunner = app.state.fpn_runner
    rows = runner.latest_bm_stack
    periods = sorted({(r["settlement_date"], r["settlement_period"]) for r in rows})
    cur = current_period()
    return {
        "enabled": True,
        "current": {"settlement_date": cur.settlement_date.isoformat(), "settlement_period": cur.settlement_period},
        "computed_at": runner.bm_stack_computed_at.isoformat() if runner.bm_stack_computed_at else None,
        "periods": [{"settlement_date": sd, "settlement_period": sp} for sd, sp in periods],
        "rows": rows,
    }
