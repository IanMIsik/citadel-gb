"""Unit tests for the ported FPN analytics engine (citadel/engine/fpn.py) --
covers the pieces the user specifically flagged as needing exact fidelity
to the original notebooks: the MEL/MIL adjusted-FPN clamp, the wind-FPN
period-average override (deliberately not a simplified "average"), the
worst-deviants threshold, and the niv_estimate decision-table arithmetic.
"""

from __future__ import annotations

import pandas as pd
import pytest

from citadel.engine.fpn import (
    apply_wind_period_average,
    build_decision_table,
    compute_worst_deviants,
    explode_and_merge,
    fuel_type_reference,
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
