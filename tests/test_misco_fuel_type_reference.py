"""Unit tests for citadel/ingest/misco_fuel_type_reference.py's
resolve_national_grid_bm_units() -- the fix for interconnector rows in the
misco CSV silently creating dead duplicate rows in bm_unit_reference instead
of correcting the real one (see that function's own docstring for the
confirmed live case, I_IEG-FRAN1).
"""

from __future__ import annotations

from citadel.ingest.misco_fuel_type_reference import preferred_national_grid_bm_unit_map, resolve_national_grid_bm_units
from citadel.storage.db import BmUnitReferenceRow


def _row(elexon_bm_unit: str, national_grid_bm_unit: str, fuel_type: str) -> BmUnitReferenceRow:
    return BmUnitReferenceRow(
        national_grid_bm_unit=national_grid_bm_unit, elexon_bm_unit=elexon_bm_unit,
        lead_party_name=None, bm_unit_type=None, generation_capacity_mw=None, fuel_type=fuel_type,
    )


def test_remaps_a_synthetic_key_onto_the_real_existing_national_grid_bm_unit():
    """The CSV's own interconnector row has no real national_grid_bm_unit,
    so it's synthesized as the elexon_bm_unit itself -- but a real row for
    that same unit already exists in the table under its own genuine key.
    """
    rows = [_row("I_IEG-FRAN1", "I_IEG-FRAN1", "INTFR")]
    existing = {"I_IEG-FRAN1": "IEG-FRAN1"}

    resolved = resolve_national_grid_bm_units(rows, existing)

    assert resolved[0].national_grid_bm_unit == "IEG-FRAN1"
    assert resolved[0].fuel_type == "INTFR"


def test_leaves_a_genuinely_new_unit_with_no_prior_row_untouched():
    rows = [_row("2__NEWUNIT1", "2__NEWUNIT1", "OTHER")]
    existing: dict[str, str] = {}

    resolved = resolve_national_grid_bm_units(rows, existing)

    assert resolved[0].national_grid_bm_unit == "2__NEWUNIT1"


def test_leaves_a_row_alone_when_its_key_already_matches_the_existing_one():
    rows = [_row("T_TEST-1", "TEST-1", "CCGT")]
    existing = {"T_TEST-1": "TEST-1"}

    resolved = resolve_national_grid_bm_units(rows, existing)

    assert resolved[0].national_grid_bm_unit == "TEST-1"


def test_preferred_map_picks_the_real_key_over_a_stale_synthetic_duplicate_regardless_of_scan_order():
    """A stale duplicate from a server restart that ran before this fix
    existed leaves two rows for the same elexon_bm_unit: the real one
    (national_grid_bm_unit != elexon_bm_unit) and the dead synthetic one
    (national_grid_bm_unit == elexon_bm_unit). The real one must win no
    matter which order `SELECT *` happens to return them in -- confirmed
    live this mattered: a naive last-write-wins dict picked whichever the
    database returned last, upserting onto the dead row about half the time.
    """
    real_first = [
        {"elexon_bm_unit": "I_IEG-FRAN1", "national_grid_bm_unit": "IEG-FRAN1"},
        {"elexon_bm_unit": "I_IEG-FRAN1", "national_grid_bm_unit": "I_IEG-FRAN1"},
    ]
    synthetic_first = list(reversed(real_first))

    assert preferred_national_grid_bm_unit_map(real_first) == {"I_IEG-FRAN1": "IEG-FRAN1"}
    assert preferred_national_grid_bm_unit_map(synthetic_first) == {"I_IEG-FRAN1": "IEG-FRAN1"}


def test_preferred_map_skips_rows_with_no_elexon_bm_unit():
    rows = [{"elexon_bm_unit": None, "national_grid_bm_unit": "SOMETHING"}]
    assert preferred_national_grid_bm_unit_map(rows) == {}
