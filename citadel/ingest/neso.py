"""NESO (National Energy System Operator) open data platform --
`datastore_search_sql` queries. Used for National Grid's own BM-adjacent
trades feed, which engine/fpn.py folds into `adjusted_fpn` as a synthetic
"NATGRID" fuel-type row (see the FPN notebook this ports, cell 12's
`natgrid_start` block). Returns raw records, same convention as
ingest/elexon_rest.py -- shaping is engine/fpn.py's job.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import datetime, timedelta, timezone

import httpx

from ..settlement import utc_to_settlement

logger = logging.getLogger(__name__)

BASE = "https://api.neso.energy/api/3/action/datastore_search_sql"
PACKAGE_SHOW = "https://api.neso.energy/api/3/action/package_show"

# NESO's "Upcoming Trades" resource (below) only ever holds trades that have
# not happened yet, so it is routinely EMPTY (confirmed 2026-10-02: zero
# rows all day) -- which left Fundies' natgrid row and FPN's NATGRID row
# blank. The trades themselves live in the "Historic GTMA Trades" package,
# one resource per financial year, as hourly (or longer) blocks:
# StartTime/EndTime/Volume/Price/Cost, where Volume is MW held across the
# block (Cost = Volume x Price x hours, and a 300 MW 09:00-10:00 block
# matches Elexon's own 150 MWh SP21 buy adjustment).
GTMA_PACKAGE_ID = "historic-gtma-grid-trade-master-agreement-trades-data"

# NESO dataset id for National Grid's own BM-adjacent trades -- confirmed
# against the FPN notebook's own query (cell 12).
NATIONAL_GRID_TRADES_DATASET_ID = "0b7e84ec-eded-4458-b111-d29ba4508e85"

# NESO *package* id for the embedded wind/solar generation forecast --
# the notebook hardcodes one specific resource's dated download filename
# (`202506032012_embedded_forecast.csv`), which 404s once NESO republishes
# the CSV under a new name. Resolving the package's current resource list
# first (see fetch_embedded_forecast()) avoids that staleness.
EMBEDDED_FORECAST_PACKAGE_ID = "91c0c70e-0ef5-4116-b6fa-7ad084b5e0e8"


async def fetch_national_grid_trades(client: httpx.AsyncClient, date_from_iso: str, date_to_iso: str) -> list[dict]:
    """Rows with `Date` in [date_from_iso, date_to_iso) -- pass plain
    'YYYY-MM-DDTHH:MM:SS' bounds, same as the notebook's own query.
    """
    dataset = NATIONAL_GRID_TRADES_DATASET_ID
    sql = (
        f'SELECT * FROM "{dataset}" '
        f'WHERE "{dataset}"."Date" >= \'{date_from_iso}\' AND "{dataset}"."Date" < \'{date_to_iso}\' '
        f'ORDER BY "_id" ASC'
    )
    resp = await client.get(BASE, params={"sql": sql}, timeout=20)
    resp.raise_for_status()
    upcoming = resp.json()["result"]["records"]
    # The GTMA history is best-effort on top of the upcoming list: if it
    # fails (or NESO renames it) the caller still gets whatever upcoming has.
    try:
        historic = await fetch_gtma_trades(client, date_from_iso, date_to_iso)
    except Exception:
        logger.warning("NESO historic GTMA trades fetch failed, using upcoming trades only", exc_info=True)
        historic = []
    covered = {(r["Date"][:10], int(r["SP"])) for r in historic}
    return historic + [r for r in upcoming if (str(r.get("Date"))[:10], int(r.get("SP", 0))) not in covered]


def gtma_blocks_to_sp_rows(blocks: list[dict]) -> list[dict]:
    """Expand GTMA block trades (StartTime/EndTime/Volume MW, UTC) into one
    {Date, SP, Volume} row per settlement period they cover, in the same
    shape and unit as the old upcoming-trades feed: Volume is MWh per SP
    (MW / 2), which engine/fundies.py's `* 2` and engine/fpn.py's `* -2`
    turn back into MW.
    """
    rows: list[dict] = []
    for b in blocks:
        try:
            start = datetime.fromisoformat(str(b["StartTime"])).replace(tzinfo=timezone.utc)
            end = datetime.fromisoformat(str(b["EndTime"])).replace(tzinfo=timezone.utc)
            mw = float(b["Volume"])
        except (KeyError, TypeError, ValueError):
            continue
        t = start
        while t < end:
            sd, sp = utc_to_settlement(t)
            rows.append({"Date": sd.isoformat(), "SP": sp, "Volume": mw / 2})
            t += timedelta(minutes=30)
    return rows


async def fetch_gtma_trades(client: httpx.AsyncClient, date_from_iso: str, date_to_iso: str) -> list[dict]:
    """Per-SP trade rows from the current and previous financial-year GTMA
    resources (the last two listed -- a fresh April resource can be empty at
    first). The query window starts a day early because a settlement day's
    first SPs sit in the previous UTC day during BST.
    """
    pkg = await client.get(PACKAGE_SHOW, params={"id": GTMA_PACKAGE_ID}, timeout=20)
    pkg.raise_for_status()
    resource_ids = [r["id"] for r in pkg.json()["result"]["resources"]][-2:]
    frm = (datetime.fromisoformat(date_from_iso) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
    blocks: list[dict] = []
    for rid in resource_ids:
        sql = (
            f'SELECT "StartTime", "EndTime", "Volume" FROM "{rid}" '
            f'WHERE "EndTime" > \'{frm}\' AND "StartTime" < \'{date_to_iso}\''
        )
        resp = await client.get(BASE, params={"sql": sql}, timeout=20)
        resp.raise_for_status()
        blocks += resp.json()["result"]["records"]
    return gtma_blocks_to_sp_rows(blocks)


async def fetch_embedded_forecast(client: httpx.AsyncClient) -> list[dict]:
    """NESO's embedded (distribution-connected, non-BM) wind/solar
    generation forecast CSV -- engine/fundies.py's day-ahead table source
    for `EMBEDDED_WIND_FORECAST`/`EMBEDDED_SOLAR_FORECAST`.

    Resolves the package's current CSV resource URL via `package_show`
    first rather than hardcoding a dated filename (the notebook's own
    `.../download/202506032012_embedded_forecast.csv` will 404 once NESO
    republishes this resource under a new name) -- picks the first CSV
    resource in the package, which is this dataset's only resource as of
    this writing.
    """
    resp = await client.get(PACKAGE_SHOW, params={"id": EMBEDDED_FORECAST_PACKAGE_ID}, timeout=20)
    resp.raise_for_status()
    resources = resp.json()["result"]["resources"]
    csv_resource = next(r for r in resources if r.get("format", "").upper() == "CSV")

    # NESO's own CSV download URL 302-redirects to the actual file storage
    # host -- confirmed live -- so this needs to follow it explicitly
    # rather than relying on the shared client's default (Citadel's
    # httpx.AsyncClient instances aren't created with follow_redirects=True).
    csv_resp = await client.get(csv_resource["url"], timeout=30, follow_redirects=True)
    csv_resp.raise_for_status()
    reader = csv.DictReader(io.StringIO(csv_resp.text))
    return list(reader)
