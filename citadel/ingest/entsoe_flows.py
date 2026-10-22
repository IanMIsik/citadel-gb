"""ENTSO-E Transparency Platform -- interconnector scheduled cross-border
flows, replacing the old reference app's paid RNP feed (see Fundies.ipynb,
this dashboard's real, working reference pipeline). Needs a personal API
key (settings.entsoe_key) -- register free at
https://transparency.entsoe.eu/.

`entsoe-py`'s client is synchronous (built on `requests`), so every call
here runs via `asyncio.to_thread` instead of blocking the event loop --
the I/O-bound equivalent of how Citadel's pandas-heavy recomputes already
avoid blocking it the other way, via a shared ProcessPoolExecutor.
"""

from __future__ import annotations

import asyncio
import logging

import pandas as pd
from entsoe import EntsoePandasClient

logger = logging.getLogger("citadel.ingest.entsoe_flows")

LONDON = "Europe/London"

# Directed pairs the notebook queries -- IFA/IFA2/ElecLink are reported as
# their own ENTSO-E "bidding zones" even though they're physically GB<->FR
# interconnectors, not the FR zone itself. No Ireland pair here -- SEMO
# (ingest/semo.py) is the sole source for IE/NI flows, same split as the
# notebook's own PAIRS list + separate SEMO pipeline.
PAIRS: list[tuple[str, str]] = [
    ("GB_IFA2", "FR"),
    ("GB_IFA", "FR"),
    ("GB_ELECLINK", "FR"),
    ("GB", "BE"),
    ("GB", "NL"),
    ("GB", "NO"),
    ("GB", "DK_1"),
]


def get_entsoe_date_range(now: pd.Timestamp | None = None) -> list[pd.Timestamp]:
    """Yesterday, today and tomorrow, always. Tomorrow is asked for from the start of the day, not only after a
    cut-off hour: ENTSO-E answers with whatever it has for the range, so tomorrow's day-ahead schedules appear
    in the cache the first time they are published (typically early-to-mid afternoon) instead of waiting for a
    fixed clock time.
    """
    now = (now or pd.Timestamp.now(tz=LONDON)).tz_convert(LONDON)
    today = now.normalize()
    return [today - pd.Timedelta(days=1), today, today + pd.Timedelta(days=1)]


# ENTSO-E's per-host connection pool is small; a handful of requests at a time keeps it from thrashing.
_REQUEST_SLOTS = 8


async def _fetch_direction(
    client: EntsoePandasClient, from_country: str, to_country: str, start: pd.Timestamp, end: pd.Timestamp, dayahead: bool,
    slots: asyncio.Semaphore | None = None,
) -> pd.Series:
    # entsoe-py's @month_limited decorator (which splits the request into
    # per-month chunks under the hood) requires `start`/`end` as keyword
    # arguments -- confirmed live: passing them positionally raised
    # "missing 2 required keyword-only arguments: 'start' and 'end'"
    # despite the underlying method's own signature accepting them
    # positionally.
    def call():
        return client.query_scheduled_exchanges(from_country, to_country, start=start, end=end, dayahead=dayahead)

    if slots is None:
        return await asyncio.to_thread(call)
    async with slots:
        return await asyncio.to_thread(call)


async def fetch_pair_net_flow(
    client: EntsoePandasClient, from_country: str, to_country: str, start: pd.Timestamp, end: pd.Timestamp,
    dayahead: bool = False, slots: asyncio.Semaphore | None = None,
) -> pd.Series | None:
    """Net flow from `from_country` to `to_country` (positive = net export from `from_country`), UTC-indexed,
    for ONE of the two ENTSO-E schedules:

    - `dayahead=False`: the TOTAL commercial schedule (day-ahead plus every intraday revision), named
      f"{from_country}_to_{to_country}_net". It only covers the days that have intraday revisions so far
      (often just today), and is the more accurate figure wherever it exists.
    - `dayahead=True`: the day-ahead schedule, named f"da_{from_country}_to_{to_country}_net". It covers
      tomorrow as soon as it is published.

    The two are fetched independently (a failure in one never costs the other) and stored side by side;
    db.merge_scheduled_flows() puts the total over the day-ahead value period by period, so an intraday value
    replaces the day-ahead one the moment it comes through. A pair with no data for a schedule returns None
    for that schedule only (ENTSO-E commonly has gaps; Norway can lack the day's last hour).
    """
    name = f"{'da_' if dayahead else ''}{from_country}_to_{to_country}_net"
    try:
        flow_out, flow_in = await asyncio.gather(
            _fetch_direction(client, from_country, to_country, start, end, dayahead, slots),
            _fetch_direction(client, to_country, from_country, start, end, dayahead, slots),
        )
        net = flow_in.tz_convert("UTC") - flow_out.tz_convert("UTC")
        net.name = name
        return net
    except Exception:
        logger.warning("ENTSO-E %s fetch failed for %s", "day-ahead" if dayahead else "total-schedule", name, exc_info=True)
        return None


async def fetch_all_pairs(api_key: str, date_range: list[pd.Timestamp]) -> pd.DataFrame:
    """Two columns per PAIRS entry -- the total schedule (f"{from}_to_{to}_net") and the day-ahead one (the same
    name with a `da_` prefix) -- UTC-indexed (column `gmt_time`), net flow in MW, covering the whole span of
    `date_range` (its earliest entry through one day past its latest).

    Fetches each pair exactly ONCE across that whole span, not once per
    (day, pair) combination -- confirmed live: looping per day produced
    one same-named series PER DAY per pair (`fetch_pair_net_flow` names a
    series only after the pair, not the day), so `pd.concat(axis=1)` on a
    2-day range yielded two columns both called e.g. "uk_ifa2_net", each
    only covering its own day and NaN elsewhere. A later `.rename()` onto
    that ALREADY-duplicated frame renamed both at once, and any code that
    then read that column by name (`row.get("uk_ifa2_net")`) got whichever
    duplicate pandas happened to keep -- which silently made TODAY's real
    values disappear behind TOMORROW's same-named, mostly-NaN column.
    Empty DataFrame if every pair failed.
    """
    # Explicit timeout: the default (None) lets one stuck request hang for
    # good, which is how pairs went missing from a refresh (2026-10-02).
    client = EntsoePandasClient(api_key=api_key, timeout=45)
    start = min(date_range)
    end = max(date_range) + pd.Timedelta(days=1)
    slots = asyncio.Semaphore(_REQUEST_SLOTS)
    tasks = [fetch_pair_net_flow(client, from_c, to_c, start, end, dayahead, slots)
             for dayahead in (False, True) for from_c, to_c in PAIRS]

    results = await asyncio.gather(*tasks)
    series_list = [s for s in results if s is not None]
    if not series_list:
        return pd.DataFrame()

    df = pd.concat(series_list, axis=1)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.index.name = "gmt_time"
    return df.reset_index()
