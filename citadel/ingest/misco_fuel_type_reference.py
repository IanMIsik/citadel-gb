"""The user's own trusted BM unit fuel type references -- the exact same
two files the original FPN notebook used
(`misco_power_bm_units_updated_*.xlsx` for general units,
`misco_power_interconnector_bm_units_updated_*.csv` for interconnectors),
checked in as `data/misco_bmu_fuel_type.csv`. This is engine/fpn.py's sole
fuel-type source -- explicitly preferred over both the gbpw project's own
curated mapping and Elexon's official published BMU fuel type reference,
per direct user instruction after both of those turned out to have their
own per-unit accuracy problems.

The interconnector file has no `nationalGridBmUnit` column (only `unit`,
i.e. `elexon_bm_unit`, and `cable`). `bm_unit_reference` primary-keys on
`national_grid_bm_unit`, so interconnector rows here get `elexon_bm_unit`
copied into that column as a synthetic stand-in key -- not a real NESO id,
just enough to store the row. This is safe specifically because
engine/fpn.py's `unit_sets()`/`interconnector_rows()` key interconnectors
by `bmUnit`, never by `nationalGridBmUnit` -- see those functions' own
docstrings.

To refresh: re-export both source files, keep only
(nationalGridBmUnit|unit, bmUnit, FT), rename to
(national_grid_bm_unit, elexon_bm_unit, fuel_type), concatenate, then
`sort_values(["elexon_bm_unit", "fuel_type"]).drop_duplicates(subset=["elexon_bm_unit"], keep="first")`
-- both source files carry a handful of genuine conflicts (the same
bmUnit under two different fuel types), so this dedup step is required,
not optional.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pandas as pd

from ..storage.db import BmUnitReferenceRow

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "data" / "misco_bmu_fuel_type.csv"


def preferred_national_grid_bm_unit_map(existing_rows: list[dict]) -> dict[str, str]:
    """Collapses `bm_unit_reference`'s current rows to one
    national_grid_bm_unit per elexon_bm_unit, for
    resolve_national_grid_bm_units() below. Order-independent (`SELECT *`
    has no defined row order): when two rows already exist for the same
    elexon_bm_unit -- the stale-duplicate state this whole module exists to
    fix, e.g. from a server restart that ran before this fix existed -- the
    one whose national_grid_bm_unit differs from its own elexon_bm_unit wins
    over the synthetic-keyed one, regardless of which the database happens
    to return first. Confirmed necessary live: a naive last-write-wins dict
    comprehension picked the stale synthetic row about as often as the real
    one depending on scan order, silently upserting the correct fuel_type
    onto the dead row while leaving the real one (and the dashboard) wrong.
    """
    preferred: dict[str, str] = {}
    for row in existing_rows:
        elexon_bm_unit = row.get("elexon_bm_unit")
        if not elexon_bm_unit:
            continue
        national_grid_bm_unit = row.get("national_grid_bm_unit")
        if elexon_bm_unit not in preferred or preferred[elexon_bm_unit] == elexon_bm_unit:
            preferred[elexon_bm_unit] = national_grid_bm_unit
    return preferred


def resolve_national_grid_bm_units(
    rows: list[BmUnitReferenceRow], existing_by_elexon_bm_unit: dict[str, str]
) -> list[BmUnitReferenceRow]:
    """The interconnector file has no real `national_grid_bm_unit` (see this
    module's own docstring), so `read_bmu_fuel_types()` stands in with a
    synthetic one -- `elexon_bm_unit` itself -- just so the row has
    something to key on. But `bm_unit_reference` may already carry that same
    `elexon_bm_unit` under its REAL national_grid_bm_unit, from either
    Elexon's own live reference fetch or the gbpw import (both run earlier
    in api/app.py's lifespan) -- and `upsert_bm_unit_reference`'s `ON
    CONFLICT` is keyed on `national_grid_bm_unit` alone, so upserting under
    the synthetic key creates a second, dead row instead of correcting the
    real one's `fuel_type`. Confirmed live: every one of the 11 major
    interconnector "boundary" BM units (e.g. I_IEG-FRAN1) ended up
    duplicated this way -- the real row (keyed "IEG-FRAN1") still carrying
    the gbpw import's own wrong "OTHER" tag, sitting beside a dead second
    row (keyed "I_IEG-FRAN1") that actually has the correct "INTFR".

    `existing_by_elexon_bm_unit`: the current table's own elexon_bm_unit ->
    national_grid_bm_unit mapping (from `db.bm_unit_reference_all`, fetched
    right before this runs) -- remapping to that real key here means the
    upsert actually reaches the row that matters. A unit with no prior row
    at all (genuinely new) keeps its own (possibly synthetic) key -- there's
    nothing real to remap onto yet.
    """
    resolved = []
    for row in rows:
        real_ngc = existing_by_elexon_bm_unit.get(row.elexon_bm_unit)
        if real_ngc and real_ngc != row.national_grid_bm_unit:
            row = replace(row, national_grid_bm_unit=real_ngc)
        resolved.append(row)
    return resolved


def read_bmu_fuel_types(path: Path = DEFAULT_PATH) -> list[BmUnitReferenceRow]:
    if not path.exists():
        return []
    df = pd.read_csv(path)
    df["national_grid_bm_unit"] = df["national_grid_bm_unit"].fillna(df["elexon_bm_unit"])
    return [
        BmUnitReferenceRow(
            national_grid_bm_unit=r.national_grid_bm_unit,
            elexon_bm_unit=r.elexon_bm_unit,
            lead_party_name=None,
            bm_unit_type=None,
            generation_capacity_mw=None,
            fuel_type=r.fuel_type,
        )
        for r in df.itertuples()
    ]
