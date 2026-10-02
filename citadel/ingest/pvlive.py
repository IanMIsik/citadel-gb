"""Sheffield Solar's PV_Live API -- public, no key required. Gives
national (GSP 0) solar generation outturn, used as engine/fundies.py's
real-time table `pv_live` column (the notebook's own `pv_df`).
"""

from __future__ import annotations

import httpx

BASE = "https://api.pvlive.uk/pvlive/api/v4/gsp/0"

# Positional column names for the response's `data` rows -- PV_Live returns
# an array-of-arrays, not an array-of-objects, so the field order below is
# load-bearing (confirmed against the notebook's own positional mapping).
COLUMNS = ["gsp_id", "start_time", "pv_live", "pv_lower_limit", "pv_upper_limit"]


async def fetch_pv_live(client: httpx.AsyncClient, start_iso: str, end_iso: str) -> list[dict]:
    """`start_iso`/`end_iso`: 'YYYY-MM-DDTHH:MM' bounds, same as the
    notebook's own `start`/`end` query params.
    """
    resp = await client.get(
        BASE,
        params={"start": start_iso, "end": end_iso, "extra_fields": "lcl_mw,ucl_mw"},
        timeout=20,
        headers={"Accept": "application/json"},
    )
    resp.raise_for_status()
    rows = resp.json().get("data", [])
    return [dict(zip(COLUMNS, row)) for row in rows]
