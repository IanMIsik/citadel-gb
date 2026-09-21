"""One-off import of BM unit fuel-type data from the sibling gbpw project
("Mazao Consulting/Market Intelligence Tool") -- a *different*, standalone
project (see this project's own architecture notes: Citadel doesn't run
alongside it or depend on it at runtime), but that project already solved
the fuel-type mapping problem this one would otherwise have to solve again:
Elexon's own live /reference/bmunits/all endpoint returns fuelType null for
~81% of units, so gbpw merges in NESO's manually-downloaded BM Unit Fuel
Type spreadsheet (REG FUEL TYPE column -- the only source with a real
WIND/BATTERY/etc. category for most units; see that project's own
ingest/bmu_fuel_types.py for the full merge logic). Rather than duplicate
that spreadsheet-parsing logic and require the user to feed it into two
places, this reads the *already-merged* result straight out of gbpw's own
SQLite database file.

This is a read of another project's local file, not a network call or a
shared dependency -- if that file isn't present (a different machine, or
that project not installed), this fails soft and Citadel just runs on
Elexon's live reference API alone (coarser fuel-type coverage, everything
else unaffected).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ..storage.db import BmUnitReferenceRow

DEFAULT_GBPW_DB_PATH = Path(r"C:\Users\USER\Desktop\Mazao Consulting\Market Intelligence Tool\data\gbpw.db")


def read_bm_unit_reference(db_path: Path = DEFAULT_GBPW_DB_PATH) -> list[BmUnitReferenceRow]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """
            SELECT national_grid_bm_unit, elexon_bm_unit, lead_party_name, bm_unit_type,
                   generation_capacity_mw, fuel_type
            FROM bm_unit_reference
            WHERE fuel_type IS NOT NULL
            """
        ).fetchall()
    finally:
        conn.close()
    return [
        BmUnitReferenceRow(
            national_grid_bm_unit=r[0], elexon_bm_unit=r[1], lead_party_name=r[2],
            bm_unit_type=r[3], generation_capacity_mw=r[4], fuel_type=r[5],
        )
        for r in rows
    ]
