"""Historical backfill for the demand forecaster (`citadel backfill-demand`).

The aim is a training set for a model of the demand error (INDO minus NDF), built from what
was *known at the time*, not from today's revised values. So forecasts are stored as
"vintages": for every period we keep the forecast published about 0, 30, 60, 120, 240, 480,
720 and 1080 minutes before that period started (`LEAD_MINUTES`), plus the first-published
outturns, 5-minute FUELINST, NESO's embedded wind/solar estimates, PV_Live, weather and the
bank-holiday calendar.

A vintage is "as of" a lead time: for lead L the stored forecast is the latest one published at
least L minutes before the period started, which is exactly what a model running L minutes ahead
could have seen. `staleness_min` records how much older than that cut-off it is (0 = published right
at the cut-off); a slot with nothing recent enough is left empty rather than filled with an old value.

Everything is cut into chunks (a day, a week, a month or a year depending on the source's
limits). Finished chunks are logged in `demand_backfill_log`, so an interrupted run resumes
and a repeat run only fetches what is missing. Chunks that touch the last two days are always
redone because their data is still arriving.

Published NDF/INDO already include the effect of embedded generation; nothing here adjusts
for it. The embedded series are stored only as explanatory inputs.
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
from bisect import bisect_left, bisect_right
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import asyncpg
import httpx

from citadel.ingest import elexon_rest as elexon
from citadel.ingest import pvlive
from citadel.settlement import sp_start_utc
from citadel.storage import db

logger = logging.getLogger(__name__)

LEAD_MINUTES = (0, 30, 60, 120, 240, 480, 720, 1080)
# How much older than the cut-off a forecast may be and still count as that lead's vintage.
DEFAULT_MAX_STALE_MIN = 60

NESO_API = "https://api.neso.energy/api/3/action"
HISTORIC_DEMAND_PACKAGE_ID = "8f2fe0af-871c-488d-8bad-960426f24601"
EMBEDDED_PACKAGE_ID = "91c0c70e-0ef5-4116-b6fa-7ad084b5e0e8"
OPEN_METEO = "https://historical-forecast-api.open-meteo.com/v1/forecast"
BANK_HOLIDAYS_URL = "https://www.gov.uk/bank-holidays.json"

# Rough population centres, so weather features track where demand actually is.
WEATHER_POINTS = {
    "london": (51.51, -0.13),
    "birmingham": (52.48, -1.90),
    "manchester": (53.48, -2.24),
    "glasgow": (55.86, -4.25),
    "bristol": (51.45, -2.59),
}
WEATHER_VARS = ("temperature_2m", "cloud_cover", "shortwave_radiation", "wind_speed_100m")

# dataset -> (Elexon name, value column, max staleness in minutes). WINDFOR is published only eight times a day.
VINTAGE_DATASETS = {"ndf": ("NDF", "demand", 60), "tsdf": ("TSDF", "demand", 60), "windfor": ("WINDFOR", "generation", 360)}
ALL_DATASETS = ("ndf", "tsdf", "windfor", "outturn", "fuelinst", "neso_historic", "embedded_forecast",
                "pv_live", "weather", "calendar")

# Days of recent data that are still changing; a chunk reaching into them is never skipped.
FRESH_DAYS = 2


# ---------------------------------------------------------------------------------------------
# Pure helpers (unit-tested)
# ---------------------------------------------------------------------------------------------

def parse_ts(value: str) -> datetime:
    """ISO 8601 with a trailing Z (or none) -> aware UTC datetime."""
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def lead_minutes(publish: datetime, target_start: datetime) -> float:
    return (target_start - publish).total_seconds() / 60.0


class VintagePicker:
    """Keeps, per (target period, lead), the latest forecast published at least `lead` minutes before the
    period started, if it is at most `max_stale_min` older than that cut-off. Works on a stream, so a
    multi-million-row file is never held in memory."""

    def __init__(self, leads: Iterable[int] = LEAD_MINUTES, max_stale_min: int = DEFAULT_MAX_STALE_MIN):
        self.leads = sorted(set(leads))
        self.max_stale = max_stale_min
        self.best: dict[tuple[datetime, int], tuple[float, datetime, object]] = {}

    def add(self, target_start: datetime, publish: datetime, payload: object) -> None:
        minutes = lead_minutes(publish, target_start)
        if minutes < 0 or minutes > self.leads[-1] + self.max_stale:
            return
        for lead in self.leads[bisect_left(self.leads, minutes - self.max_stale):bisect_right(self.leads, minutes)]:
            stale = minutes - lead
            key = (target_start, lead)
            cur = self.best.get(key)
            if cur is None or stale < cur[0] or (stale == cur[0] and publish > cur[1]):
                self.best[key] = (stale, publish, payload)

    def items(self) -> Iterator[tuple[datetime, int, datetime, float, object]]:
        """(target_start, lead_min, publish_time, staleness_min, payload)"""
        for (target, lead), (stale, publish, payload) in self.best.items():
            yield target, lead, publish, stale, payload


def vintage_rows(dataset_label: str, api_rows: list[dict], value_key: str, max_stale_min: int = DEFAULT_MAX_STALE_MIN) -> list[tuple]:
    """NDF/TSDF/WINDFOR rows -> demand_forecast_vintages tuples. Only the national boundary is kept
    where a boundary column exists."""
    picker = VintagePicker(max_stale_min=max_stale_min)
    for r in api_rows:
        if r.get("boundary", "N") != "N":
            continue
        picker.add(parse_ts(r["startTime"]), parse_ts(r["publishTime"]), r.get(value_key))
    return [(dataset_label, target, lead, publish, stale, value) for target, lead, publish, stale, value in picker.items()]


def outturn_rows(api_rows: list[dict]) -> list[tuple]:
    out = []
    for r in api_rows:
        out.append((parse_ts(r["startTime"]), date.fromisoformat(str(r["settlementDate"])[:10]), int(r["settlementPeriod"]),
                    r.get("initialDemandOutturn"), r.get("initialTransmissionSystemDemandOutturn")))
    return out


def fuelinst_rows(api_rows: list[dict]) -> list[tuple]:
    """Group per 5-minute block: total across fuel types plus the per-fuel split (JSON text)."""
    blocks: dict[datetime, dict[str, float]] = {}
    for r in api_rows:
        if r.get("generation") is None:
            continue
        blocks.setdefault(parse_ts(r["startTime"]), {})[r["fuelType"]] = float(r["generation"])
    return [(start, sum(by_fuel.values()), json.dumps(by_fuel, sort_keys=True)) for start, by_fuel in sorted(blocks.items())]


def _num(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _day(value) -> date:
    s = str(value).strip()
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return datetime.strptime(s, "%d-%b-%Y").date()


def neso_historic_rows(records: list[dict]) -> list[tuple]:
    out = []
    for r in records:
        up = {str(k).upper(): v for k, v in r.items()}
        if not up.get("SETTLEMENT_DATE") or not up.get("SETTLEMENT_PERIOD"):
            continue
        out.append((_day(up["SETTLEMENT_DATE"]), int(up["SETTLEMENT_PERIOD"]),
                    *[_num(up.get(c.upper())) for c in db._NESO_HISTORIC_COLS]))
    return out


class EmbeddedReducer:
    """Streams a NESO embedded-forecast archive CSV (millions of rows, many forecasts per period) down to
    the vintages we keep (as-of each lead). Feed it blocks of lines; the first line it sees must be the header."""

    def __init__(self) -> None:
        self.picker = VintagePicker()
        self.fieldnames: list[str] | None = None
        self._starts: dict[tuple[str, str], datetime] = {}
        self._publishes: dict[str, datetime] = {}

    def feed(self, lines: list[str]) -> None:
        if self.fieldnames is None:
            if not lines:
                return
            self.fieldnames = next(csv.reader([lines[0]]))
            lines = lines[1:]
        for r in csv.DictReader(lines, fieldnames=self.fieldnames):
            try:
                key = (r["SETTLEMENT_DATE"], r["SETTLEMENT_PERIOD"])
                target = self._starts.get(key)
                if target is None:
                    target = self._starts[key] = sp_start_utc(_day(r["SETTLEMENT_DATE"]), int(r["SETTLEMENT_PERIOD"]))
                pub_raw = r["Forecast_Datetime"]
                publish = self._publishes.get(pub_raw)
                if publish is None:
                    publish = self._publishes[pub_raw] = parse_ts(pub_raw)
            except (KeyError, TypeError, ValueError):
                continue
            self.picker.add(target, publish, (_num(r.get("EMBEDDED_WIND_FORECAST")), _num(r.get("EMBEDDED_WIND_CAPACITY")),
                                              _num(r.get("EMBEDDED_SOLAR_FORECAST")), _num(r.get("EMBEDDED_SOLAR_CAPACITY"))))

    def rows(self) -> list[tuple]:
        """(target_start, lead_min, publish_time, staleness_min, wind, wind_cap, solar, solar_cap)"""
        return [(target, lead, publish, stale, *payload) for target, lead, publish, stale, payload in self.picker.items()]


def pv_live_rows(api_rows: list[dict]) -> list[tuple]:
    return [(parse_ts(r["start_time"]), r.get("pv_live"), r.get("pv_lower_limit"), r.get("pv_upper_limit")) for r in api_rows]


def weather_rows(point: str, hourly: dict) -> list[tuple]:
    times = hourly.get("time", [])
    cols = [hourly.get(v, [None] * len(times)) for v in WEATHER_VARS]
    return [(point, parse_ts(t), *[c[i] for c in cols]) for i, t in enumerate(times)]


def calendar_rows(payload: dict) -> list[tuple]:
    out = []
    for region, body in payload.items():
        for ev in body.get("events", []):
            out.append((date.fromisoformat(ev["date"]), region, ev.get("title")))
    return out


@dataclass(frozen=True)
class Chunk:
    dataset: str
    start: date
    end: date


def _split(start: date, end: date, days: int) -> Iterator[tuple[date, date]]:
    cur = start
    while cur <= end:
        yield cur, min(cur + timedelta(days=days - 1), end)
        cur += timedelta(days=days)


def plan_chunks(dataset: str, start: date, end: date) -> list[Chunk]:
    """Chunk size follows each source's limits: Elexon vintages and FUELINST per day, outturn per 7 days
    (its maximum), PV_Live per month, weather per 120 days and point, NESO files per calendar year."""
    if dataset in ("ndf", "tsdf", "windfor", "fuelinst"):
        return [Chunk(dataset, a, b) for a, b in _split(start, end, 1)]
    if dataset == "outturn":
        return [Chunk(dataset, a, b) for a, b in _split(start, end, 7)]
    if dataset == "pv_live":
        return [Chunk(dataset, a, b) for a, b in _split(start, end, 30)]
    if dataset == "weather":
        return [Chunk(f"weather:{p}", a, b) for p in WEATHER_POINTS for a, b in _split(start, end, 120)]
    if dataset in ("neso_historic", "embedded_forecast"):
        return [Chunk(dataset, date(y, 1, 1), date(y, 12, 31)) for y in range(start.year, end.year + 1)]
    if dataset == "calendar":
        return [Chunk("calendar", date(2000, 1, 1), date(2100, 1, 1))]
    raise ValueError(f"unknown dataset {dataset!r}")


def is_finished(chunk: Chunk, done: dict[tuple[str, date], date], today: date) -> bool:
    """A logged chunk counts as finished unless it still reaches into the days that are changing, or
    it was logged with a different span than the one planned now."""
    logged_end = done.get((chunk.dataset, chunk.start))
    if logged_end is None or logged_end != chunk.end:
        return False
    return chunk.dataset == "calendar" or chunk.end < today - timedelta(days=FRESH_DAYS)


# ---------------------------------------------------------------------------------------------
# Fetching and storing, one function per dataset
# ---------------------------------------------------------------------------------------------

def _utc_bounds(chunk: Chunk) -> tuple[str, str]:
    return f"{chunk.start.isoformat()}T00:00:00Z", f"{chunk.end.isoformat()}T23:59:59Z"


async def _get_json(client: httpx.AsyncClient, url: str, params: dict | None = None, attempts: int = 5):
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            resp = await client.get(url, params=params, headers={"Accept": "application/json"}, follow_redirects=True)
            if resp.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            last = e
            await asyncio.sleep(2.0 * (attempt + 1))
    raise last  # type: ignore[misc]


async def _store_vintages(client, pool, chunk: Chunk) -> int:
    label, value_key, max_stale = VINTAGE_DATASETS[chunk.dataset]
    frm, to = _utc_bounds(chunk)
    params = {"publishDateTimeFrom": frm, "publishDateTimeTo": to}
    if label == "TSDF":
        params["boundary"] = "N"
    api_rows = await elexon._get_stream(client, f"{elexon.BASE}/datasets/{label}/stream", params)
    return await db.upsert_demand_vintages(pool, vintage_rows(label, api_rows, value_key, max_stale))


async def _store_outturn(client, pool, chunk: Chunk) -> int:
    data = await _get_json(client, f"{elexon.BASE}/demand/outturn",
                           {"settlementDateFrom": chunk.start.isoformat(), "settlementDateTo": chunk.end.isoformat(), "format": "json"})
    api_rows = data if isinstance(data, list) else data.get("data", [])
    return await db.upsert_demand_outturn(pool, outturn_rows(api_rows))


async def _store_fuelinst(client, pool, chunk: Chunk) -> int:
    frm, to = _utc_bounds(chunk)
    api_rows = await elexon._get_stream(client, f"{elexon.BASE}/datasets/FUELINST/stream",
                                        {"publishDateTimeFrom": frm, "publishDateTimeTo": to})
    return await db.upsert_demand_fuelinst(pool, fuelinst_rows(api_rows))


async def _neso_resource(client, package_id: str, name: str) -> dict | None:
    pkg = await _get_json(client, f"{NESO_API}/package_show", {"id": package_id})
    return next((r for r in pkg["result"]["resources"] if str(r.get("name", "")).strip() == name), None)


async def _store_neso_historic(client, pool, chunk: Chunk) -> int:
    res = await _neso_resource(client, HISTORIC_DEMAND_PACKAGE_ID, f"Historic Demand Data {chunk.start.year}")
    if res is None:
        logger.info("NESO historic demand: no file for %s yet", chunk.start.year)
        return 0
    data = await _get_json(client, f"{NESO_API}/datastore_search", {"resource_id": res["id"], "limit": 32000})
    return await db.upsert_demand_neso_historic(pool, neso_historic_rows(data["result"]["records"]))


async def _store_embedded(client, pool, chunk: Chunk) -> int:
    res = await _neso_resource(client, EMBEDDED_PACKAGE_ID, f"Embedded Solar and Wind Forecast Archive {chunk.start.year}")
    if res is None:
        logger.info("embedded forecast archive: no file for %s", chunk.start.year)
        return 0
    reducer = EmbeddedReducer()
    block: list[str] = []
    # The year files are hundreds of MB: stream them and reduce block by block, never holding the file.
    async with client.stream("GET", res["url"], follow_redirects=True, timeout=httpx.Timeout(60.0, read=300.0)) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            block.append(line)
            if len(block) >= 100_000:
                reducer.feed(block)
                block = []
                await asyncio.sleep(0)
    reducer.feed(block)
    return await db.upsert_demand_embedded_vintages(pool, reducer.rows())


async def _store_pv_live(client, pool, chunk: Chunk) -> int:
    api_rows = await pvlive.fetch_pv_live(client, f"{chunk.start.isoformat()}T00:00", f"{chunk.end.isoformat()}T23:30")
    return await db.upsert_demand_pv_live(pool, pv_live_rows(api_rows))


async def _store_weather(client, pool, chunk: Chunk) -> int:
    point = chunk.dataset.split(":", 1)[1]
    lat, lon = WEATHER_POINTS[point]
    end = min(chunk.end, datetime.now(timezone.utc).date() - timedelta(days=1))
    if end < chunk.start:
        return 0
    data = await _get_json(client, OPEN_METEO, {"latitude": lat, "longitude": lon, "hourly": ",".join(WEATHER_VARS),
                                                "start_date": chunk.start.isoformat(), "end_date": end.isoformat(), "timezone": "UTC"})
    return await db.upsert_demand_weather(pool, weather_rows(point, data["hourly"]))


async def _store_calendar(client, pool, chunk: Chunk) -> int:
    return await db.upsert_demand_calendar(pool, calendar_rows(await _get_json(client, BANK_HOLIDAYS_URL)))


# ---------------------------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------------------------

Store = Callable[[httpx.AsyncClient, asyncpg.Pool, Chunk], Awaitable[int]]


def store_for(dataset: str) -> Store:
    if dataset in VINTAGE_DATASETS:
        return _store_vintages
    if dataset.startswith("weather:"):
        return _store_weather
    return {"outturn": _store_outturn, "fuelinst": _store_fuelinst, "neso_historic": _store_neso_historic,
            "embedded_forecast": _store_embedded, "pv_live": _store_pv_live, "calendar": _store_calendar}[dataset]


def resolve_datasets(names: str | Iterable[str]) -> list[str]:
    if isinstance(names, str):
        names = [n.strip() for n in names.split(",") if n.strip()]
    names = list(names)
    if not names or "all" in names:
        return list(ALL_DATASETS)
    unknown = [n for n in names if n not in ALL_DATASETS]
    if unknown:
        raise ValueError(f"unknown dataset(s) {unknown}; choose from {', '.join(ALL_DATASETS)} or 'all'")
    return names


async def run_backfill(pool: asyncpg.Pool, start: date, end: date, datasets: list[str], concurrency: int = 3,
                       force: bool = False, today: date | None = None) -> dict[str, dict[str, int]]:
    """Fetch and store every missing chunk. Returns {dataset: {"chunks", "skipped", "rows", "failed"}}."""
    today = today or datetime.now(timezone.utc).date()
    done = {} if force else await db.demand_backfill_done(pool)
    summary: dict[str, dict[str, int]] = {}
    work: list[Chunk] = []
    for ds in datasets:
        for chunk in plan_chunks(ds, start, end):
            row = summary.setdefault(chunk.dataset.split(":")[0], {"chunks": 0, "skipped": 0, "rows": 0, "failed": 0})
            if is_finished(chunk, done, today):
                row["skipped"] += 1
            else:
                work.append(chunk)
    total = len(work)
    logger.info("backfill %s .. %s: %d chunk(s) to fetch", start, end, total)
    sem = asyncio.Semaphore(concurrency)
    finished = 0

    async with httpx.AsyncClient(timeout=httpx.Timeout(40.0)) as client:
        async def one(chunk: Chunk) -> None:
            nonlocal finished
            family = summary[chunk.dataset.split(":")[0]]
            async with sem:
                try:
                    rows = await store_for(chunk.dataset)(client, pool, chunk)
                    await db.log_demand_backfill(pool, chunk.dataset, chunk.start, chunk.end, rows)
                    family["chunks"] += 1
                    family["rows"] += rows
                except Exception:  # noqa: BLE001 -- one bad chunk must not stop the run; it is retried next time
                    family["failed"] += 1
                    logger.warning("backfill: %s %s..%s failed", chunk.dataset, chunk.start, chunk.end, exc_info=True)
                finally:
                    finished += 1
                    if finished % 25 == 0 or finished == total:
                        logger.info("backfill progress: %d/%d chunks", finished, total)

        await asyncio.gather(*(one(c) for c in work))
    return summary
