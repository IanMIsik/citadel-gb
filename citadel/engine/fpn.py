"""FPN Analytics -- a faithful, cleaned port of
FPN_Analysis_Development_Continous.ipynb (adjusted FPN / worst deviants /
NIV-estimate decision table) and Fuelinst_Development_Continous.ipynb
(real-vs-market generation by fuel type), sharing this project's own
Postgres/engine instead of Google Sheets, and this project's own
`bm_unit_reference` instead of the notebooks' manually re-uploaded fuel
type CSVs.

Like engine/stack.py, this is deliberately literal about the arithmetic --
the goal is to preserve the original tool's own behaviour (including
quirks like the wind-FPN period-average override below) while replacing
everything *around* it. The one genuine integration this drops rather than
ports is the cross-notebook exchange via Google-Drive pickle files
(`spot_niv.pkl`, `small_fpn_boalf_disbsad_spot.pkl`): both pickles carried
the same thing -- Citadel's own per-unit, per-minute `delta` (accepted
volume minus FPN) -- which this module now computes directly as part of
its own pipeline (see `explode_and_merge`), so there's nothing left to
fetch from another process at all.

Elexon's raw PN/MEL/MIL/BOALF records carry both a `bmUnit` (Elexon's own
BM Unit code) and a `nationalGridBmUnit` field. `bm_unit_reference` (see
storage/schema.sql) keys on `national_grid_bm_unit` but also carries
`elexon_bm_unit` -- `fuel_type_reference()` below turns it into a small
(bmUnit, nationalGridBmUnit, FT) frame joinable against either raw field,
the same shape as the notebooks' own `bmu_fuel_types`/`int_bm_units` CSVs.
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import stack as stack_engine

# A unit's own "fuel type" in Elexon's reference data doubles as its
# interconnector name for interconnectors (INTFR, INTNED, INTELEC, ...) --
# confirmed against the notebooks' own column lists (Fuelinst notebook's
# interconnector_df columns are exactly these INT* codes). No separate
# interconnector reference is needed.
INTERCONNECTOR_FUEL_PREFIX = "INT"

# A BM unit whose MEL/MIL-adjusted FPN deviates from its raw FPN by more
# than this (MW, period-average) makes the worst-deviants table.
WORST_DEVIANT_THRESHOLD_MW = 20.0

# Fuel types whose market_gen_vs_fpn/market_gen_vs_adj_fpn get smoothed
# with a trailing 4-sample rolling mean before charting (ported as-is --
# these three are simply the notebook's own choice).
SMOOTHED_FUEL_TYPES = ("CCGT", "COAL", "PS")


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

def fuel_type_reference(bm_unit_reference: pd.DataFrame) -> pd.DataFrame:
    """`bm_unit_reference` rows (national_grid_bm_unit, elexon_bm_unit,
    fuel_type, ...) -> a (bmUnit, nationalGridBmUnit, FT) frame, dropping
    units missing either identifier or a fuel type -- the direct analogue
    of the notebooks' own `bmu_fuel_types` CSV.
    """
    ref = bm_unit_reference.dropna(subset=["elexon_bm_unit", "national_grid_bm_unit", "fuel_type"])
    return ref.rename(columns={"elexon_bm_unit": "bmUnit", "national_grid_bm_unit": "nationalGridBmUnit", "fuel_type": "FT"})[
        ["bmUnit", "nationalGridBmUnit", "FT"]
    ].drop_duplicates()


def unit_sets(fuel_ref: pd.DataFrame) -> dict[str, set[str]]:
    is_interconnector = fuel_ref["FT"].str.upper().str.startswith(INTERCONNECTOR_FUEL_PREFIX)
    generating = fuel_ref[~is_interconnector]
    return {
        "generating_units": set(generating["nationalGridBmUnit"]),
        "wind_units": set(generating.loc[generating["FT"] == "WIND", "nationalGridBmUnit"]),
        "interconnector_units": set(fuel_ref.loc[is_interconnector, "nationalGridBmUnit"]),
    }


# ---------------------------------------------------------------------------
# Pre-explosion filtering (notebook: right before its "explode everything
# once" step)
# ---------------------------------------------------------------------------

def filter_to_generating_units(pn_df: pd.DataFrame, mel_df: pd.DataFrame, mil_df: pd.DataFrame, fuel_ref: pd.DataFrame, sets: dict[str, set[str]]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """PN restricted to generating units (excludes interconnectors, which
    are handled separately -- see `interconnector_rows`), with every
    non-Pumped-Storage unit's negative (demand-direction) FPN rows dropped
    -- only PS legitimately has a negative FPN; any other fuel type's
    negative row is noise, not a real one. MEL/MIL filtered to the same
    unit set.
    """
    if pn_df.empty:
        return pn_df, mel_df, mil_df

    generating = sets["generating_units"]
    usable_pn = pn_df[pn_df["nationalGridBmUnit"].isin(generating)].copy()
    usable_pn = usable_pn.drop(columns=["dataset"], errors="ignore")

    with_ft = pd.merge(usable_pn, fuel_ref[["nationalGridBmUnit", "bmUnit", "FT"]], how="left", on=["nationalGridBmUnit", "bmUnit"])
    is_ps = with_ft["FT"] == "PS"
    non_negative = pd.to_numeric(with_ft["levelTo"]) >= 0
    usable_pn = pd.concat([with_ft[is_ps], with_ft[~is_ps & non_negative]]).drop(columns=["FT"])

    mel_filtered = mel_df[mel_df["nationalGridBmUnit"].isin(generating)].copy() if not mel_df.empty else mel_df
    mil_filtered = mil_df[mil_df["nationalGridBmUnit"].isin(generating)].copy() if not mil_df.empty else mil_df
    return usable_pn, mel_filtered, mil_filtered


# ---------------------------------------------------------------------------
# Exploders -- built on stack.vectorized_exploder, but (unlike
# stack.mel_exploder/fpn_exploder) keeping `nationalGridBmUnit` and
# `settlementDate`, which this module's joins need.
# ---------------------------------------------------------------------------

def _fpn_exploder(usable_pn: pd.DataFrame) -> pd.DataFrame:
    if usable_pn.empty:
        return pd.DataFrame()
    exploded = stack_engine.vectorized_exploder(usable_pn)
    if exploded.empty:
        return exploded
    exploded = exploded.drop(columns=["timeFrom", "timeTo", "dataset", "levelFrom", "levelTo"], errors="ignore")
    exploded = exploded.rename(columns={"spot_level": "fpn_spot_vol"})
    exploded["settlementPeriod"] = stack_engine._settlement_period_col(exploded["spot_time"])
    return exploded


def _mel_mil_exploder(df: pd.DataFrame, value_col: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    exploded = stack_engine.vectorized_exploder(df)
    if exploded.empty:
        return exploded
    exploded["notificationTime"] = pd.to_datetime(exploded["notificationTime"], utc=True)
    exploded = exploded.loc[exploded.groupby(["bmUnit", "spot_time"])["notificationTime"].idxmax()]
    cols_to_drop = ["timeFrom", "timeTo", "dataset", "levelFrom", "levelTo", "notificationTime", "notificationSequence"]
    exploded = exploded.drop(columns=[c for c in cols_to_drop if c in exploded.columns])
    exploded = exploded.rename(columns={"spot_level": value_col})
    exploded["settlementPeriod"] = stack_engine._settlement_period_col(exploded["spot_time"])
    return exploded


def apply_wind_period_average(exploded_fpn_df: pd.DataFrame, wind_units: set[str]) -> pd.DataFrame:
    """Wind's per-minute linear ramp between period-boundary declared
    levels doesn't represent anything physically real the way a thermal
    plant's genuine ramp does, so every WIND unit's `fpn_spot_vol` is
    flattened to its own mean for that (settlementDate, settlementPeriod)
    -- ported exactly as the notebook does it. Every other fuel type keeps
    the real per-minute ramp from `_fpn_exploder`. Must run before the
    MEL/MIL clamp.
    """
    if exploded_fpn_df.empty:
        return exploded_fpn_df
    wind_mask = exploded_fpn_df["nationalGridBmUnit"].isin(wind_units)
    wind_fpn = exploded_fpn_df[wind_mask]
    non_wind_fpn = exploded_fpn_df[~wind_mask]
    if wind_fpn.empty:
        return exploded_fpn_df

    wind_agg = wind_fpn.groupby(["settlementDate", "settlementPeriod", "nationalGridBmUnit"], as_index=False)["fpn_spot_vol"].mean()
    wind_fpn = wind_fpn.drop(columns="fpn_spot_vol").merge(wind_agg, on=["settlementDate", "settlementPeriod", "nationalGridBmUnit"])
    return pd.concat([wind_fpn, non_wind_fpn], ignore_index=True)


def explode_and_merge(pn_df: pd.DataFrame, boalf_df: pd.DataFrame, mel_df: pd.DataFrame, mil_df: pd.DataFrame, fuel_ref: pd.DataFrame, sets: dict[str, set[str]]) -> pd.DataFrame:
    """Per-(nationalGridBmUnit, bmUnit, spot_time) merge of exploded
    FPN/BOALF/MEL/MIL, plus the accepted-volume `delta` and the MEL/MIL-
    clamped `adjusted_fpn` -- the core of the notebook's main loop.
    """
    # Elexon's raw settlementDate is a plain date string; vectorized_exploder
    # never touches non-time columns, so it would otherwise survive as an
    # object dtype all the way through this module's own later merges
    # (interconnector_rows/natgrid_trade_rows), which convert their own
    # settlementDate to datetime64 -- normalising it here once avoids a
    # dtype-mismatch merge failure between the two.
    pn_df, mel_df, mil_df, boalf_df = pn_df.copy(), mel_df.copy(), mil_df.copy(), boalf_df.copy()
    for df in (pn_df, mel_df, mil_df, boalf_df):
        if not df.empty:
            df["settlementDate"] = pd.to_datetime(df["settlementDate"])

    usable_pn, mel_filtered, mil_filtered = filter_to_generating_units(pn_df, mel_df, mil_df, fuel_ref, sets)

    exploded_fpn = _fpn_exploder(usable_pn)
    exploded_boalf = stack_engine.boalf_exploder(boalf_df)
    exploded_mel = _mel_mil_exploder(mel_filtered, "mel_spot_vol")
    exploded_mil = _mel_mil_exploder(mil_filtered, "mil_spot_vol")

    if exploded_fpn.empty:
        return pd.DataFrame()

    exploded_fpn = apply_wind_period_average(exploded_fpn, sets["wind_units"])

    join_cols = ["nationalGridBmUnit", "bmUnit", "spot_time", "settlementDate", "settlementPeriod"]

    def _left_join(base: pd.DataFrame, other: pd.DataFrame, value_col: str) -> pd.DataFrame:
        if other.empty:
            base[value_col] = np.nan
            return base
        on_cols = [c for c in join_cols if c in other.columns]
        return base.merge(other[on_cols + [value_col]], on=on_cols, how="left")

    result = _left_join(exploded_fpn, exploded_boalf, "boalf_spot_vol")
    result = _left_join(result, exploded_mel, "mel_spot_vol")
    result = _left_join(result, exploded_mil, "mil_spot_vol")

    result["boalf_spot_vol"] = result["boalf_spot_vol"].fillna(0)
    result["delta"] = (result["boalf_spot_vol"] - result["fpn_spot_vol"]).fillna(0)

    # A momentary MEL/MIL data gap must not corrupt `adjusted_fpn` with a
    # NaN (min/max propagate it, and 0 * NaN is still NaN even where the
    # clamp arithmetic's own coefficient should zero that side out) --
    # treat a missing reading as "no constraint" rather than "zero
    # headroom", which a plain fillna(0) would wrongly imply.
    result["mel_spot_vol"] = result["mel_spot_vol"].fillna(np.inf)
    result["mil_spot_vol"] = result["mil_spot_vol"].fillna(-np.inf)

    result["mel_reduced_fpn"] = np.minimum(result["mel_spot_vol"], result["fpn_spot_vol"])
    result["mil_increased_fpn"] = np.maximum(result["mil_spot_vol"], result["fpn_spot_vol"])
    fpn_sign = np.sign(result["fpn_spot_vol"])
    result["adjusted_fpn"] = (
        np.maximum(fpn_sign, 0) * result["mel_reduced_fpn"] + np.minimum(fpn_sign, 0) * result["mil_increased_fpn"] * -1
    )
    return result


# ---------------------------------------------------------------------------
# Worst deviants
# ---------------------------------------------------------------------------

def compute_worst_deviants(fpn_mel_boalf_by_unit: pd.DataFrame, now: datetime | None = None, threshold_mw: float = WORST_DEVIANT_THRESHOLD_MW) -> pd.DataFrame:
    """A BM unit whose MEL/MIL-adjusted FPN deviates from its raw FPN by
    more than `threshold_mw` (period-average) is a "worst deviant" --
    ported exactly, including the MEL/MIL downside-risk figures, which are
    only shown looking forward (a unit's *past* MEL/MIL headroom is no
    longer actionable).
    """
    if fpn_mel_boalf_by_unit.empty:
        return pd.DataFrame()
    now = now or datetime.now(timezone.utc)

    period_means = fpn_mel_boalf_by_unit.groupby(["settlementDate", "settlementPeriod", "bmUnit"])[["fpn_spot_vol", "adjusted_fpn"]].mean().reset_index()
    period_means["mel_mil_drop"] = (period_means["adjusted_fpn"] - period_means["fpn_spot_vol"]).abs()
    deviant_units = period_means.loc[period_means["mel_mil_drop"] > threshold_mw, "bmUnit"].unique()
    if len(deviant_units) == 0:
        return pd.DataFrame()

    deviants = fpn_mel_boalf_by_unit[fpn_mel_boalf_by_unit["bmUnit"].isin(deviant_units)].copy()
    deviants["mel_mil_drop"] = (deviants["adjusted_fpn"] - deviants["fpn_spot_vol"]).abs() / 30

    recent_from = pd.Timestamp(now) - pd.Timedelta(minutes=2)
    recent_mask = (deviants["spot_time"] > recent_from) & (deviants["spot_time"] < pd.Timestamp(now))
    recent = deviants[recent_mask]
    current_mel = recent.groupby("bmUnit")["mel_spot_vol"].mean().rename("current_mel")
    current_mil = recent.groupby("bmUnit")["mil_spot_vol"].mean().rename("current_mil")
    deviants = deviants.merge(current_mel, on="bmUnit").merge(current_mil, on="bmUnit")

    deviants["mel_downside"] = np.minimum(deviants["current_mel"] - deviants["mel_reduced_fpn"], 0) / 30
    deviants["mil_upside"] = np.maximum(deviants["current_mil"] - deviants["mil_increased_fpn"], 0) / 30

    time_diff_minutes = (deviants["spot_time"] - pd.Timestamp(now)).dt.total_seconds() / 60
    future_check = np.maximum(np.sign(time_diff_minutes), 0)
    deviants["mel_downside"] = future_check * deviants["mel_downside"]
    deviants["mil_upside"] = future_check * deviants["mil_upside"]
    deviants["mel_mil_downside"] = deviants["mel_downside"] + deviants["mil_upside"]
    return deviants


# ---------------------------------------------------------------------------
# Interconnectors and NATGRID trades (synthetic fuel-type rows)
# ---------------------------------------------------------------------------

def interconnector_rows(pn_df: pd.DataFrame, fuel_ref: pd.DataFrame, sets: dict[str, set[str]], spot_times: pd.DataFrame) -> pd.DataFrame:
    """Interconnectors are treated as always meeting their FPN exactly
    (the notebook's own simplifying assumption -- real interconnector
    failure isn't modelled), aggregated straight from PN without exploding
    a ramp: one `fpn_spot_vol` per (settlementDate, settlementPeriod, FT),
    aligned onto the shared spot-time grid via `spot_times`
    (settlementDate, settlementPeriod, spot_time).
    """
    if pn_df.empty or not sets["interconnector_units"]:
        return pd.DataFrame()
    int_pn = pn_df[pn_df["nationalGridBmUnit"].isin(sets["interconnector_units"])].copy()
    if int_pn.empty:
        return pd.DataFrame()
    int_pn = pd.merge(int_pn, fuel_ref, on=["nationalGridBmUnit", "bmUnit"], how="left")
    int_pn["settlementDate"] = pd.to_datetime(int_pn["settlementDate"])
    grouped = int_pn.groupby(["settlementDate", "settlementPeriod", "FT"])["levelTo"].sum().reset_index()
    grouped = grouped.rename(columns={"levelTo": "fpn_spot_vol"})

    rows = pd.merge(grouped, spot_times, on=["settlementDate", "settlementPeriod"], how="right")
    rows["delta"] = 0.0
    rows["mel_spot_vol"] = 0.0
    rows["mil_spot_vol"] = 0.0
    # Interconnectors are assumed never to fail their FPN -- see docstring.
    rows["mel_reduced_fpn"] = rows["fpn_spot_vol"]
    rows["mil_increased_fpn"] = rows["fpn_spot_vol"]
    rows["adjusted_fpn"] = rows["fpn_spot_vol"]
    return rows


def natgrid_trade_rows(neso_trades: list[dict], disbsad_df: pd.DataFrame, spot_times: pd.DataFrame) -> pd.DataFrame:
    """National Grid's own BM-adjacent trades, as a synthetic "NATGRID"
    fuel-type row -- ported from the notebook's own `natgrid_trades` block,
    combining the NESO feed with a DISBSAD-derived fallback for any
    (settlementDate, settlementPeriod) the NESO feed doesn't cover.
    """
    ng_df = pd.DataFrame(neso_trades)
    if not ng_df.empty and {"SP", "Date", "Volume"}.issubset(ng_df.columns):
        ng_df = ng_df.rename(columns={"SP": "settlementPeriod", "Date": "settlementDate"})
        ng_df["settlementDate"] = pd.to_datetime(ng_df["settlementDate"]).dt.strftime("%Y-%m-%d")
        ng_df = ng_df.groupby(["settlementPeriod", "settlementDate"])["Volume"].sum().reset_index()
        ng_df = ng_df.rename(columns={"Volume": "ng_vol"})
    else:
        ng_df = pd.DataFrame(columns=["settlementPeriod", "settlementDate", "ng_vol"])

    disbsad_fallback = pd.DataFrame(columns=["settlementPeriod", "settlementDate", "ng_vol"])
    if not disbsad_df.empty and "volume" in disbsad_df.columns:
        disbsad_fallback = disbsad_df.groupby(["settlementPeriod", "settlementDate"])["volume"].sum().reset_index()
        disbsad_fallback = disbsad_fallback.rename(columns={"volume": "ng_vol"})

    if not ng_df.empty:
        covered = set(ng_df[["settlementDate", "settlementPeriod"]].itertuples(index=False, name=None))
        disbsad_fallback = disbsad_fallback[~disbsad_fallback[["settlementDate", "settlementPeriod"]].apply(tuple, axis=1).isin(covered)]
    combined = pd.concat([ng_df, disbsad_fallback], ignore_index=True)
    if combined.empty:
        return pd.DataFrame()
    combined["settlementDate"] = pd.to_datetime(combined["settlementDate"])

    rows = pd.merge(combined, spot_times, on=["settlementDate", "settlementPeriod"], how="right")
    rows["ng_vol"] = rows["ng_vol"].fillna(0) * -2
    rows = rows.rename(columns={"ng_vol": "fpn_spot_vol"})
    rows["FT"] = "NATGRID"
    rows["delta"] = 0.0
    rows["mel_reduced_fpn"] = rows["fpn_spot_vol"]
    rows["mil_increased_fpn"] = rows["fpn_spot_vol"]
    rows["adjusted_fpn"] = rows["fpn_spot_vol"]
    rows = rows.fillna(0)
    return rows


# ---------------------------------------------------------------------------
# By-fuel-type aggregation + smoothing
# ---------------------------------------------------------------------------

def aggregate_by_fuel(merged: pd.DataFrame, fuel_ref: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """`merged` (per-unit, per-minute, from `explode_and_merge`) -> (the
    per-unit table with fuel type attached, ready for `compute_worst_deviants`,
    and the summed-by-fuel-type table). Fuel type is joined on `bmUnit`
    alone here (matches the notebook: its own equivalent subset had already
    dropped `nationalGridBmUnit` by this point).
    """
    by_unit_cols = ["bmUnit", "settlementDate", "settlementPeriod", "spot_time", "fpn_spot_vol", "delta", "mel_spot_vol", "mel_reduced_fpn", "mil_spot_vol", "mil_increased_fpn", "adjusted_fpn"]
    by_unit = merged[by_unit_cols].drop_duplicates()
    by_unit = pd.merge(by_unit, fuel_ref[["bmUnit", "FT"]].drop_duplicates(), how="left", on="bmUnit")

    sum_cols = ["fpn_spot_vol", "delta", "mel_spot_vol", "mel_reduced_fpn", "mil_spot_vol", "mil_increased_fpn", "adjusted_fpn"]
    by_fuel = by_unit.groupby(["settlementDate", "settlementPeriod", "spot_time", "FT"])[sum_cols].sum().reset_index()
    return by_unit, by_fuel


def _smooth_selected_fuel_types(by_fuel: pd.DataFrame, cols: list[str], fuel_types: tuple[str, ...] = SMOOTHED_FUEL_TYPES, window: int = 4) -> pd.DataFrame:
    smoothed_parts = [by_fuel[~by_fuel["FT"].isin(fuel_types)]]
    for ft in fuel_types:
        part = by_fuel[by_fuel["FT"] == ft].sort_values("spot_time").copy()
        for col in cols:
            part[col] = part[col].rolling(window).mean()
        smoothed_parts.append(part)
    return pd.concat(smoothed_parts, ignore_index=True)


def blend_generation_and_smooth(by_fuel: pd.DataFrame, fuelinst_df: pd.DataFrame, interconnector_rows_df: pd.DataFrame, natgrid_rows_df: pd.DataFrame) -> pd.DataFrame:
    """Rounds each fuel-type row's `spot_time` onto FUELINST's own 5-minute
    grid, blends in real generation (`market_gen`/`market_gen_vs_fpn`/
    `market_gen_vs_adj_fpn`/`mel_mil_drop`), smooths the three trickiest
    fuel types, then appends the interconnector and NATGRID synthetic rows
    (which are *not* smoothed or FUELINST-blended -- ported as-is, same
    order the notebook does it in).
    """
    by_fuel = by_fuel.copy()
    by_fuel["startTime"] = (by_fuel["spot_time"] - pd.Timedelta(seconds=1.51 * 60)).dt.round("5min")

    if not interconnector_rows_df.empty:
        by_fuel = pd.concat([by_fuel, interconnector_rows_df.drop(columns=["startTime"], errors="ignore")], ignore_index=True)

    if not fuelinst_df.empty:
        fuelinst = fuelinst_df.rename(columns={"fuelType": "FT", "generation": "fuelinst_generation"})
        fuelinst = fuelinst[["FT", "settlementDate", "settlementPeriod", "fuelinst_generation", "startTime"]].copy()
        fuelinst["settlementDate"] = pd.to_datetime(fuelinst["settlementDate"])
        fuelinst["startTime"] = pd.to_datetime(fuelinst["startTime"], utc=True).dt.round("5min")
        by_fuel["FT"] = by_fuel["FT"].replace({"ELECLINK": "INTELEC"})
        by_fuel["settlementDate"] = pd.to_datetime(by_fuel["settlementDate"])
        by_fuel = pd.merge(by_fuel, fuelinst, how="left")
        by_fuel["market_gen"] = by_fuel["fuelinst_generation"] - by_fuel["delta"]
        by_fuel["market_gen_vs_fpn"] = by_fuel["market_gen"] - by_fuel["fpn_spot_vol"]
        by_fuel["market_gen_vs_adj_fpn"] = by_fuel["market_gen"] - by_fuel["adjusted_fpn"]
        by_fuel["mel_mil_drop"] = by_fuel["adjusted_fpn"] - by_fuel["fpn_spot_vol"]
        by_fuel = _smooth_selected_fuel_types(by_fuel, ["market_gen_vs_fpn", "market_gen_vs_adj_fpn"])

    if not natgrid_rows_df.empty:
        by_fuel = pd.concat([by_fuel, natgrid_rows_df], ignore_index=True)
    return by_fuel


# ---------------------------------------------------------------------------
# Demand side
# ---------------------------------------------------------------------------

def _create_minute_dataframe(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    total_minutes = int((end - start).total_seconds() / 60)
    timestamps = pd.date_range(start=start, periods=max(total_minutes, 0), freq="min")
    return pd.DataFrame({"minute_ta": timestamps})


def build_spot_time_grids(start: pd.Timestamp, end: pd.Timestamp) -> tuple[pd.DataFrame, pd.DataFrame]:
    """`demand_spot_times` (settlementDate/settlementPeriod/spot_time, one
    per minute) and `mid_spot_times` (each minute's distance from its own
    settlement period's midpoint) -- the two grids `sp_time_interpolator`
    needs to turn a half-hourly demand forecast into a per-minute series.
    """
    minutes = _create_minute_dataframe(start, end)
    demand_spot_times = minutes.rename(columns={"minute_ta": "spot_time"})
    spot_time_utc = demand_spot_times["spot_time"]
    if spot_time_utc.dt.tz is None:
        spot_time_utc = spot_time_utc.dt.tz_localize("UTC")
    demand_spot_times["settlementDate"] = pd.to_datetime(demand_spot_times["spot_time"].dt.date)
    demand_spot_times["settlementPeriod"] = stack_engine._settlement_period_col(spot_time_utc)

    mid_spot_times = demand_spot_times[["spot_time"]].copy()
    mid_spot_times["mid_sp_time"] = mid_spot_times["spot_time"].dt.round("30min") + pd.Timedelta(seconds=15 * 60)
    mid_spot_times["minutes_from_mid"] = (mid_spot_times["mid_sp_time"] - mid_spot_times["spot_time"]).dt.seconds / 60
    return demand_spot_times, mid_spot_times


def sp_time_interpolator(df: pd.DataFrame, variable_col_name: str, spot_times: pd.DataFrame, mid_spot_times: pd.DataFrame) -> pd.DataFrame:
    """Turns a half-hourly forecast series (one value per settlement
    period) into a per-minute series by linearly interpolating between
    each period's own midpoint value and the *previous* period's midpoint
    value -- ported exactly (notebook cell 9).
    """
    temp = pd.merge(df, spot_times, on=["settlementDate", "settlementPeriod"])
    period_start = temp.groupby(["settlementDate", "settlementPeriod"])["spot_time"].min().reset_index()
    temp2 = pd.merge(temp, period_start, on=["settlementDate", "settlementPeriod", "spot_time"])
    temp2 = temp2.rename(columns={variable_col_name: "mid_sp_value", "spot_time": "sp_start"})

    previous_period = temp2[["sp_start", "mid_sp_value"]].copy()
    previous_period["sp_start"] = previous_period["sp_start"] + pd.Timedelta(seconds=0.5 * 3600)
    previous_period = previous_period.rename(columns={"mid_sp_value": "mid_sp_value_sub1"})

    temp3 = pd.merge(temp2, previous_period, how="left", on="sp_start")
    temp3["mw_delta"] = temp3["mid_sp_value"] - temp3["mid_sp_value_sub1"]
    temp3["mid_sp_time"] = temp3["sp_start"] + pd.Timedelta(seconds=0.25 * 3600)

    # Joins each settlement period's own row (keyed by its `mid_sp_time`)
    # against every minute belonging to that period -- `mid_spot_times`
    # carries each minute's own `spot_time` plus the `mid_sp_time` of the
    # period it falls in, so this is where the one-row-per-period value
    # actually gets broadcast out to one row per minute.
    temp4 = pd.merge(temp3, mid_spot_times, on="mid_sp_time", how="inner")
    temp4["spot_mw_value"] = temp4["mid_sp_value"] - temp4["mw_delta"] * (temp4["minutes_from_mid"] / 30)

    out = temp4[["spot_time", "spot_mw_value"]].dropna()
    return out.rename(columns={"spot_mw_value": f"spot_{variable_col_name}"})


def _latest_publish_per_period(records: list[dict], value_field: str, out_name: str) -> pd.DataFrame:
    """NDF/TSDF/INDO/ITSDO share this shape: one row per (settlementDate,
    settlementPeriod, publishTime) -- keep only the most recently published
    value for each period, ported from the notebook's own fetch_ndf/etc.
    """
    df = pd.DataFrame(records)
    if df.empty:
        return pd.DataFrame(columns=["settlementDate", "settlementPeriod", out_name])
    df["publishTime"] = pd.to_datetime(df["publishTime"], utc=True)
    df["settlementDate"] = pd.to_datetime(df["settlementDate"]).dt.date
    latest = df.groupby(["settlementDate", "settlementPeriod"])["publishTime"].max().reset_index()
    df = pd.merge(df, latest, on=["settlementDate", "settlementPeriod", "publishTime"])
    df = df[["settlementDate", "settlementPeriod", value_field]].rename(columns={value_field: out_name})
    df["settlementDate"] = pd.to_datetime(df["settlementDate"])
    return df.sort_values(["settlementDate", "settlementPeriod"]).reset_index(drop=True)


def build_demand_frame(ndf: list[dict], tsdf: list[dict], indo: list[dict], itsdo: list[dict], da_ndf: list[dict], start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """The four demand-forecast datasets, each interpolated onto the same
    per-minute grid and merged into one frame -- notebook cells covering
    `demand_processing_start`..`demand_dfs`.
    """
    demand_spot_times, mid_spot_times = build_spot_time_grids(start, end)

    latest_ndf = _latest_publish_per_period(ndf, "demand", "latest_ndf")
    latest_tsdf = _latest_publish_per_period(tsdf, "demand", "latest_tsdf")
    latest_indo = _latest_publish_per_period(indo, "demand", "indo")
    latest_itsdo = _latest_publish_per_period(itsdo, "demand", "itsdo")
    latest_da_ndf = _latest_publish_per_period(da_ndf, "demand", "da_ndf")

    spot_tsdf = sp_time_interpolator(latest_tsdf, "latest_tsdf", demand_spot_times, mid_spot_times)
    spot_ndf = sp_time_interpolator(latest_ndf, "latest_ndf", demand_spot_times, mid_spot_times)
    spot_itsdo = sp_time_interpolator(latest_itsdo, "itsdo", demand_spot_times, mid_spot_times)
    spot_indo = sp_time_interpolator(latest_indo, "indo", demand_spot_times, mid_spot_times)
    spot_da_ndf = sp_time_interpolator(latest_da_ndf, "da_ndf", demand_spot_times, mid_spot_times)

    demand = pd.merge(spot_tsdf, spot_ndf, how="outer", on="spot_time")
    demand = pd.merge(demand, spot_itsdo, how="outer", on="spot_time")
    demand = pd.merge(demand, spot_indo, how="outer", on="spot_time")
    demand = pd.merge(demand, spot_da_ndf, how="outer", on="spot_time")
    return pd.merge(demand, demand_spot_times, on="spot_time", how="outer")


# ---------------------------------------------------------------------------
# The decision table: aggregated view + NIV estimate
# ---------------------------------------------------------------------------

def build_aggregated(by_fuel: pd.DataFrame, demand: pd.DataFrame, system_delta: pd.DataFrame) -> pd.DataFrame:
    """One row per `spot_time`: total generation-vs-plan, demand-side
    error, and the `niv_estimate` decision figure -- the notebook's
    `aggregated_fpn_mel_boalf`/final `df` merged into a single table.

    `system_delta`: (settlementDate, settlementPeriod, spot_time, delta) --
    the system-wide sum of accepted-minus-FPN volume, computed directly
    from this module's own per-unit merge (`explode_and_merge`'s `delta`,
    summed) instead of the notebooks' cross-process `spot_niv.pkl`.
    """
    group_cols = ["settlementDate", "settlementPeriod", "spot_time"]
    sum_cols = ["fpn_spot_vol", "fuelinst_generation", "market_gen", "mel_reduced_fpn", "market_gen_vs_fpn", "mel_mil_drop", "market_gen_vs_adj_fpn", "adjusted_fpn"]
    present = [c for c in sum_cols if c in by_fuel.columns]
    aggregated = by_fuel.groupby(group_cols)[present].sum(min_count=1).reset_index()

    aggregated = pd.merge(aggregated, system_delta, on=group_cols, how="left")

    nuke_drop = by_fuel.loc[by_fuel["FT"] == "NUCLEAR", ["spot_time", "mel_mil_drop"]].rename(columns={"mel_mil_drop": "nuke_mel_drop"})
    aggregated = pd.merge(aggregated, nuke_drop, on="spot_time", how="left")
    aggregated["nuke_mel_drop"] = aggregated["nuke_mel_drop"].fillna(0)
    aggregated["nn_mel_drop"] = aggregated["mel_mil_drop"] - aggregated["nuke_mel_drop"]

    aggregated["misco_indo"] = aggregated["fuelinst_generation"].rolling(5).mean()
    aggregated["control_room_gen"] = aggregated["fpn_spot_vol"] + aggregated["delta"]
    aggregated["fpn_vol_bar_nuke_drop"] = aggregated["fpn_spot_vol"] + aggregated["nuke_mel_drop"]

    aggregated = pd.merge(aggregated, demand, on=group_cols, how="outer")

    aggregated["unexplained_dmd"] = aggregated["misco_indo"] - aggregated["spot_indo"]
    aggregated["unexplained_dmd_MA"] = aggregated["unexplained_dmd"].rolling(window=30, min_periods=5).mean().ffill()
    aggregated["misco_ndf"] = aggregated["spot_latest_ndf"] + np.maximum(aggregated["unexplained_dmd_MA"], 300)

    aggregated["misco_indo_vs_ndf"] = aggregated["misco_indo"] - aggregated["misco_ndf"]
    aggregated["misco_indo_vs_da_ndf"] = aggregated["misco_indo"] - aggregated["spot_da_ndf"]
    aggregated["misco_indo_vs_da_MA"] = aggregated["misco_indo_vs_da_ndf"].rolling(window=30, min_periods=5).mean().ffill()
    aggregated["misco_da_adj_ndf"] = aggregated["spot_da_ndf"] + aggregated["misco_indo_vs_da_MA"]
    aggregated["dmd_risk"] = aggregated["misco_da_adj_ndf"] - aggregated["misco_ndf"]

    aggregated["area_under_curve"] = aggregated["misco_ndf"] - aggregated["adjusted_fpn"]
    period_avg = aggregated.groupby(["settlementDate", "settlementPeriod"])[["area_under_curve", "delta"]].mean().reset_index()
    period_avg = period_avg.rename(columns={"area_under_curve": "auc_av", "delta": "delta_av"})
    aggregated = pd.merge(aggregated, period_avg, on=["settlementDate", "settlementPeriod"], how="left")

    aggregated["niv_error"] = aggregated["delta"] - aggregated["area_under_curve"]
    expected_error = aggregated["misco_indo_vs_ndf"] - aggregated["market_gen_vs_adj_fpn"]
    aggregated["unexp_delta"] = aggregated["niv_error"] - expected_error
    return aggregated


def build_decision_table(by_fuel: pd.DataFrame, aggregated: pd.DataFrame) -> pd.DataFrame:
    """The headline output: `niv_estimate`, one row per (settlementDate,
    settlementPeriod), broken into named drivers -- ported exactly from
    the notebook's final `df` (its cells 15-20).
    """
    group_cols = ["settlementDate", "settlementPeriod"]
    by_fuel_by_sp = by_fuel.groupby(group_cols + ["FT"]).mean(numeric_only=True).reset_index()
    aggregated_by_sp = aggregated.groupby(group_cols).mean(numeric_only=True).reset_index()

    wind = by_fuel_by_sp[by_fuel_by_sp["FT"] == "WIND"][group_cols + ["market_gen_vs_adj_fpn"]].copy()
    wind = wind.rename(columns={"market_gen_vs_adj_fpn": "wind_deviation"})
    wind["wind_deviation"] = wind["wind_deviation"].ffill()

    other = by_fuel_by_sp[~by_fuel_by_sp["FT"].isin(["WIND", "NUCLEAR"])][group_cols + ["market_gen_vs_adj_fpn"]].copy()
    other = other.groupby(group_cols)["market_gen_vs_adj_fpn"].sum().reset_index()
    other = other.rename(columns={"market_gen_vs_adj_fpn": "other_gen_deviation"})
    if len(other) >= 3:
        other.loc[other.index[-2:], "other_gen_deviation"] = other["other_gen_deviation"].iloc[-3]

    decision = pd.merge(aggregated_by_sp, wind, on=group_cols, how="left")
    decision = pd.merge(decision, other, on=group_cols, how="left")
    decision = decision[group_cols + ["auc_av", "delta_av", "misco_indo_vs_ndf", "dmd_risk", "niv_error", "unexp_delta", "wind_deviation", "other_gen_deviation"]]

    decision["misco_indo_vs_ndf"] = decision["misco_indo_vs_ndf"].fillna(decision["dmd_risk"])
    decision["unexp_delta"] = decision["unexp_delta"].fillna(decision["unexp_delta"].expanding(min_periods=1).mean())

    decision["niv_estimate"] = (
        decision["auc_av"] - decision["wind_deviation"] - decision["other_gen_deviation"] + decision["misco_indo_vs_ndf"] + decision["unexp_delta"]
    )
    return decision


# ---------------------------------------------------------------------------
# Real generation by fuel type (Fuelinst notebook)
# ---------------------------------------------------------------------------

def compute_generation_by_fuel(fuelinst_records: list[dict], per_unit_delta: pd.DataFrame, fuel_ref: pd.DataFrame) -> pd.DataFrame:
    """Splits FUELINST's real generation, by fuel type, into "market-driven"
    (`market_gen`) and "BM-action-driven" (`delta_gen`) portions, using this
    module's own per-unit `delta` (see `explode_and_merge`) in place of the
    Fuelinst notebook's `small_fpn_boalf_disbsad_spot.pkl`. The notebook's
    own bmUnit-suffix-stripping (`[:-2]`/`[:-3]`) was a workaround for how
    *that* pickle encoded multiple BOD bands into one string column; this
    module's own per-unit delta has no such encoding, so that step is
    dropped -- not a behaviour change, just no longer needed.

    Returns a long (TS, fuel_type, real_gen, market_gen, delta_gen) frame,
    replacing the notebook's wide `_r`/`_m`/`_d`-suffixed pivot.
    """
    fuelinst = pd.DataFrame(fuelinst_records)
    if fuelinst.empty:
        return pd.DataFrame(columns=["TS", "fuel_type", "real_gen", "market_gen", "delta_gen"])

    fuelinst["startTime"] = pd.to_datetime(fuelinst["startTime"], utc=True).dt.round("5min")
    fuelinst = fuelinst.rename(columns={"generation": "fuelinst_generation", "fuelType": "FT"})

    delta_by_fuel = pd.DataFrame(columns=["FT", "startTime", "delta"])
    if not per_unit_delta.empty:
        delta = per_unit_delta.copy()
        delta["startTime"] = (delta["spot_time"] - pd.Timedelta(seconds=1.51 * 60)).dt.round("5min")
        delta = pd.merge(delta, fuel_ref[["bmUnit", "FT"]].drop_duplicates(), how="left", on="bmUnit")
        delta_by_fuel = delta.groupby(["FT", "startTime"])["delta"].sum().reset_index()

    merged = pd.merge(fuelinst, delta_by_fuel, how="left", on=["FT", "startTime"])
    merged["delta"] = merged["delta"].fillna(0)
    merged["market_gen"] = merged["fuelinst_generation"] - merged["delta"]

    return pd.DataFrame({
        "TS": merged["startTime"],
        "fuel_type": merged["FT"],
        "real_gen": merged["fuelinst_generation"],
        "market_gen": merged["market_gen"],
        "delta_gen": merged["delta"],
    })


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def compute(
    pn_df: pd.DataFrame, boalf_df: pd.DataFrame, mel_df: pd.DataFrame, mil_df: pd.DataFrame, disbsad_df: pd.DataFrame,
    fuelinst_records: list[dict], ndf: list[dict], tsdf: list[dict], indo: list[dict], itsdo: list[dict], da_ndf: list[dict],
    neso_trades: list[dict], bm_unit_reference: pd.DataFrame, demand_window: tuple[pd.Timestamp, pd.Timestamp],
    now: datetime | None = None,
) -> dict[str, pd.DataFrame]:
    """Runs the whole pipeline for the current rolling window and returns
    the frames FpnRunner persists: `by_fuel`, `worst_deviants`,
    `aggregated` (per-minute, with the decision-table's `niv_estimate` and
    its drivers broadcast onto every minute of their settlement period --
    the SP-level table the notebook itself only computes once per period),
    and `generation_by_fuel`. Empty dict if there's no PN data to explode
    yet (e.g. a fresh startup).
    """
    fuel_ref = fuel_type_reference(bm_unit_reference)
    sets = unit_sets(fuel_ref)

    merged = explode_and_merge(pn_df, boalf_df, mel_df, mil_df, fuel_ref, sets)
    if merged.empty:
        return {}

    by_unit, by_fuel = aggregate_by_fuel(merged, fuel_ref)
    worst_deviants = compute_worst_deviants(by_unit, now=now)

    spot_times = merged[["settlementDate", "settlementPeriod", "spot_time"]].drop_duplicates()
    interconnectors = interconnector_rows(pn_df, fuel_ref, sets, spot_times)
    natgrid = natgrid_trade_rows(neso_trades, disbsad_df, spot_times)

    fuelinst_df = pd.DataFrame(fuelinst_records)
    by_fuel = blend_generation_and_smooth(by_fuel, fuelinst_df, interconnectors, natgrid)

    start, end = demand_window
    demand = build_demand_frame(ndf, tsdf, indo, itsdo, da_ndf, start, end)

    system_delta = merged.groupby(["settlementDate", "settlementPeriod", "spot_time"])["delta"].sum().reset_index()
    aggregated = build_aggregated(by_fuel, demand, system_delta)
    decision = build_decision_table(by_fuel, aggregated)

    group_cols = ["settlementDate", "settlementPeriod"]
    aggregated = pd.merge(
        aggregated, decision[group_cols + ["wind_deviation", "other_gen_deviation", "niv_estimate"]], on=group_cols, how="left"
    )

    per_unit_delta = merged[["bmUnit", "spot_time", "delta"]]
    generation_by_fuel = compute_generation_by_fuel(fuelinst_records, per_unit_delta, fuel_ref)

    return {
        "by_fuel": by_fuel,
        "worst_deviants": worst_deviants,
        "aggregated": aggregated,
        "generation_by_fuel": generation_by_fuel,
    }
