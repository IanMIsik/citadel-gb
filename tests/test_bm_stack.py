"""Unit tests for engine/bm_stack.py's availability maths -- hand-derived
single-unit scenarios (FPN 100, MEL 300, MIL 0; offer bands 50 MW @ GBP80
then 100 MW @ GBP120; bid bands 40 MW @ GBP60 then 60 MW @ GBP40).
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from citadel.engine.bm_stack import compute_bm_stack

WINDOW = [(date(2026, 10, 2), 23)]


def _by_unit(fpn=100.0, mel=300.0, mil=0.0, delta=0.0, unit="T_TEST-1") -> pd.DataFrame:
    times = pd.to_datetime(["2026-10-02T10:00:00Z", "2026-10-02T10:05:00Z"])
    return pd.DataFrame({
        "bmUnit": unit,
        "settlementDate": pd.Timestamp("2026-10-02"),
        "settlementPeriod": 23,
        "spot_time": times,
        "fpn_spot_vol": fpn,
        "delta": delta,
        "mel_spot_vol": mel,
        "mel_reduced_fpn": np.minimum(mel, fpn),
        "mil_spot_vol": mil,
    })


def _bod(unit="T_TEST-1") -> pd.DataFrame:
    rows = [
        (1, 50.0, 80.0, 0.0), (2, 100.0, 120.0, 0.0),
        (-1, -40.0, 0.0, 60.0), (-2, -60.0, 0.0, 40.0),
    ]
    return pd.DataFrame([{
        "settlementDate": "2026-10-02", "settlementPeriod": 23, "bmUnit": unit, "nationalGridBmUnit": unit,
        "pairId": pid, "levelTo": lvl, "offer": offer, "bid": bid,
    } for pid, lvl, offer, bid in rows])


FUEL_REF = pd.DataFrame({"bmUnit": ["T_TEST-1"], "nationalGridBmUnit": ["TEST-1"], "FT": ["CCGT"]})
REFERENCE = pd.DataFrame({
    "elexon_bm_unit": ["T_TEST-1"], "lead_party_name": ["Test Power Ltd"], "generation_capacity_mw": [250.0],
})


def _run(by_unit, **kwargs) -> pd.DataFrame:
    return compute_bm_stack(by_unit, _bod(), FUEL_REF, REFERENCE, WINDOW, **kwargs)


def _mw(df: pd.DataFrame, side: str, price: float) -> float:
    row = df[(df["side"] == side) & (df["price"] == price)]
    return float(row["mw"].iloc[0]) if len(row) else 0.0


def test_untouched_unit_shows_each_band_from_where_the_previous_ends():
    df = _run(_by_unit())
    assert _mw(df, "offer", 80.0) == 50.0
    assert _mw(df, "offer", 120.0) == 100.0   # [50, 150] -- not 100 counted from FPN like the notebook did
    assert _mw(df, "bid", 60.0) == 40.0
    assert _mw(df, "bid", 40.0) == 60.0       # capped by down_total = FPN 100: [40, 100]


def test_accepted_offer_volume_is_no_longer_available():
    df = _run(_by_unit(delta=70.0))            # 70 MW of offer already accepted
    assert _mw(df, "offer", 80.0) == 0.0       # band 1 [0, 50] fully consumed
    assert _mw(df, "offer", 120.0) == 80.0     # band 2 [50, 150] minus the 20 MW already used
    assert _mw(df, "bid", 60.0) == 40.0        # bids untouched by an accepted offer


def test_accepted_bid_volume_is_no_longer_available():
    df = _run(_by_unit(delta=-50.0))           # 50 MW of bid already accepted
    assert _mw(df, "bid", 60.0) == 0.0
    assert _mw(df, "bid", 40.0) == 50.0        # [50, 100]
    assert _mw(df, "offer", 80.0) == 50.0


def test_mel_truncates_offer_capacity():
    df = _run(_by_unit(mel=130.0))             # only 30 MW above FPN is deliverable
    assert _mw(df, "offer", 80.0) == 30.0
    assert _mw(df, "offer", 120.0) == 0.0


def test_unit_with_no_mel_reading_is_not_shown():
    assert _run(_by_unit(mel=np.inf)).empty


def test_missing_mil_means_bids_run_down_to_zero_only():
    df = _run(_by_unit(mil=-np.inf))
    assert _mw(df, "bid", 40.0) == 60.0


def test_unit_context_and_fuel_are_attached():
    df = _run(_by_unit(delta=10.0))
    row = df[(df["side"] == "offer") & (df["price"] == 120.0)].iloc[0]
    assert row["fuel_type"] == "CCGT"
    assert row["lead_party"] == "Test Power Ltd"
    assert row["capacity_mw"] == 250.0
    assert row["fpn_mw"] == 100.0 and row["mel_mw"] == 300.0 and row["accepted_mw"] == 10.0
    assert row["settlement_date"] == "2026-10-02" and row["settlement_period"] == 23


def test_excluded_fuel_prefix_drops_those_units():
    fuel_ref = FUEL_REF.assign(FT="INTFR")
    df = compute_bm_stack(_by_unit(), _bod(), fuel_ref, REFERENCE, WINDOW, exclude_fuel_prefix="INT")
    assert df.empty


def test_empty_inputs_return_empty_frame():
    assert compute_bm_stack(pd.DataFrame(), _bod(), FUEL_REF, REFERENCE, WINDOW).empty
    assert compute_bm_stack(_by_unit(), pd.DataFrame(), FUEL_REF, REFERENCE, WINDOW).empty
