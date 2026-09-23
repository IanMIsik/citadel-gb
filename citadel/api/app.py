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
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..config import settings
from ..engine.fpn_runner import FpnRunner
from ..engine.runner import Runner
from ..engine.view import split_and_sort
from ..ingest import gbpw_import, misco_fuel_type_reference
from ..ingest.elexon_rest import fetch_bmu_reference
from ..settlement import current_period, window_around
from ..storage import db
from .broadcast import Broadcaster
from .fpn_broadcast import FpnBroadcaster

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("citadel.api.app")

WEB_DIR = __import__("pathlib").Path(__file__).resolve().parents[1] / "web"


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

    # Shared by both engines' per-cycle recompute (see engine/runner.py and
    # engine/fpn_runner.py) so neither one's pandas-heavy work ever blocks
    # this event loop -- the two recomputes are independent (same raw
    # input, no shared mutable state) and already triggered concurrently,
    # so running them in separate OS processes is genuine parallel CPU
    # work, not a cosmetic addition.
    process_pool = ProcessPoolExecutor()
    app.state.process_pool = process_pool

    broadcaster = Broadcaster()
    app.state.broadcaster = broadcaster
    runner = Runner(settings, pool, broadcaster, process_pool=process_pool)
    app.state.runner = runner
    runner_task = asyncio.create_task(runner.run())

    fpn_broadcaster = FpnBroadcaster()
    app.state.fpn_broadcaster = fpn_broadcaster
    fpn_runner = FpnRunner(settings, pool, fpn_broadcaster, runner.buffers, process_pool)
    app.state.fpn_runner = fpn_runner
    fpn_runner_task = asyncio.create_task(fpn_runner.run())

    yield

    await runner.stop()
    runner_task.cancel()
    await fpn_runner.stop()
    fpn_runner_task.cancel()
    process_pool.shutdown(wait=False, cancel_futures=True)
    await pool.close()


app = FastAPI(title="Citadel Pricing Stack", lifespan=lifespan)


@app.get("/api/health")
async def health():
    return {"ok": True, "iris_configured": settings.iris_configured}


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
    records = await db.fpn_worst_deviants_current(app.state.pool)
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


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
async def index():
    return FileResponse(str(WEB_DIR / "index.html"))


@app.get("/fpn")
async def fpn_page():
    return FileResponse(str(WEB_DIR / "fpn.html"))
