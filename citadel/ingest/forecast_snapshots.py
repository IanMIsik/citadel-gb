"""Live forecast snapshots for the demand forecaster (`citadel snapshot-demand-forecasts`). Dev only.

Why: a model trained on today's revised numbers would look better than it can really be. Most forecast
vintages can be rebuilt from their publishers (Elexon keeps every NDF/WINDFOR publication, NESO's embedded
archive keeps every issue), so one cycle simply re-reads the last few days of those. Weather is different:
Open-Meteo only keeps a re-analysed series, so the only honest record of "what was forecast at 14:00" is
to ask at 14:00 and keep the answer. That is what `demand_weather_forecasts` accumulates.

One cycle:
  1. weather: Open-Meteo's forecast for the next ~3 days at each point, stored with its issue time;
  2. embedded wind/solar: NESO's issues from the last `embedded_days` days (SQL endpoint, small);
  3. Elexon NDF/TSDF/WINDFOR, outturn, FUELINST and PV_Live for the last few days (the backfill code).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

import asyncpg
import httpx

from citadel.ingest import demand_backfill as bf
from citadel.storage import db

logger = logging.getLogger(__name__)

OPEN_METEO_FORECAST = "https://api.open-meteo.com/v1/forecast"
NESO_SQL = f"{bf.NESO_API}/datastore_search_sql"
FORECAST_DAYS = 3
OFFICIAL_DATASETS = ["ndf", "tsdf", "windfor", "outturn", "fuelinst", "pv_live"]


def issue_time(now: datetime | None = None) -> datetime:
    """Snapshots are keyed to the hour they were taken, so two runs in the same hour don't duplicate."""
    now = now or datetime.now(timezone.utc)
    return now.replace(minute=0, second=0, microsecond=0)


def forecast_rows(issued_at: datetime, point: str, hourly: dict) -> list[tuple]:
    """Open-Meteo forecast payload -> demand_weather_forecasts tuples."""
    return [(issued_at, p, ts, *vals) for (p, ts, *vals) in bf.weather_rows(point, hourly)]


async def snapshot_weather(client: httpx.AsyncClient, pool: asyncpg.Pool, issued_at: datetime | None = None) -> int:
    issued_at = issued_at or issue_time()

    async def one(point: str, lat: float, lon: float) -> list[tuple]:
        data = await bf._get_json(client, OPEN_METEO_FORECAST, {
            "latitude": lat, "longitude": lon, "hourly": ",".join(bf.WEATHER_VARS),
            "forecast_days": FORECAST_DAYS, "timezone": "UTC"})
        return forecast_rows(issued_at, point, data["hourly"])

    results = await asyncio.gather(*(one(p, lat, lon) for p, (lat, lon) in bf.WEATHER_POINTS.items()), return_exceptions=True)
    rows: list[tuple] = []
    for point, res in zip(bf.WEATHER_POINTS, results):
        if isinstance(res, BaseException):
            logger.warning("weather snapshot for %s failed: %s", point, res)
        else:
            rows.extend(res)
    return await db.upsert_demand_weather_forecasts(pool, rows)


async def refresh_embedded_recent(client: httpx.AsyncClient, pool: asyncpg.Pool, days: int = 1, today: date | None = None) -> int:
    """Re-read NESO's embedded forecast issues from the last `days` days through the SQL endpoint (one small query per
    day) instead of downloading the whole multi-hundred-MB year file."""
    today = today or datetime.now(timezone.utc).date()
    res = await bf._neso_resource(client, bf.EMBEDDED_PACKAGE_ID, f"Embedded Solar and Wind Forecast Archive {today.year}")
    if res is None:
        return 0
    reducer = bf.EmbeddedReducer()
    for back in range(days + 1):
        day = today - timedelta(days=back)
        sql = (f'SELECT "SETTLEMENT_DATE", "SETTLEMENT_PERIOD", "EMBEDDED_WIND_FORECAST", "EMBEDDED_WIND_CAPACITY", '
               f'"EMBEDDED_SOLAR_FORECAST", "EMBEDDED_SOLAR_CAPACITY", "Forecast_Datetime" FROM "{res["id"]}" '
               f"WHERE \"Forecast_Datetime\" >= '{day.isoformat()}T00:00:00Z' AND \"Forecast_Datetime\" < '{(day + timedelta(days=1)).isoformat()}T00:00:00Z'")
        data = await bf._get_json(client, NESO_SQL, {"sql": sql})
        reducer.feed_records(data["result"]["records"])
    return await db.upsert_demand_embedded_vintages(pool, reducer.rows())


async def snapshot_cycle(pool: asyncpg.Pool, embedded_days: int = 1, official_days: int = 3) -> dict[str, int | str]:
    """One pass over every source. A failing source is logged and reported, never fatal."""
    out: dict[str, int | str] = {}
    async with httpx.AsyncClient(timeout=httpx.Timeout(40.0)) as client:
        steps = {
            "weather": lambda: snapshot_weather(client, pool),
            "embedded": lambda: refresh_embedded_recent(client, pool, embedded_days),
        }
        for name, step in steps.items():
            try:
                out[name] = await step()
            except Exception as e:  # noqa: BLE001
                logger.warning("snapshot step %s failed", name, exc_info=True)
                out[name] = f"failed: {type(e).__name__}"
    try:
        today = datetime.now(timezone.utc).date()
        summary = await bf.run_backfill(pool, today - timedelta(days=official_days), today, OFFICIAL_DATASETS, concurrency=3, force=True)
        out["official"] = sum(v["rows"] for v in summary.values())
        failed = sum(v["failed"] for v in summary.values())
        if failed:
            out["official_failed_chunks"] = failed
    except Exception as e:  # noqa: BLE001
        logger.warning("snapshot step official failed", exc_info=True)
        out["official"] = f"failed: {type(e).__name__}"
    return out


# ---------------------------------------------------------------------------------------------
# Using the snapshots: the weather that was really forecast at decision time
# ---------------------------------------------------------------------------------------------

def asof_weather(snapshots, target_starts, lead_min: int):
    """For each period start, the newest snapshot issued at least `lead_min` before it started (decision time
    T = start - lead), averaged across points. `snapshots`: DataFrame with issued_at, point, ts and the weather
    columns. Returns a DataFrame indexed by target_start with the four variables (NaN where nothing qualifies).

    Hourly forecast values are linearly interpolated onto the half-hour grid first, as build_base does."""
    import pandas as pd

    cols = list(bf.WEATHER_VARS)
    if snapshots.empty:
        return pd.DataFrame(index=pd.DatetimeIndex(target_starts), columns=cols, dtype=float)
    snap = snapshots.copy()
    snap["issued_at"] = pd.to_datetime(snap["issued_at"], utc=True)
    snap["ts"] = pd.to_datetime(snap["ts"], utc=True)
    targets = pd.DatetimeIndex(target_starts)
    out = pd.DataFrame(index=targets, columns=cols, dtype=float)
    issues = pd.DatetimeIndex(sorted(snap["issued_at"].unique()))
    # the issue that is "as of" each target: latest issued_at <= start - lead
    pos = issues.searchsorted(targets - pd.Timedelta(minutes=lead_min), side="right") - 1
    chosen = pd.Series(pd.NaT, index=targets, dtype="datetime64[ns, UTC]")
    valid = pos >= 0
    chosen[valid] = issues[pos[valid]]
    for issued, group_idx in chosen.dropna().groupby(chosen.dropna()).groups.items():
        part = snap[snap["issued_at"] == issued]
        hourly = part.groupby("ts")[cols].mean()
        both = hourly.index.union(group_idx)
        grid = hourly.reindex(both).interpolate(limit=3).reindex(group_idx)
        out.loc[group_idx, cols] = grid.to_numpy()
    return out
