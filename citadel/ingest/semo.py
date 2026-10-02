"""SEMO's public reports API -- no key required. Gives scheduled
cross-border flows for the three GB<->Ireland interconnectors (Moyle,
East-West, Greenlink), which ENTSO-E's own pair list doesn't cover (see
ingest/entsoe_flows.py's module docstring). Returns raw records, same
convention as ingest/elexon_rest.py -- net-flow arithmetic and
settlement-period derivation are engine/fundies.py's job.
"""

from __future__ import annotations

import httpx

BASE = "https://reports.sem-o.com/api/v1/dynamic/EA-012/total-scheduled-flows"

# The notebook queries both intraday auctions and lets IDA2 (later, more
# current) override IDA1 on any settlement period both cover -- see
# engine/fundies.py's build_day_ahead().
AUCTIONS = ("IDA1", "IDA2")


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
