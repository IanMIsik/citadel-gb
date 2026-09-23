"""Unit tests for the ported FPN analytics engine (citadel/engine/fpn.py) --
covers the pieces the user specifically flagged as needing exact fidelity
to the original notebooks: the MEL/MIL adjusted-FPN clamp, the wind-FPN
period-average override (deliberately not a simplified "average"), the
worst-deviants threshold, and the niv_estimate decision-table arithmetic.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from citadel.engine.fpn import (
    _strip_pair_id_suffix,
    apply_wind_period_average,
    build_aggregated,
    build_decision_table,
    compute_generation_by_fuel,
    compute_worst_deviants,
    explode_and_merge,
    fuel_type_reference,
    pricing_stack_delta_by_fuel,
    pricing_stack_delta_by_fuel_5min,
    unit_sets,
)


def _bm_unit_reference(rows: list[tuple[str, str, str]]) -> pd.DataFrame:
    """rows: (national_grid_bm_unit, elexon_bm_unit, fuel_type)."""
    return pd.DataFrame(
        [{"national_grid_bm_unit": ngc, "elexon_bm_unit": elexon, "fuel_type": ft} for ngc, elexon, ft in rows]
    )


def _pn_row(bm_unit: str, ngc_unit: str, level: float, sd: str = "2026-01-01") -> dict:
    return {
        "bmUnit": bm_unit, "nationalGridBmUnit": ngc_unit, "timeFrom": f"{sd}T00:00:00Z", "timeTo": f"{sd}T00:01:00Z",
        "levelFrom": level, "levelTo": level, "dataset": "PN", "settlementDate": sd, "settlementPeriod": 1,
    }


def _mel_row(bm_unit: str, ngc_unit: str, level: float, sd: str = "2026-01-01") -> dict:
    return {
        "bmUnit": bm_unit, "nationalGridBmUnit": ngc_unit, "timeFrom": f"{sd}T00:00:00Z", "timeTo": f"{sd}T00:01:00Z",
        "levelFrom": level, "levelTo": level, "dataset": "MELS", "settlementDate": sd, "settlementPeriod": 1,
        "notificationTime": f"{sd}T00:00:00Z", "notificationSequence": 1,
    }


def _boalf_row(bm_unit: str, level: float, sd: str = "2026-01-01") -> dict:
    return {
        "bmUnit": bm_unit, "timeFrom": f"{sd}T00:00:00Z", "timeTo": f"{sd}T00:01:00Z",
        "levelFrom": level, "levelTo": level, "acceptanceTime": f"{sd}T00:00:00Z",
        "acceptanceNumber": 1, "settlementDate": sd, "settlementPeriodFrom": 1,
        "settlementPeriodTo": 1, "deemedBoFlag": False, "soFlag": False,
        "storFlag": False, "rrFlag": False,
    }


def test_adjusted_fpn_clamps_generation_direction_by_mel():
    """A thermal unit planning 100 MW (FPN) but with only 60 MW of MEL
    headroom must have its adjusted FPN clamped down to 60 -- the
    generation-direction half of the notebook's clamp arithmetic.
    """
    ref = _bm_unit_reference([("T_GEN-1", "T_GEN-1", "CCGT")])
    pn_df = pd.DataFrame([_pn_row("T_GEN-1", "T_GEN-1", 100.0)])
    mel_df = pd.DataFrame([_mel_row("T_GEN-1", "T_GEN-1", 60.0)])
    mil_df = pd.DataFrame(columns=pn_df.columns)
    boalf_df = pd.DataFrame(columns=["bmUnit", "timeFrom", "timeTo", "levelFrom", "levelTo", "acceptanceTime", "settlementDate", "settlementPeriodFrom", "settlementPeriodTo"])

    fuel_ref = fuel_type_reference(ref)
    sets = unit_sets(fuel_ref)
    result = explode_and_merge(pn_df, boalf_df, mel_df, mil_df, fuel_ref, sets)

    assert not result.empty
    assert result["adjusted_fpn"].iloc[0] == pytest.approx(60.0)
    assert result["mel_reduced_fpn"].iloc[0] == pytest.approx(60.0)


def test_adjusted_fpn_clamps_import_direction_by_mil():
    """A pumped-storage unit planning to import 100 MW (negative FPN) but
    with a MIL of only -60 MW must have its adjusted FPN clamped to -60 --
    the import-direction half of the clamp, which relies on the
    double-negation in the notebook's own formula resolving to a still-
    negative result (not flipping sign).
    """
    ref = _bm_unit_reference([("T_PS-1", "T_PS-1", "PS")])
    pn_df = pd.DataFrame([_pn_row("T_PS-1", "T_PS-1", -100.0)])
    mil_df = pd.DataFrame([_mel_row("T_PS-1", "T_PS-1", -60.0)])
    mel_df = pd.DataFrame(columns=pn_df.columns)
    boalf_df = pd.DataFrame(columns=["bmUnit", "timeFrom", "timeTo", "levelFrom", "levelTo", "acceptanceTime", "settlementDate", "settlementPeriodFrom", "settlementPeriodTo"])

    fuel_ref = fuel_type_reference(ref)
    sets = unit_sets(fuel_ref)
    result = explode_and_merge(pn_df, boalf_df, mel_df, mil_df, fuel_ref, sets)

    assert not result.empty
    assert result["adjusted_fpn"].iloc[0] == pytest.approx(-60.0)
    assert result["mil_increased_fpn"].iloc[0] == pytest.approx(-60.0)


def test_delta_is_zero_not_negative_fpn_when_unit_has_no_boalf_acceptance():
    """A unit with a real FPN (100 MW) but no active BOALF acceptance at
    all must have `delta == 0` (no BM action, no deviation from plan) --
    not `-100` (which is what treating "no acceptance" as "accepted volume
    is literally zero" computes). Regression test for a real bug: filling
    `boalf_spot_vol` to 0 *before* computing `delta` made every untouched
    unit look "curtailed to zero", inflating every fuel type's own summed
    delta (and so `market_gen`) by roughly its whole untouched fleet's own
    FPN -- confirmed against real CCGT figures (a correct ~664 MW fuel-type
    deviation from 3 genuinely active units was showing as roughly -14,000
    once the other ~78 idle-in-the-BM units were each wrongly counted).
    """
    ref = _bm_unit_reference([("T_GEN-1", "T_GEN-1", "CCGT")])
    pn_df = pd.DataFrame([_pn_row("T_GEN-1", "T_GEN-1", 100.0)])
    boalf_df = pd.DataFrame(columns=["bmUnit", "timeFrom", "timeTo", "levelFrom", "levelTo", "acceptanceTime", "settlementDate", "settlementPeriodFrom", "settlementPeriodTo"])
    mel_df = pd.DataFrame(columns=pn_df.columns)
    mil_df = pd.DataFrame(columns=pn_df.columns)

    fuel_ref = fuel_type_reference(ref)
    sets = unit_sets(fuel_ref)
    result = explode_and_merge(pn_df, boalf_df, mel_df, mil_df, fuel_ref, sets)

    assert not result.empty
    assert result["delta"].iloc[0] == pytest.approx(0.0)
    assert result["boalf_spot_vol"].iloc[0] == pytest.approx(0.0)


def test_delta_reflects_a_real_boalf_acceptance_against_fpn():
    """A unit accepted up to 80 MW against a 100 MW FPN has delta = -20
    (a real, partial reduction) -- confirms the fix above didn't also
    break the ordinary case where an acceptance genuinely exists.
    """
    ref = _bm_unit_reference([("T_GEN-1", "T_GEN-1", "CCGT")])
    pn_df = pd.DataFrame([_pn_row("T_GEN-1", "T_GEN-1", 100.0)])
    boalf_df = pd.DataFrame([_boalf_row("T_GEN-1", 80.0)])
    mel_df = pd.DataFrame(columns=pn_df.columns)
    mil_df = pd.DataFrame(columns=pn_df.columns)

    fuel_ref = fuel_type_reference(ref)
    sets = unit_sets(fuel_ref)
    result = explode_and_merge(pn_df, boalf_df, mel_df, mil_df, fuel_ref, sets)

    assert not result.empty
    assert result["delta"].iloc[0] == pytest.approx(-20.0)
    assert result["boalf_spot_vol"].iloc[0] == pytest.approx(80.0)


def test_non_ps_negative_fpn_rows_are_dropped():
    """Only Pumped Storage is allowed a negative (demand-direction) FPN --
    any other fuel type's negative PN row is filtered out before it ever
    reaches the explode step."""
    ref = _bm_unit_reference([("T_CCGT-1", "T_CCGT-1", "CCGT")])
    pn_df = pd.DataFrame([_pn_row("T_CCGT-1", "T_CCGT-1", -50.0)])
    boalf_df = pd.DataFrame(columns=["bmUnit", "timeFrom", "timeTo", "levelFrom", "levelTo", "acceptanceTime", "settlementDate", "settlementPeriodFrom", "settlementPeriodTo"])
    mel_df = pd.DataFrame(columns=pn_df.columns)
    mil_df = pd.DataFrame(columns=pn_df.columns)

    fuel_ref = fuel_type_reference(ref)
    sets = unit_sets(fuel_ref)
    result = explode_and_merge(pn_df, boalf_df, mel_df, mil_df, fuel_ref, sets)
    assert result.empty


def test_wind_fpn_is_flattened_to_period_average_not_kept_as_a_ramp():
    """Wind's per-minute linearly-interpolated ramp (0 -> 20 over two
    minutes, i.e. 5 and 15 at minute centres) must be overwritten with the
    unit's own period mean (10) for every minute -- not averaged away
    entirely, and not left as the original ramp.
    """
    wind_units = {"W_WIND-1"}
    exploded = pd.DataFrame([
        {"nationalGridBmUnit": "W_WIND-1", "bmUnit": "W_WIND-1", "settlementDate": pd.Timestamp("2026-01-01"), "settlementPeriod": 1, "spot_time": pd.Timestamp("2026-01-01T00:00:00Z"), "fpn_spot_vol": 5.0},
        {"nationalGridBmUnit": "W_WIND-1", "bmUnit": "W_WIND-1", "settlementDate": pd.Timestamp("2026-01-01"), "settlementPeriod": 1, "spot_time": pd.Timestamp("2026-01-01T00:01:00Z"), "fpn_spot_vol": 15.0},
        {"nationalGridBmUnit": "T_OTHER-1", "bmUnit": "T_OTHER-1", "settlementDate": pd.Timestamp("2026-01-01"), "settlementPeriod": 1, "spot_time": pd.Timestamp("2026-01-01T00:00:00Z"), "fpn_spot_vol": 5.0},
    ])
    out = apply_wind_period_average(exploded, wind_units)

    wind_out = out[out["nationalGridBmUnit"] == "W_WIND-1"]
    assert set(wind_out["fpn_spot_vol"]) == {10.0}
    other_out = out[out["nationalGridBmUnit"] == "T_OTHER-1"]
    assert other_out["fpn_spot_vol"].iloc[0] == pytest.approx(5.0)


def _worst_deviant_frame() -> pd.DataFrame:
    sd = pd.Timestamp("2026-01-01")
    rows = []
    for minute in range(3):
        rows.append({
            "bmUnit": "T_BIG_DEVIANT-1", "settlementDate": sd, "settlementPeriod": 1,
            "spot_time": pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(minutes=minute),
            "fpn_spot_vol": 100.0, "adjusted_fpn": 50.0, "delta": 0.0,
            "mel_spot_vol": 50.0, "mel_reduced_fpn": 50.0, "mil_spot_vol": 200.0, "mil_increased_fpn": 100.0, "FT": "CCGT",
        })
        rows.append({
            "bmUnit": "T_ON_PLAN-1", "settlementDate": sd, "settlementPeriod": 1,
            "spot_time": pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(minutes=minute),
            "fpn_spot_vol": 100.0, "adjusted_fpn": 99.0, "delta": 0.0,
            "mel_spot_vol": 150.0, "mel_reduced_fpn": 99.0, "mil_spot_vol": 200.0, "mil_increased_fpn": 100.0, "FT": "CCGT",
        })
    return pd.DataFrame(rows)


def test_worst_deviants_only_flags_units_past_the_threshold():
    by_unit = _worst_deviant_frame()
    now = pd.Timestamp("2026-01-01T00:02:30Z")  # within 2 minutes of the frame's last (minute-2) row
    result = compute_worst_deviants(by_unit, now=now, threshold_mw=20.0)

    assert set(result["bmUnit"]) == {"T_BIG_DEVIANT-1"}


def test_worst_deviants_mel_mil_downside_only_applies_to_future_minutes():
    by_unit = _worst_deviant_frame()
    now = pd.Timestamp("2026-01-01T00:01:30Z")  # between minute 1 (past) and minute 2 (future)
    result = compute_worst_deviants(by_unit, now=now, threshold_mw=20.0)

    past_row = result[result["spot_time"] < now].iloc[0]
    future_row = result[result["spot_time"] > now].iloc[0]
    assert past_row["mel_mil_downside"] == pytest.approx(0.0)
    assert future_row["mel_mil_downside"] != pytest.approx(0.0)


def test_niv_estimate_arithmetic():
    """auc_av - wind_deviation - other_gen_deviation + misco_indo_vs_ndf +
    unexp_delta, exactly as the notebook's own final `df` computes it.
    """
    group_cols = {"settlementDate": pd.Timestamp("2026-01-01"), "settlementPeriod": 1}
    aggregated = pd.DataFrame([{**group_cols, "auc_av": 100.0, "delta_av": 1.0, "misco_indo_vs_ndf": 10.0, "dmd_risk": 2.0, "niv_error": 5.0, "unexp_delta": 3.0}])
    by_fuel = pd.DataFrame([
        {**group_cols, "FT": "WIND", "market_gen_vs_adj_fpn": 20.0},
        {**group_cols, "FT": "CCGT", "market_gen_vs_adj_fpn": 7.0},
        {**group_cols, "FT": "NUCLEAR", "market_gen_vs_adj_fpn": 1000.0},  # excluded from other_gen_deviation
    ])

    decision = build_decision_table(by_fuel, aggregated)

    row = decision.iloc[0]
    assert row["wind_deviation"] == pytest.approx(20.0)
    assert row["other_gen_deviation"] == pytest.approx(7.0)
    expected = 100.0 - 20.0 - 7.0 + 10.0 + 3.0
    assert row["niv_estimate"] == pytest.approx(expected)


def test_strip_pair_id_suffix_removes_a_trailing_numeric_pairid():
    # stack.py's own convention: bmUnit + "_" + pairId (pairId can be negative).
    assert _strip_pair_id_suffix("T_DRAXX-1_3") == "T_DRAXX-1"
    assert _strip_pair_id_suffix("T_DRAXX-1_-2") == "T_DRAXX-1"


def test_strip_pair_id_suffix_leaves_a_bmunit_with_no_pairid_suffix_alone():
    # Elexon's own codes routinely contain underscores that aren't a pairId.
    assert _strip_pair_id_suffix("2__NSMAE001") == "2__NSMAE001"
    assert _strip_pair_id_suffix("V__LCEND001") == "V__LCEND001"


def test_pricing_stack_delta_by_fuel_strips_suffix_and_sums_by_fuel_type():
    """Also covers the MWh -> MW conversion: `pricing_stack_rows.delta` is
    in MWh (stack.py's own compute_stack divides its per-minute MW-level
    sum by 60), but every caller here needs an average-MW-over-the-period
    figure to combine with FUELINST's own instantaneous MW reading -- a
    30-minute settlement period means that's `* 2`, confirmed directly
    against real data (an unconverted MWh figure read as half the genuine
    real MW deviation).
    """
    ref = _bm_unit_reference([("T_DRAXX-1", "T_DRAXX-1", "BIOMASS"), ("T_OTHER-1", "T_OTHER-1", "CCGT")])
    fuel_ref = fuel_type_reference(ref)
    rows = [
        {"settlement_date": pd.Timestamp("2026-01-01").date(), "settlement_period": 1, "bm_unit": "T_DRAXX-1_1", "delta": 10.0},
        {"settlement_date": pd.Timestamp("2026-01-01").date(), "settlement_period": 1, "bm_unit": "T_DRAXX-1_-2", "delta": 5.0},
        {"settlement_date": pd.Timestamp("2026-01-01").date(), "settlement_period": 1, "bm_unit": "T_OTHER-1_1", "delta": -3.0},
    ]
    out = pricing_stack_delta_by_fuel(rows, fuel_ref)

    biomass_row = out[out["FT"] == "BIOMASS"].iloc[0]
    ccgt_row = out[out["FT"] == "CCGT"].iloc[0]
    assert biomass_row["pricing_stack_delta"] == pytest.approx(30.0)
    assert ccgt_row["pricing_stack_delta"] == pytest.approx(-6.0)


def test_pricing_stack_delta_by_fuel_5min_keeps_buckets_separate_and_needs_no_mw_conversion():
    """Unlike `pricing_stack_delta_by_fuel` above, the 5-minute source is
    already in MW (engine/stack.py's spot_time_bm_unit_delta_5min() sums
    per-minute delta directly, before build_price_stack()'s /60 MWh
    collapse) -- no `* 2` here -- and two buckets in the same settlement
    period must stay distinct rows, not get collapsed into one period total.
    """
    ref = _bm_unit_reference([("T_DRAXX-1", "T_DRAXX-1", "BIOMASS")])
    fuel_ref = fuel_type_reference(ref)
    rows = [
        {"bm_unit": "T_DRAXX-1_1", "start_time": "2026-01-01T00:00:00Z", "delta": 10.0},
        {"bm_unit": "T_DRAXX-1_-2", "start_time": "2026-01-01T00:00:00Z", "delta": 5.0},
        {"bm_unit": "T_DRAXX-1_1", "start_time": "2026-01-01T00:05:00Z", "delta": 40.0},
    ]
    out = pricing_stack_delta_by_fuel_5min(rows, fuel_ref)
    by_start = out.set_index("startTime")["pricing_stack_delta"]

    assert by_start[pd.Timestamp("2026-01-01T00:00:00Z")] == pytest.approx(15.0)
    assert by_start[pd.Timestamp("2026-01-01T00:05:00Z")] == pytest.approx(40.0)


def test_generation_by_fuel_uses_pricing_stack_delta_not_its_own_derived_one():
    """`market_gen`/`delta_gen` must come from the pricing stack's own real
    accepted-volume delta (`pricing_stack_delta_by_fuel_5min`, the per-5-
    minute-bucket sibling of the figure `blend_generation_and_smooth`'s
    "Market Gen vs Adjusted Fpn" table uses), not a second, independently
    re-derived figure -- confirmed against the user's own direct comparison
    of this table's CCGT deviation against the real pricing stack page
    (they disagreed; the pricing stack is the correct, real one). Unlike
    `pricing_stack_delta_by_fuel`'s own period-total figure, this source is
    already in MW (see `pricing_stack_delta_by_fuel_5min`'s own docstring),
    so no `* 2` conversion applies here -- `delta_gen` should equal the raw
    230 MW directly.
    """
    ref = _bm_unit_reference([("T_GEN-1", "T_GEN-1", "CCGT")])
    fuel_ref = fuel_type_reference(ref)
    fuelinst_records = [{"fuelType": "CCGT", "generation": 1000.0, "startTime": "2026-01-01T00:00:00Z"}]
    pricing_stack_unit_delta_5min_rows = [
        {"bm_unit": "T_GEN-1", "start_time": "2026-01-01T00:00:00Z", "delta": 230.0},
    ]

    result = compute_generation_by_fuel(fuelinst_records, pricing_stack_unit_delta_5min_rows, fuel_ref)

    assert not result.empty
    row = result.iloc[0]
    assert row["real_gen"] == pytest.approx(1000.0)
    assert row["delta_gen"] == pytest.approx(230.0)
    assert row["market_gen"] == pytest.approx(770.0)


def test_generation_by_fuel_varies_delta_gen_across_5min_buckets_within_one_period():
    """The whole point of switching to the 5-minute-bucketed source: two
    buckets in the same settlement period must show their own distinct
    `delta_gen`, not the same period-total value repeated.
    """
    ref = _bm_unit_reference([("T_GEN-1", "T_GEN-1", "CCGT")])
    fuel_ref = fuel_type_reference(ref)
    fuelinst_records = [
        {"fuelType": "CCGT", "generation": 1000.0, "startTime": "2026-01-01T00:00:00Z"},
        {"fuelType": "CCGT", "generation": 1000.0, "startTime": "2026-01-01T00:05:00Z"},
    ]
    pricing_stack_unit_delta_5min_rows = [
        {"bm_unit": "T_GEN-1", "start_time": "2026-01-01T00:00:00Z", "delta": 10.0},
        {"bm_unit": "T_GEN-1", "start_time": "2026-01-01T00:05:00Z", "delta": 40.0},
    ]

    result = compute_generation_by_fuel(fuelinst_records, pricing_stack_unit_delta_5min_rows, fuel_ref)

    by_ts = result.set_index("TS")["delta_gen"]
    assert by_ts[pd.Timestamp("2026-01-01T00:00:00Z")] == pytest.approx(10.0)
    assert by_ts[pd.Timestamp("2026-01-01T00:05:00Z")] == pytest.approx(40.0)


def _by_fuel_row(sd, sp, spot_time, ft="CCGT") -> dict:
    return {
        "settlementDate": sd, "settlementPeriod": sp, "spot_time": spot_time, "FT": ft,
        "fpn_spot_vol": 100.0, "fuelinst_generation": 100.0, "market_gen": 100.0,
        "mel_reduced_fpn": 100.0, "market_gen_vs_fpn": 0.0, "mel_mil_drop": 0.0,
        "market_gen_vs_adj_fpn": 0.0, "adjusted_fpn": 100.0,
    }


def _demand_row(sd, sp, spot_time) -> dict:
    return {"settlementDate": sd, "settlementPeriod": sp, "spot_time": spot_time, "spot_indo": 100.0, "spot_latest_ndf": 100.0, "spot_da_ndf": 100.0}


def test_build_aggregated_delta_comes_from_the_genuinely_per_minute_niv_spot_time():
    """`delta` (and everything downstream: `niv_error`, `delta_av`) must
    vary minute to minute with `niv_spot_time` -- the pricing stack's own
    real per-minute NIV trajectory (engine/stack.py's spot_time_niv()) --
    not a flat once-per-period broadcast of a settled period total. Two
    spot_times in the same settlement period get different
    niv_spot_time_max values here; `delta` must differ between them
    accordingly, and `delta_av` (the period average) must reflect both.
    """
    sd = pd.Timestamp("2026-01-01")
    t0 = pd.Timestamp("2026-01-01T00:00:00Z")
    t1 = pd.Timestamp("2026-01-01T00:01:00Z")
    by_fuel = pd.DataFrame([_by_fuel_row(sd, 1, t0), _by_fuel_row(sd, 1, t1)])
    demand = pd.DataFrame([_demand_row(sd, 1, t0), _demand_row(sd, 1, t1)])
    niv_spot_time = pd.DataFrame([
        {"settlementDate": sd, "settlementPeriod": 1, "spot_time": t0, "niv_spot_time_max": 10.0},
        {"settlementDate": sd, "settlementPeriod": 1, "spot_time": t1, "niv_spot_time_max": -4.0},
    ])

    aggregated = build_aggregated(by_fuel, demand, niv_spot_time=niv_spot_time)

    row0 = aggregated[aggregated["spot_time"] == t0].iloc[0]
    row1 = aggregated[aggregated["spot_time"] == t1].iloc[0]
    assert row0["delta"] == pytest.approx(10.0)
    assert row1["delta"] == pytest.approx(-4.0)
    assert row0["delta_av"] == pytest.approx(3.0)
    assert row1["delta_av"] == pytest.approx(3.0)


def test_build_aggregated_delta_defaults_to_zero_with_no_niv_spot_time_data():
    """A minute with no matching pricing-stack row (no accepted volume
    active) is a real, honest zero -- not missing/NaN.
    """
    sd = pd.Timestamp("2026-01-01")
    t0 = pd.Timestamp("2026-01-01T00:00:00Z")
    by_fuel = pd.DataFrame([_by_fuel_row(sd, 1, t0)])
    demand = pd.DataFrame([_demand_row(sd, 1, t0)])

    aggregated = build_aggregated(by_fuel, demand)

    assert aggregated.iloc[0]["delta"] == pytest.approx(0.0)
