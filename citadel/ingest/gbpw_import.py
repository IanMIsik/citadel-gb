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
that project not installed), this falls back to a checked-in snapshot of
the same table (`data/gbpw_bmu_snapshot.csv`) so every machine, including
the AWS box, ends up with the same fuel-type mapping. Refresh the snapshot
with `python -m citadel.ingest.gbpw_import` on the machine that has the
database, then commit it.
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from ..storage.db import BmUnitReferenceRow

DEFAULT_GBPW_DB_PATH = Path(r"C:\Users\USER\Desktop\Mazao Consulting\Market Intelligence Tool\data\gbpw.db")
SNAPSHOT_PATH = Path(__file__).resolve().parents[1] / "data" / "gbpw_bmu_snapshot.csv"
_COLUMNS = ["national_grid_bm_unit", "elexon_bm_unit", "lead_party_name", "bm_unit_type",
            "generation_capacity_mw", "fuel_type"]


def read_snapshot(path: Path = SNAPSHOT_PATH) -> list[BmUnitReferenceRow]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return [
            BmUnitReferenceRow(
                national_grid_bm_unit=r["national_grid_bm_unit"],
                elexon_bm_unit=r["elexon_bm_unit"] or None,
                lead_party_name=r["lead_party_name"] or None,
                bm_unit_type=r["bm_unit_type"] or None,
                generation_capacity_mw=float(r["generation_capacity_mw"]) if r["generation_capacity_mw"] else None,
                fuel_type=r["fuel_type"],
            )
            for r in csv.DictReader(f)
            if r["fuel_type"]
        ]


def write_snapshot(rows: list[BmUnitReferenceRow], path: Path = SNAPSHOT_PATH) -> int:
    ordered = sorted(rows, key=lambda r: (r.national_grid_bm_unit, r.elexon_bm_unit or ""))
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(_COLUMNS)
        for r in ordered:
            w.writerow([r.national_grid_bm_unit, r.elexon_bm_unit or "", r.lead_party_name or "",
                        r.bm_unit_type or "",
                        "" if r.generation_capacity_mw is None else r.generation_capacity_mw,
                        r.fuel_type or ""])
    return len(ordered)


def read_bm_unit_reference(db_path: Path = DEFAULT_GBPW_DB_PATH,
                           snapshot_path: Path = SNAPSHOT_PATH) -> list[BmUnitReferenceRow]:
    if not db_path.exists():
        return read_snapshot(snapshot_path)
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


if __name__ == "__main__":
    n = write_snapshot(read_bm_unit_reference(snapshot_path=Path("/nonexistent")))
    print(f"wrote {n} rows to {SNAPSHOT_PATH}")
