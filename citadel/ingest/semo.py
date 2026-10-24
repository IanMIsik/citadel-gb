"""SEMO's public reports API -- no key required. Gives scheduled
cross-border flows for the three GB<->Ireland interconnectors (Moyle,
East-West, Greenlink), which ENTSO-E's own pair list doesn't cover (see
ingest/entsoe_flows.py's module docstring). Returns raw records, same
convention as ingest/elexon_rest.py -- net-flow arithmetic and
settlement-period derivation are engine/fundies.py's job.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

BASE = "https://reports.sem-o.com/api/v1/dynamic/EA-012/total-scheduled-flows"

# The notebook queries both intraday auctions and lets IDA2 (later, more
# current) override IDA1 on any settlement period both cover -- see
# engine/fundies.py's build_day_ahead().
AUCTIONS = ("IDA1", "IDA2")


def query_window(now: datetime) -> tuple[str, str]:
    """(StartTime, EndTime) bounds, UTC, for one refresh: from two days back to the end of tomorrow.

    The API answers with whatever it has in the range, so asking for tomorrow early costs nothing and tomorrow appears the first
    time it is published. Two days back, not one, because in summer time a settlement day starts at 23:00 UTC the evening before:
    starting at yesterday's UTC midnight lost yesterday's SP1-SP2. Ending at today's UTC midnight before 15:00 (the old rule)
    meant today's own schedule -- published the day before -- was never fetched at all.
    """
    now = now.astimezone(timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (day - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S"), (day + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")


async def fetch_scheduled_flows(client: httpx.AsyncClient, auction: str, start_iso: str, end_iso: str) -> list[dict]:
    """`start_iso`/`end_iso`: 'YYYY-MM-DDTHH:MM:SS' bounds. `auction` must
    be one of AUCTIONS.
    """
    resp = await client.get(
        BASE,
        params={"StartTime": f">={start_iso}", "EndTime": f"<={end_iso}", "page_size": 5000, "Auction": auction},
        timeout=20,
        headers={"Accept": "application/json"},
    )
    resp.raise_for_status()
    return resp.json().get("items", [])
