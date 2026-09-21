"""NESO (National Energy System Operator) open data platform --
`datastore_search_sql` queries. Used for National Grid's own BM-adjacent
trades feed, which engine/fpn.py folds into `adjusted_fpn` as a synthetic
"NATGRID" fuel-type row (see the FPN notebook this ports, cell 12's
`natgrid_start` block). Returns raw records, same convention as
ingest/elexon_rest.py -- shaping is engine/fpn.py's job.
"""

from __future__ import annotations

import httpx

BASE = "https://api.neso.energy/api/3/action/datastore_search_sql"

# NESO dataset id for National Grid's own BM-adjacent trades -- confirmed
# against the FPN notebook's own query (cell 12).
NATIONAL_GRID_TRADES_DATASET_ID = "0b7e84ec-eded-4458-b111-d29ba4508e85"


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
    return resp.json()["result"]["records"]
