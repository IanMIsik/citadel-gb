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
from datetime import date, datetime, timedelta, timezone

import httpx

from ..settlement import sp_end_utc, sp_start_utc, utc_to_settlement
from ..storage import db
from .natgrid_store import normalise_block

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


async def fetch_upcoming_trades(client: httpx.AsyncClient, date_from_iso: str, date_to_iso: str) -> list[dict]:
    """NESO's "Upcoming Trades" list: rows with `Date` in [date_from_iso, date_to_iso) --
    plain 'YYYY-MM-DDTHH:MM:SS' bounds, same as the notebook's own query."""
    dataset = NATIONAL_GRID_TRADES_DATASET_ID
    sql = (
        f'SELECT * FROM "{dataset}" '
        f'WHERE "{dataset}"."Date" >= \'{date_from_iso}\' AND "{dataset}"."Date" < \'{date_to_iso}\' '
        f'ORDER BY "_id" ASC'
    )
    resp = await client.get(BASE, params={"sql": sql}, timeout=20)
    resp.raise_for_status()
    return resp.json()["result"]["records"]


def combine_trades(historic: list[dict], upcoming: list[dict]) -> list[dict]:
    """GTMA rows win; upcoming-list rows only fill (date, SP)s the GTMA rows do not cover."""
    covered = {(r["Date"][:10], int(r["SP"])) for r in historic}
    return historic + [r for r in upcoming if (str(r.get("Date"))[:10], int(r.get("SP", 0))) not in covered]


async def fetch_national_grid_trades(client: httpx.AsyncClient, date_from_iso: str, date_to_iso: str) -> list[dict]:
    """Per-SP trade rows straight from NESO (nothing stored). The GTMA history is
    best-effort on top of the upcoming list: if it fails (or NESO renames it)
    the caller still gets whatever upcoming has."""
    upcoming = await fetch_upcoming_trades(client, date_from_iso, date_to_iso)
    try:
        historic = await fetch_gtma_trades(client, date_from_iso, date_to_iso)
    except Exception:
        logger.warning("NESO historic GTMA trades fetch failed, using upcoming trades only", exc_info=True)
        historic = []
    return combine_trades(historic, upcoming)


def upcoming_rows_to_blocks(rows: list[dict]) -> list[dict]:
    """NESO's "Upcoming Trades" list -> trade blocks in the same shape as the GTMA history, so
    both can live in one table.

    The upcoming list is per settlement period: a trade covering two periods is two rows with
    the same ID. `Volume` there is MWh for the period (verified: Cost = Volume x Price exactly,
    and a period's volumes sum to Elexon's DISBSAD volume for it), so MW = Volume x 2. Rows with
    one ID that are contiguous and the same size become ONE block that keeps the ID -- the
    nightly GTMA history later carries the same ID, and simply replaces this copy. Rows that
    cannot be one block (different sizes, or a gap) are kept as one block per period.
    """
    by_id: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("ID") and r.get("SP") is not None and r.get("Date") and r.get("Volume") is not None and r.get("Price") is not None:
            by_id.setdefault(str(r["ID"]), []).append(r)

    def block(block_id: str, group: list[dict]) -> dict:
        first, last = min(group, key=lambda r: int(r["SP"])), max(group, key=lambda r: int(r["SP"]))
        start = sp_start_utc(date.fromisoformat(str(first["Date"])[:10]), int(first["SP"]))
        end = sp_end_utc(date.fromisoformat(str(last["Date"])[:10]), int(last["SP"]))
        return {
            "ID": block_id, "StartTime": start.strftime("%Y-%m-%dT%H:%M:%S"), "EndTime": end.strftime("%Y-%m-%dT%H:%M:%S"),
            "Volume": float(first["Volume"]) * 2, "Price": first["Price"],
            "Cost": sum(float(x["Cost"]) for x in group if x.get("Cost") is not None) or None,
            "SO_Flag": first.get("SO_Flag"), "Reason": first.get("Reason"), "Last_Updated": max((x.get("Last_Updated") for x in group if x.get("Last_Updated")), default=None),
        }

    out: list[dict] = []
    for trade_id, group in by_id.items():
        sps = sorted(int(r["SP"]) for r in group)
        one_size = len({float(r["Volume"]) for r in group}) == 1 and len({str(r["Date"])[:10] for r in group}) == 1
        if one_size and sps == list(range(sps[0], sps[0] + len(sps))):
            out.append(block(trade_id, group))
        else:
            out.extend(block(f"{trade_id}@{r['Date'][:10]}-SP{r['SP']}", [r]) for r in group)
    return out


async def national_grid_trades_cached(pool, client: httpx.AsyncClient, date_from_iso: str, date_to_iso: str,
                                      store: bool = True) -> list[dict]:
    """Per-SP trade rows for the window, read from the local database. With `store`, a fresh
    fetch of BOTH NESO lists is saved first -- the GTMA history (updated nightly) and the
    upcoming list (the trades agreed since, up to the end of the day) -- new trades added,
    revised ones versioned in natgrid_trade_history. If a fetch fails the stored copy is used
    as it stands, so a NESO outage or a restart never blanks the trades. Without `store`
    (the FPN runner) it only reads; the Fundies runner keeps the database fresh every minute.
    """
    if store:
        try:
            rows = [r for r in map(normalise_block, await fetch_gtma_blocks(client, date_from_iso, date_to_iso)) if r]
            new, revised = await db.upsert_natgrid_trades(pool, rows)
            if new or revised:
                logger.info("NESO GTMA trades stored: %d new, %d revised", new, revised)
        except Exception:
            logger.warning("NESO GTMA trades fetch failed, using the stored trades", exc_info=True)
        try:
            rows = [r for r in map(normalise_block, upcoming_rows_to_blocks(await fetch_upcoming_trades(client, date_from_iso, date_to_iso))) if r]
            new, revised = await db.upsert_natgrid_trades(pool, rows)
            if new or revised:
                logger.info("NESO upcoming trades stored: %d new, %d revised", new, revised)
        except Exception:
            logger.warning("NESO upcoming trades fetch failed, using the stored trades", exc_info=True)
    start = datetime.fromisoformat(date_from_iso).replace(tzinfo=timezone.utc) - timedelta(days=1)
    end = datetime.fromisoformat(date_to_iso).replace(tzinfo=timezone.utc)
    return gtma_blocks_to_sp_rows(await db.natgrid_blocks_between(pool, start, end))


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
            # Volume stays MWh per SP (what ng_vol consumers expect); the
            # per-trade detail (MW, price, flag, reason, id) rides along for
            # the Natgrid ladder (engine/natgrid.py).
            rows.append({
                "Date": sd.isoformat(), "SP": sp, "Volume": mw / 2, "MW": mw, "Price": b.get("Price"),
                "SO_Flag": b.get("SO_Flag"), "Reason": b.get("Reason"), "ID": b.get("ID"), "Source": "NESO",
            })
            t += timedelta(minutes=30)
    return rows


async def fetch_gtma_trades(client: httpx.AsyncClient, date_from_iso: str, date_to_iso: str) -> list[dict]:
    return gtma_blocks_to_sp_rows(await fetch_gtma_blocks(client, date_from_iso, date_to_iso))


async def fetch_gtma_blocks(client: httpx.AsyncClient, date_from_iso: str, date_to_iso: str) -> list[dict]:
    """Raw trade blocks from the current and previous financial-year GTMA
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
            f'SELECT "ID", "StartTime", "EndTime", "Volume", "Price", "Cost", "SO_Flag", "Reason", "Last_Updated" FROM "{rid}" '
            f'WHERE "EndTime" > \'{frm}\' AND "StartTime" < \'{date_to_iso}\''
        )
        try:
            resp = await client.get(BASE, params={"sql": sql}, timeout=20)
            resp.raise_for_status()
            blocks += resp.json()["result"]["records"]
        except Exception:
            # One resource (a brand-new financial year, or one with a different
            # column set) failing must not lose the other's trades.
            logger.warning("NESO GTMA resource %s query failed, skipping it", rid, exc_info=True)
    return blocks


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
