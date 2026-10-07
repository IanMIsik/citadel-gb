from pathlib import Path

from citadel.ingest import gbpw_import as g
from citadel.storage.db import BmUnitReferenceRow


def test_snapshot_round_trip(tmp_path):
    rows = [
        BmUnitReferenceRow("B-1", "E_B1", 'Acme, "Ltd"', "E", 12.5, "NPSHYD"),
        BmUnitReferenceRow("A-1", None, None, None, None, "OTHER"),
    ]
    p = tmp_path / "snap.csv"
    assert g.write_snapshot(rows, p) == 2
    back = g.read_snapshot(p)
    assert back == sorted(rows, key=lambda r: r.national_grid_bm_unit)


def test_missing_database_falls_back_to_the_committed_snapshot():
    rows = g.read_bm_unit_reference(db_path=Path("/definitely/not/here.db"))
    fuels = {r.fuel_type for r in rows}
    assert len(rows) > 2000 and {"NPSHYD", "WIND", "BATTERIES", "OTHER"} <= fuels


def test_no_database_and_no_snapshot_is_still_empty(tmp_path):
    assert g.read_bm_unit_reference(db_path=tmp_path / "x.db", snapshot_path=tmp_path / "y.csv") == []
