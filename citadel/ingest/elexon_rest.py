"""Elexon BMRS REST fetchers -- public, no key required. Base:
https://data.elexon.co.uk/bmrs/api/v1

Async ports of the original notebook's fetch_boalf/fetch_pn/fetch_bod/
fetch_mel/fetch_mil/fetch_disbsad, used two ways here: as the sole ingest
path when IRIS isn't configured, and always for the reference/backfill
calls IRIS doesn't cover (bmu reference metadata, historical backtesting,
and the real settlement system price used for accuracy tracking).

Returns raw list[dict] (the API's own `data` array), not DataFrames --
building the working DataFrame is engine/stack.py's job, keeping this
module a thin, independently-testable HTTP layer.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

import httpx

BASE = "https://data.elexon.co.uk/bmrs/api/v1"
TIMEOUT = 20
RETRIES = 3
RETRY_BACKOFF_SECONDS = 1.5


async def _get(client: httpx.AsyncClient, url: str, params: dict) -> list[dict]:
    last_error: Exception | None = None
    for attempt in range(RETRIES):
        try:
            resp = await client.get(url, params=params, timeout=TIMEOUT, headers={"Accept": "application/json"})
            resp.raise_for_status()
            return resp.json().get("data", [])
        except httpx.HTTPError as e:
            last_error = e
            if attempt < RETRIES - 1:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise last_error  # type: ignore[misc]


async def _get_stream(client: httpx.AsyncClient, url: str, params: dict) -> list[dict]:
    """Same retry/backoff as `_get()`, but for a `/stream` endpoint variant
    -- Elexon's streaming endpoints return the row array directly (no
    `{"data": [...]}` envelope) and, per their own docs, carry no call-
    volume quota, unlike the plain dataset endpoint (confirmed live: MELS
    without `/stream` returned "403 Out of call volume quota" after
    moderate use this session; `/stream` did not).
    """
    last_error: Exception | None = None
    for attempt in range(RETRIES):
        try:
            resp = await client.get(url, params=params, timeout=TIMEOUT, headers={"Accept": "application/json"})
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            last_error = e
            if attempt < RETRIES - 1:
                await asyncio.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise last_error  # type: ignore[misc]


async def fetch_boalf(client: httpx.AsyncClient, sd: date, sp: int) -> list[dict]:
    return await _get(client, f"{BASE}/balancing/acceptances/all", {"settlementDate": sd.isoformat(), "settlementPeriod": sp})


async def fetch_bod(client: httpx.AsyncClient, sd: date, sp: int) -> list[dict]:
    return await _get(client, f"{BASE}/balancing/bid-offer/all", {"settlementDate": sd.isoformat(), "settlementPeriod": sp})


async def fetch_pn(client: httpx.AsyncClient, sd: date, sp: int) -> list[dict]:
    return await _get(client, f"{BASE}/datasets/PN", {"settlementDate": sd.isoformat(), "settlementPeriod": sp})


async def fetch_mels(client: httpx.AsyncClient, from_iso: str, to_iso: str) -> list[dict]:
    # /stream, not the plain dataset endpoint -- see _get_stream()'s own
    # docstring on why (quota exhaustion observed live, repeatedly, on
    # this exact endpoint).
    return await _get_stream(client, f"{BASE}/datasets/MELS/stream", {"from": from_iso, "to": to_iso})


async def fetch_mils(client: httpx.AsyncClient, from_iso: str, to_iso: str) -> list[dict]:
    return await _get_stream(client, f"{BASE}/datasets/MILS/stream", {"from": from_iso, "to": to_iso})


async def fetch_disbsad(client: httpx.AsyncClient, sd: date, sp: int) -> list[dict]:
    return await _get(client, f"{BASE}/balancing/nonbm/disbsad/details", {"settlementDate": sd.isoformat(), "settlementPeriod": sp})


async def fetch_fuelinst(client: httpx.AsyncClient, sd: date, sp: int) -> list[dict]:
    """Instantaneous generation by fuel type -- feeds engine/fpn.py's
    real-vs-market generation split (see the Fuelinst notebook this ports).
    """
    return await _get(client, f"{BASE}/datasets/FUELINST", {"settlementDateFrom": sd.isoformat(), "settlementDateTo": sd.isoformat(), "settlementPeriod": sp})


async def fetch_ndf(client: httpx.AsyncClient, from_iso: str, to_iso: str) -> list[dict]:
    """National Demand Forecast -- also used, with a different `from`/`to`
    window, as the day-ahead NDF for engine/fpn.py's `dmd_risk` (same
    endpoint, same shape, the notebook just queries it twice), and for
    engine/fundies.py's own wider yesterday/today/tomorrow window.

    Unlike every other dataset here, NDF's own API rejects a
    publishDateTimeFrom/To span over 1 day outright (confirmed live: "The
    date range between PublishDateTimeFrom and PublishDateTimeTo inclusive
    must not exceed 1 day", a 400). A request wider than that is
    transparently split into <=1-day chunks, fetched concurrently, and
    concatenated -- callers don't need to know about this quirk.
    """
    start = datetime.fromisoformat(from_iso.replace("Z", "+00:00"))
    end = datetime.fromisoformat(to_iso.replace("Z", "+00:00"))
    if end - start <= timedelta(days=1):
        return await _get(client, f"{BASE}/datasets/NDF", {"publishDateTimeFrom": from_iso, "publishDateTimeTo": to_iso})

    chunks: list[tuple[str, str]] = []
    chunk_start = start
    while chunk_start < end:
        chunk_end = min(chunk_start + timedelta(days=1), end)
        chunks.append((chunk_start.strftime("%Y-%m-%dT%H:%MZ"), chunk_end.strftime("%Y-%m-%dT%H:%MZ")))
        chunk_start = chunk_end
    results = await asyncio.gather(*(
        _get(client, f"{BASE}/datasets/NDF", {"publishDateTimeFrom": f, "publishDateTimeTo": t}) for f, t in chunks
    ), return_exceptions=True)
    rows: list[dict] = []
    for r in results:
        if not isinstance(r, BaseException):
            rows.extend(r)
    return rows


async def fetch_tsdf(client: httpx.AsyncClient, from_iso: str, to_iso: str) -> list[dict]:
    return await _get(client, f"{BASE}/datasets/TSDF", {"boundary": "N", "publishDateTimeFrom": from_iso, "publishDateTimeTo": to_iso})


async def fetch_indo(client: httpx.AsyncClient, from_iso: str, to_iso: str) -> list[dict]:
    return await _get(client, f"{BASE}/datasets/INDO", {"publishDateTimeFrom": from_iso, "publishDateTimeTo": to_iso})


async def fetch_itsdo(client: httpx.AsyncClient, from_iso: str, to_iso: str) -> list[dict]:
    return await _get(client, f"{BASE}/datasets/ITSDO", {"publishDateTimeFrom": from_iso, "publishDateTimeTo": to_iso})


async def fetch_bmu_reference(client: httpx.AsyncClient) -> list[dict]:
    """Every registered BM unit -- not date-scoped, call infrequently."""
    resp = await client.get(f"{BASE}/reference/bmunits/all", timeout=TIMEOUT, headers={"Accept": "application/json"})
    resp.raise_for_status()
    return resp.json()


async def fetch_system_price(client: httpx.AsyncClient, sd: date) -> list[dict]:
    """Elexon's own settlement system price for every period on `sd` --
    "latest available settlement run" per Elexon's docs (no run identifier
    in the payload). Used only for accuracy tracking against this
    project's own computed price, not fed into the stack computation.
    """
    return await _get(client, f"{BASE}/balancing/settlement/system-prices/{sd.isoformat()}", {})


async def fetch_windfor(client: httpx.AsyncClient, from_iso: str, to_iso: str) -> list[dict]:
    """Wind generation forecast -- published hourly (not half-hourly), so
    engine/fundies.py broadcasts each row onto both settlement periods of
    its hour (see broadcast_hourly_to_half_hourly()). Same endpoint, two
    different `from`/`to` windows, gives both the "latest" and "day-ahead"
    variants -- same pattern as fetch_ndf()'s own dual use.
    """
    return await _get(client, f"{BASE}/datasets/WINDFOR", {"publishDateTimeFrom": from_iso, "publishDateTimeTo": to_iso})


async def fetch_fuelhh(client: httpx.AsyncClient, sd_from: date, sd_to: date | None = None) -> list[dict]:
    """Half-hourly generation by fuel type (outturn, not instantaneous like
    FUELINST) -- engine/fundies.py filters this to fuelType==WIND for the
    real-time table's metered wind outturn, and to the interconnector fuel
    types for the real (metered) interconnector flows. `sd_to` defaults to
    `sd_from` (a single day); pass both to cover a date range in one call.
    """
    return await _get(client, f"{BASE}/datasets/FUELHH", {"settlementDateFrom": sd_from.isoformat(), "settlementDateTo": (sd_to or sd_from).isoformat()})


async def fetch_imbalngc(client: httpx.AsyncClient, from_iso: str, to_iso: str) -> list[dict]:
    """National imbalance volume -- engine/fundies.py sign-flips this
    (`imbalance * -1`) to match the notebook's own convention.
    """
    return await _get(client, f"{BASE}/datasets/IMBALNGC", {"boundary": "N", "publishDateTimeFrom": from_iso, "publishDateTimeTo": to_iso})


async def fetch_fou2t14d_nuclear(client: httpx.AsyncClient, publish_date: date) -> list[dict]:
    """Forecast Output Usable, 2-14 days ahead -- filtered server-side to
    NUCLEAR. engine/fundies.py picks the row whose own `forecastDate`
    matches the settlement date being displayed and takes its
    `outputUsable` as a single scalar applied to every period, same as the
    notebook's `nuke_214_gen`.
    """
    return await _get(client, f"{BASE}/datasets/FOU2T14D", {"fuelType": "NUCLEAR", "publishDate": publish_date.isoformat()})


async def fetch_bid_stack(client: httpx.AsyncClient, sd: date, sp: int) -> list[dict]:
    """The full accepted-bid stack for one settlement period -- used only
    for engine/fundies.py's wind-curtailment estimate (bids from wind BM
    units approximate curtailed volume). Same URL shape as the other
    per-(date, period) endpoints, just a different resource path.
    """
    return await _get(client, f"{BASE}/balancing/settlement/stack/all/bid/{sd.isoformat()}/{sp}", {})


async def fetch_market_index(client: httpx.AsyncClient, from_iso: str, to_iso: str) -> list[dict]:
    """Market Index Data (MID) -- multiple data providers (APXMIDP,
    N2EXMIDP) each report their own price/volume per settlement period;
    fed into engine.imbalance_price's Replacement Price fallback (the
    guide's own Market Price) as a volume-weighted blend across whichever
    providers reported positive volume for that period -- same endpoint
    and blending approach already verified live in the sibling gbpw
    project's ingest/elexon.py:fetch_day_ahead.
    """
    return await _get(client, f"{BASE}/balancing/pricing/market-index", {"from": from_iso, "to": to_iso})


def blend_market_index(rows: list[dict]) -> dict[tuple[date, int], float]:
    """Raw MID rows (any date range) -> {(settlement_date, settlement_period): blended_price}."""
    by_period: dict[tuple[date, int], list[dict]] = {}
    for r in rows:
        key = (date.fromisoformat(r["settlementDate"]), r["settlementPeriod"])
        by_period.setdefault(key, []).append(r)

    blended: dict[tuple[date, int], float] = {}
    for key, entries in by_period.items():
        priced = [e for e in entries if e["volume"] > 0]
        if not priced:
            continue
        total_vol = sum(e["volume"] for e in priced)
        blended[key] = sum(e["price"] * e["volume"] for e in priced) / total_vol
    return blended


class PeriodBundle:
    """Raw fetch results for one refresh cycle across a window of periods.
    `mel`/`mil` are `None` (not `[]`) when that dataset's fetch was skipped
    this cycle -- see `fetch_window()`'s own `fetch_mel`/`fetch_mil`
    params -- so the caller can tell "genuinely empty" apart from "didn't
    ask" and avoid wiping out a buffer IRIS is still feeding.
    """

    def __init__(self, boalf: list[dict], bod: list[dict], pn: list[dict], mel: list[dict] | None, mil: list[dict] | None, disbsad: list[dict]):
        self.boalf = boalf
        self.bod = bod
        self.pn = pn
        self.mel = mel
        self.mil = mil
        self.disbsad = disbsad


async def fetch_window(
    client: httpx.AsyncClient, periods: list[tuple[date, int]], mel_mil_ranges: list[tuple[str, str]],
    fetch_mel: bool = True, fetch_mil: bool = True,
) -> PeriodBundle:
    """Fans out BOALF/BOD/PN/DISBSAD across every (settlement_date, sp) in
    `periods`, and MEL/MIL across `mel_mil_ranges` (from/to ISO pairs), all
    concurrently -- the asyncio equivalent of the notebook's
    ThreadPoolExecutor fan-out, same shape, same 6-dataset call pattern.

    `fetch_mel`/`fetch_mil`: set False to skip that dataset's REST calls
    entirely this cycle (see engine/runner.py's Runner -- it does this when
    IRIS has delivered a fresh MELS/MILS message recently, so REST only
    does the periodic prune/resync, not every single cycle).
    """
    boalf_tasks = [fetch_boalf(client, sd, sp) for sd, sp in periods]
    bod_tasks = [fetch_bod(client, sd, sp) for sd, sp in periods]
    pn_tasks = [fetch_pn(client, sd, sp) for sd, sp in periods]
    disbsad_tasks = [fetch_disbsad(client, sd, sp) for sd, sp in periods]
    mel_tasks = [fetch_mels(client, f, t) for f, t in mel_mil_ranges] if fetch_mel else []
    mil_tasks = [fetch_mils(client, f, t) for f, t in mel_mil_ranges] if fetch_mil else []

    all_tasks = boalf_tasks + bod_tasks + pn_tasks + disbsad_tasks + mel_tasks + mil_tasks
    results = await asyncio.gather(*all_tasks, return_exceptions=True)

    def _flatten(chunks: list) -> list[dict]:
        out: list[dict] = []
        for c in chunks:
            if isinstance(c, BaseException):
                continue
            out.extend(c)
        return out

    n = len(periods)
    m = len(mel_mil_ranges)
    boalf = _flatten(results[0:n])
    bod = _flatten(results[n:2 * n])
    pn = _flatten(results[2 * n:3 * n])
    disbsad = _flatten(results[3 * n:4 * n])
    mel = _flatten(results[4 * n:4 * n + m]) if fetch_mel else None
    mil = _flatten(results[4 * n + m:4 * n + 2 * m]) if fetch_mil else None
    return PeriodBundle(boalf=boalf, bod=bod, pn=pn, mel=mel, mil=mil, disbsad=disbsad)


class FpnPeriodBundle:
    """Raw fetch results for one FPN-dashboard refresh cycle -- the extra
    datasets engine/fpn.py needs on top of what `fetch_window` already
    keeps current for the pricing stack.
    """

    def __init__(self, fuelinst: list[dict], ndf: list[dict], tsdf: list[dict], indo: list[dict], itsdo: list[dict], da_ndf: list[dict]):
        self.fuelinst = fuelinst
        self.ndf = ndf
        self.tsdf = tsdf
        self.indo = indo
        self.itsdo = itsdo
        self.da_ndf = da_ndf


async def fetch_fpn_window(
    client: httpx.AsyncClient, periods: list[tuple[date, int]], demand_range: tuple[str, str], da_ndf_range: tuple[str, str]
) -> FpnPeriodBundle:
    """FUELINST across every (settlement_date, sp) in `periods`, and the
    demand-forecast datasets across `demand_range` (from, to ISO pair) --
    plus NDF a second time over `da_ndf_range` for the day-ahead figure
    engine/fpn.py's `dmd_risk` needs -- all concurrently.
    """
    fuelinst_tasks = [fetch_fuelinst(client, sd, sp) for sd, sp in periods]
    demand_from, demand_to = demand_range
    da_from, da_to = da_ndf_range
    other_tasks = [
        fetch_ndf(client, demand_from, demand_to),
        fetch_tsdf(client, demand_from, demand_to),
        fetch_indo(client, demand_from, demand_to),
        fetch_itsdo(client, demand_from, demand_to),
        fetch_ndf(client, da_from, da_to),
    ]
    results = await asyncio.gather(*(fuelinst_tasks + other_tasks), return_exceptions=True)

    def _flatten(chunks: list) -> list[dict]:
        out: list[dict] = []
        for c in chunks:
            if isinstance(c, BaseException):
                continue
            out.extend(c)
        return out

    n = len(periods)
    fuelinst = _flatten(results[0:n])
    ndf, tsdf, indo, itsdo, da_ndf = (r if not isinstance(r, BaseException) else [] for r in results[n:n + 5])
    return FpnPeriodBundle(fuelinst=fuelinst, ndf=ndf, tsdf=tsdf, indo=indo, itsdo=itsdo, da_ndf=da_ndf)
