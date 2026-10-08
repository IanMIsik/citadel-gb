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
everything *around* it. Both original cross-notebook Google-Drive pickle
exchanges (`spot_niv.pkl`, `small_fpn_boalf_disbsad_spot.pkl`) carried real
pricing-stack-derived data out of "the pricing stack notebook" into these
ones -- neither is dropped, both are now direct in-process Postgres reads
of the real pricing stack's own output instead:
  - `small_fpn_boalf_disbsad_spot.pkl`'s per-unit accepted-volume delta ->
    `pricing_stack_delta_by_fuel` (this project's own `pricing_stack_rows`),
    used for `market_gen`: FUELINST's real generation reading already
    reflects accepted offers (added) and accepted bids (subtracted), and
    `market_gen` needs to remove exactly that effect, which only the
    pricing stack's own reversal-aware, CADL-aware acceptance data gets
    right -- not this module's own simplified, independently re-derived
    per-minute delta from `explode_and_merge`.
  - `spot_niv.pkl`'s genuinely per-minute NIV trajectory ->
    `pricing_stack_niv_spot_time` (engine/stack.py's `spot_time_niv()`),
    used for the Delta Chart's `delta`/`niv_error` (see build_aggregated()'s
    own docstring) -- same reasoning: the real bid/offer-driven, sub-period
    trajectory only the pricing stack's own reversal-aware `delta` can give,
    not a flat once-per-period figure or this module's own naive per-unit
    delta.
`explode_and_merge`'s own `delta` is still computed and still feeds the
by-fuel-type FPN-delta/MEL-MIL-drop pivots -- it's a different, simpler
figure (raw accepted-vs-FPN, no reversal/CADL awareness) kept for that
different purpose, not the decision-table pipeline above.

Elexon's raw PN/MEL/MIL/BOALF records carry both a `bmUnit` (Elexon's own
BM Unit code) and a `nationalGridBmUnit` field. `bm_unit_reference` (see
storage/schema.sql) keys on `national_grid_bm_unit` but also carries
`elexon_bm_unit` -- `fuel_type_reference()` below turns it into a small
(bmUnit, nationalGridBmUnit, FT) frame joinable against either raw field,
the same shape as the notebooks' own `bmu_fuel_types`/`int_bm_units` CSVs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd

from . import stack as stack_engine
from .bm_stack import compute_bm_stack
from .natgrid import total_mw_by_period
from ..settlement import current_period, utc_to_settlement, window_around

# A unit's own "fuel type" in Elexon's reference data doubles as its
# interconnector name for interconnectors (INTFR, INTNED, INTELEC, ...) --
# confirmed against the notebooks' own column lists (Fuelinst notebook's
# interconnector_df columns are exactly these INT* codes). No separate
# interconnector reference is needed.
INTERCONNECTOR_FUEL_PREFIX = "INT"

# Second letter of an interconnector user's BM unit name -> its FUELINST code (observed across every
# unit in bm_unit_reference that already has a specific code; "IE" units name the interconnector later).
INTERCONNECTOR_NAME_PREFIX_CODES = {
    "I2": "INTIFA2", "IB": "INTNED", "IF": "INTFR", "IG": "INTGRNL", "II": "INTEW",
    "IL": "INTELEC", "IM": "INTIRL", "IN": "INTNEM", "IS": "INTNSL", "IV": "INTVKL",
}

# Elexon's own documented FUELINST fuel-type enum -- the fixed set of
# categories Elexon itself recognises (confirmed against the Fuelinst
# notebook's own explicit column list, which only ever used names from
# this set). `bm_unit_reference.fuel_type` also carries broader, curator-
# supplied labels (e.g. "BATTERIES", "LOAD RESPONSE", a generic
# "INTERCONNECTOR") for units FUELINST has no category for at all -- those
# units never have real generation data to merge against anyway, so
# `fuel_type_reference()` below excludes them rather than let them show up
# as all-"--" rows on the dashboard.
ELEXON_FUEL_TYPES = frozenset({
    "CCGT", "OCGT", "OIL", "COAL", "NUCLEAR", "WIND", "PS", "NPSHYD", "OTHER", "BIOMASS",
    "INTFR", "INTIRL", "INTNED", "INTEW", "INTNEM", "INTELEC", "INTIFA2", "INTNSL", "INTVKL", "INTGRNL",
})

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

def fuel_type_reference(bm_unit_reference: pd.DataFrame, other_fallback: bool = False) -> pd.DataFrame:
    """`bm_unit_reference` rows (national_grid_bm_unit, elexon_bm_unit,
    fuel_type, ...) -> a (bmUnit, nationalGridBmUnit, FT) frame, dropping
    units missing either identifier or a fuel type, and units whose fuel
    type isn't one Elexon itself recognises (see `ELEXON_FUEL_TYPES`) --
    the direct analogue of the notebooks' own `bmu_fuel_types` CSV.

    `other_fallback` (opt-in, see config.py's `fpn_other_fallback_enabled`):
    instead of dropping a unit whose curated `fuel_type` isn't one of
    `ELEXON_FUEL_TYPES` (BATTERIES, LOAD RESPONSE, GAS, DIESEL, SOLAR, the
    generic INTERCONNECTOR label is NOT remapped -- it stays dropped, see below),
    remaps it to "OTHER" so it still shows up on the FPN dashboard instead
    of contributing nothing at all. Checked live 2026-09-25: all 110 units
    currently carrying one of those five labels are genuinely live BM units
    per Elexon's own reference API, and neither Elexon's live data nor the
    BMU fuel type spreadsheet suggests a better-fitting FUELINST category
    for any of them -- OTHER is the correct fallback, not a guess. Doesn't
    touch `bm_unit_reference.fuel_type` itself, so the granular label is
    still there for anything else that reads it.
    """
    ref = bm_unit_reference.dropna(subset=["elexon_bm_unit", "national_grid_bm_unit", "fuel_type"]).copy()
    ref["fuel_type"] = ref["fuel_type"].str.upper()
    # A unit curated only as the generic "INTERCONNECTOR" is an interconnector user's trading unit. Its
    # name says which interconnector (second letter, e.g. IBD-BAYW1 -> BritNed): the same rule holds
    # for every unit that already carries a specific INT* code. Anything we can't place stays generic
    # and is never remapped to OTHER (it is not generation).
    generic = ref["fuel_type"] == "INTERCONNECTOR"
    ref.loc[generic, "fuel_type"] = ref.loc[generic, "national_grid_bm_unit"].str[:2].map(INTERCONNECTOR_NAME_PREFIX_CODES).fillna("INTERCONNECTOR")
    if other_fallback:
        remap = ~ref["fuel_type"].isin(ELEXON_FUEL_TYPES) & ~ref["fuel_type"].str.startswith("INTERCONNECTOR")
        ref.loc[remap, "fuel_type"] = "OTHER"
    ref = ref[ref["fuel_type"].isin(ELEXON_FUEL_TYPES)]
    ref = ref.rename(columns={"elexon_bm_unit": "bmUnit", "national_grid_bm_unit": "nationalGridBmUnit", "fuel_type": "FT"})[
        ["bmUnit", "nationalGridBmUnit", "FT"]
    ]
    # A source that (rarely) carries the same bmUnit under two different
    # fuel types would otherwise fan out every merge keyed on bmUnit and,
    # worse, non-deterministically flip that unit between fuel types from
    # one recompute to the next depending on merge/groupby row order --
    # confirmed as the cause of a real reported bug (a unit's generation
    # alternating between two fuel-type buckets across dashboard
    # refreshes). Sorting first makes the one survivor per bmUnit stable
    # from run to run, not just unique.
    return ref.sort_values(["bmUnit", "FT"]).drop_duplicates(subset=["bmUnit"], keep="first").reset_index(drop=True)


def unit_sets(fuel_ref: pd.DataFrame) -> dict[str, set[str]]:
    is_interconnector = fuel_ref["FT"].str.upper().str.startswith(INTERCONNECTOR_FUEL_PREFIX)
    generating = fuel_ref[~is_interconnector]
    return {
        "generating_units": set(generating["nationalGridBmUnit"]),
        "wind_units": set(generating.loc[generating["FT"] == "WIND", "nationalGridBmUnit"]),
        # Keyed by bmUnit, not nationalGridBmUnit: the user's own
        # interconnector reference (misco_fuel_type_reference.py) has no
        # real nationalGridBmUnit column for interconnectors at all, only
        # bmUnit -- see that module's own docstring.
        "interconnector_bm_units": set(fuel_ref.loc[is_interconnector, "bmUnit"]),
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

# Exploding PN / MEL / MIL into one row per unit per minute costs 1-2.5 s EACH on
# every FPN cycle, yet those three inputs rarely change between cycles (the REST
# refresh re-reads the same records; only an occasional revision or notification
# differs). The exploded frames are therefore kept in this process, keyed by a
# hash of the exact input records, and reused while the input is identical --
# which only works because FpnRunner runs compute() in ONE long-lived worker (a
# fresh process each time would have an empty cache). Any change to the input,
# however small, changes the hash and forces a normal re-explode.
_EXPLODE_CACHE: dict[str, tuple[int, pd.DataFrame]] = {}


def _frame_fingerprint(df: pd.DataFrame) -> int | None:
    """Content hash of a frame, or None if it cannot be hashed (then nothing is cached)."""
    if df.empty:
        return 0
    try:
        return (int(pd.util.hash_pandas_object(df, index=False).sum()) % (2**63)) ^ len(df)
    except TypeError:
        return None


def _cached_explode(name: str, df: pd.DataFrame, explode) -> tuple[pd.DataFrame, bool]:
    """`explode(df)`, reused from the previous cycle if `df` is unchanged. Returns (frame, was_cached);
    the frame is always a private copy, so callers may mutate it freely."""
    key = _frame_fingerprint(df)
    hit = _EXPLODE_CACHE.get(name)
    if key is not None and hit is not None and hit[0] == key:
        return hit[1].copy(), True
    out = explode(df)
    if key is not None:
        _EXPLODE_CACHE[name] = (key, out)
        return out.copy(), False
    return out, False


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
    sub: dict[str, float] = {}
    _t = [time.perf_counter()]

    def _lap(name: str) -> None:
        now_ = time.perf_counter()
        sub[name] = round(now_ - _t[0], 2)
        _t[0] = now_

    pn_df, mel_df, mil_df, boalf_df = pn_df.copy(), mel_df.copy(), mil_df.copy(), boalf_df.copy()
    for df in (pn_df, mel_df, mil_df, boalf_df):
        if not df.empty:
            df["settlementDate"] = pd.to_datetime(df["settlementDate"])
    _lap("copy_dates")

    usable_pn, mel_filtered, mil_filtered = filter_to_generating_units(pn_df, mel_df, mil_df, fuel_ref, sets)
    _lap("filter_units")

    exploded_fpn, pn_hit = _cached_explode("pn", usable_pn, _fpn_exploder)
    _lap("explode_pn" + ("_cached" if pn_hit else ""))
    exploded_boalf = stack_engine.boalf_exploder(boalf_df)
    _lap("explode_boalf")
    exploded_mel, mel_hit = _cached_explode("mel", mel_filtered, lambda d: _mel_mil_exploder(d, "mel_spot_vol"))
    _lap("explode_mel" + ("_cached" if mel_hit else ""))
    exploded_mil, mil_hit = _cached_explode("mil", mil_filtered, lambda d: _mel_mil_exploder(d, "mil_spot_vol"))
    _lap("explode_mil" + ("_cached" if mil_hit else ""))

    if exploded_fpn.empty:
        return pd.DataFrame()

    exploded_fpn = apply_wind_period_average(exploded_fpn, sets["wind_units"])
    _lap("wind_average")

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
    _lap("joins")

    # `delta` must come from the *raw* (still-NaN-where-unmatched)
    # boalf_spot_vol, exactly as the notebook does it (`result['delta'] =
    # result['boalf_spot_vol'] - result['fpn_spot_vol']` runs before its
    # own `result['boalf_spot_vol'] = result['boalf_spot_vol'].fillna(0)`)
    # -- a unit with no active BOALF acceptance has NaN boalf_spot_vol, so
    # NaN - fpn_spot_vol is NaN, and only *that* gets filled to 0 (correctly
    # "no BM action, no deviation"). Filling boalf_spot_vol to 0 first, as
    # this used to, computed `0 - fpn_spot_vol` instead -- wrongly treating
    # "the unit wasn't in the BM this minute" as "the unit was curtailed to
    # zero", which inflated every fuel type's own summed delta by roughly
    # its whole untouched fleet's worth of FPN (confirmed directly: real
    # CCGT deviation is the sum of 3 genuinely-active units' own deltas,
    # ~664 MW -- the other 78 CCGT units were each wrongly contributing
    # their own full FPN as spurious negative delta).
    result["delta"] = (result["boalf_spot_vol"] - result["fpn_spot_vol"]).fillna(0)
    result["boalf_spot_vol"] = result["boalf_spot_vol"].fillna(0)

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
    _lap("columns")
    result.attrs["timings"] = sub
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

    # "Current" MEL/MIL means the latest minute we actually HAVE data for,
    # not literally wall-clock `now` -- MEL/MIL publish with a real lag
    # (confirmed live: ~10 minutes behind `now` is normal), so a strict
    # "last 2 minutes before now" window was empty on every single cycle,
    # silently producing zero worst deviants forever (root cause of the
    # dashboard showing days-stale data: `_persist` only replaces rows for
    # periods it has fresh data for, so an empty result here just leaves
    # old rows sitting untouched instead of refreshing or clearing them).
    # Capping at the data's own latest spot_time -- never later than real
    # `now`, in case of clock skew -- fixes the lookback without touching
    # the forward/past split below, which must stay on real wall-clock time.
    data_now = min(pd.Timestamp(now), fpn_mel_boalf_by_unit["spot_time"].max())
    recent_from = data_now - pd.Timedelta(minutes=2)
    recent_mask = (deviants["spot_time"] > recent_from) & (deviants["spot_time"] <= data_now)
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
# Plant trip detection
# ---------------------------------------------------------------------------

TRIP_THRESHOLD_MW = 50.0
# A unit must fall back below this before it can trip-alert again -- without
# this gap, a unit sitting right at TRIP_THRESHOLD_MW would re-fire on every
# single recompute cycle instead of once per genuine trip.
TRIP_RESET_MW = 15.0


@dataclass
class TripEvent:
    bm_unit: str
    fuel_type: str | None
    drop_mw: float
    settlement_date: date
    settlement_period: int
    detected_at: datetime


# A tripped unit that has clawed back this share of its peak drop is "coming
# back" -- worth its own (green) notification before it's fully recovered.
TRIP_PARTIAL_RECOVERY_SHARE = 0.5


@dataclass
class TripRecovery:
    """A previously-announced trip going away: `partial` once the drop has
    halved, `full` once it's back under TRIP_RESET_MW."""

    bm_unit: str
    fuel_type: str | None
    kind: str  # "partial" | "full"
    peak_mw: float
    drop_mw: float
    settlement_date: date
    settlement_period: int
    detected_at: datetime


def detect_trips(
    by_unit: pd.DataFrame,
    prev_state: dict[str, dict],
    threshold_mw: float = TRIP_THRESHOLD_MW,
    reset_mw: float = TRIP_RESET_MW,
    now: datetime | None = None,
    seed_only: bool = False,
) -> tuple[list[TripEvent], list[TripRecovery], dict[str, dict]]:
    """Per BM unit, tracks its MIL/MEL drop (`abs(adjusted_fpn -
    fpn_spot_vol)`, same figure compute_worst_deviants uses) *as of now*:

    - `TripEvent`: fires once, when the drop first jumps from under
      `reset_mw` to `threshold_mw` or more. The unit is then "tripped" and
      stays silent -- however many settlement periods the trip lasts --
      until it recovers.
    - `TripRecovery`: `partial` when the drop has fallen to
      TRIP_PARTIAL_RECOVERY_SHARE of its peak, `full` when it's back under
      `reset_mw` (which also re-arms the unit for a future trip).

    The reading is the unit's latest minute at or before `now` -- NOT the
    last minute in the data, which is the far end of the forecast window
    (PN/MEL run ~2 settlement periods ahead) and flips every time that
    window rolls onto a new period (the cause of a trip re-alerting in
    each subsequent SP). `seed_only` records state without emitting
    anything: used on the first cycle after a restart so units that were
    already tripped aren't announced as new.

    `prev_state` ({unit: {"drop", "peak", "partial", "recovered_at"}}) is
    carried across cycles by the caller.
    """
    if by_unit.empty:
        return [], [], prev_state
    now = now or datetime.now(timezone.utc)

    current = by_unit[by_unit["spot_time"] <= pd.Timestamp(now)]
    if current.empty:
        return [], [], prev_state
    latest = current.sort_values("spot_time").groupby("bmUnit").tail(1)
    latest = latest.assign(mel_mil_drop=(latest["adjusted_fpn"] - latest["fpn_spot_vol"]).abs())

    new_state = {u: dict(s) for u, s in prev_state.items()}
    trips: list[TripEvent] = []
    recoveries: list[TripRecovery] = []
    for row in latest.itertuples(index=False):
        unit = row.bmUnit
        drop_mw = float(row.mel_mil_drop)
        st = new_state.get(unit) or {"drop": 0.0, "peak": None, "partial": False, "recovered_at": None}
        sd = row.settlementDate
        where = dict(
            bm_unit=unit, fuel_type=row.FT, settlement_date=sd.date() if hasattr(sd, "date") else sd,
            settlement_period=int(row.settlementPeriod), detected_at=now,
        )
        if st["peak"] is None:
            if drop_mw >= threshold_mw and (st["drop"] < reset_mw or seed_only):
                st["peak"], st["partial"], st["recovered_at"] = drop_mw, False, None
                if not seed_only:
                    trips.append(TripEvent(drop_mw=drop_mw, **where))
        else:
            st["peak"] = max(st["peak"], drop_mw)
            if drop_mw < reset_mw:
                if not seed_only:
                    recoveries.append(TripRecovery(kind="full", peak_mw=st["peak"], drop_mw=drop_mw, **where))
                st["peak"], st["partial"], st["recovered_at"] = None, False, now.isoformat()
            elif not st["partial"] and drop_mw <= st["peak"] * TRIP_PARTIAL_RECOVERY_SHARE:
                st["partial"] = True
                if not seed_only:
                    recoveries.append(TripRecovery(kind="partial", peak_mw=st["peak"], drop_mw=drop_mw, **where))
        st["drop"] = drop_mw
        new_state[unit] = st
    return trips, recoveries, new_state


# How long after a trip fully recovers its unit stays on the worst-behaviour
# graphs -- long enough to see the recovery, short enough not to pile up.
WB_RECENT_RECOVERY_HOURS = 3


def worst_behaviour_series(
    by_unit: pd.DataFrame, trip_state: dict[str, dict], window_periods: list[tuple], now: datetime, minute_step: int = 5,
) -> pd.DataFrame:
    """the reference app' "Worst Behaviour Plants" data: for every currently-tripped
    unit (and any recovered within WB_RECENT_RECOVERY_HOURS), one point per
    `minute_step` minutes across the window -- `vol` (adjusted_fpn: the MW it
    can actually deliver once its MEL/MIL are applied -- which in the future
    minutes *is* its published plan for coming back), plus `fpn` and `mel`.
    """
    cutoff = now - pd.Timedelta(hours=WB_RECENT_RECOVERY_HOURS)
    units = [
        u for u, s in trip_state.items()
        if s.get("peak") is not None or (s.get("recovered_at") and pd.Timestamp(s["recovered_at"]) >= pd.Timestamp(cutoff))
    ]
    cols = ["bm_unit", "fuel_type", "status", "spot_time", "settlement_period", "fpn", "mel", "mil", "vol"]
    if by_unit.empty or not units or not window_periods:
        return pd.DataFrame(columns=cols)
    periods = pd.DataFrame({
        "settlementDate": pd.to_datetime([sd for sd, _ in window_periods]),
        "settlementPeriod": [int(sp) for _, sp in window_periods],
    })
    bu = by_unit[by_unit["bmUnit"].isin(units)].copy()
    bu["settlementDate"] = pd.to_datetime(bu["settlementDate"])
    bu = bu.merge(periods, on=["settlementDate", "settlementPeriod"])
    bu = bu[bu["spot_time"].dt.minute % minute_step == 0]
    if bu.empty:
        return pd.DataFrame(columns=cols)
    bu["status"] = bu["bmUnit"].map(lambda u: "tripped" if trip_state[u].get("peak") is not None else "recovered")
    bu["mel"] = bu["mel_spot_vol"].where(np.isfinite(bu["mel_spot_vol"]), None)
    # MIL (Maximum Import Level) is what holds back an importing plant such as pumped storage; an
    # unpublished MIL is stored as -inf ("no limit"), which is not a value worth keeping.
    bu["mil"] = bu["mil_spot_vol"].where(np.isfinite(bu["mil_spot_vol"]), None) if "mil_spot_vol" in bu else None
    bu["spot_time"] = bu["spot_time"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    out = bu.rename(columns={"bmUnit": "bm_unit", "FT": "fuel_type", "settlementPeriod": "settlement_period", "fpn_spot_vol": "fpn", "adjusted_fpn": "vol"})
    out = out[cols].sort_values(["bm_unit", "spot_time"])
    for c in ("fpn", "vol"):
        out[c] = out[c].round(1)
    for c in ("mel", "mil"):
        out[c] = out[c].map(lambda v: None if v is None or pd.isna(v) else round(float(v), 1))
    return out.reset_index(drop=True)


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
    if pn_df.empty or not sets["interconnector_bm_units"]:
        return pd.DataFrame()
    int_pn = pn_df[pn_df["bmUnit"].isin(sets["interconnector_bm_units"])].copy()
    if int_pn.empty:
        return pd.DataFrame()
    # Joined on bmUnit alone -- the user's own interconnector reference has
    # no real nationalGridBmUnit for these units, see unit_sets()'s docstring.
    int_pn = pd.merge(int_pn, fuel_ref[["bmUnit", "FT"]].drop_duplicates(), on="bmUnit", how="left")
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
    resolving the NESO feed and Elexon DISBSAD period by period (see engine/natgrid.py).
    """
    # The two sources are resolved as engine/natgrid.py describes (DISBSAD for periods that have
    # ended, NESO for current/future, DISBSAD extras merged in). `ng_vol` stays MWh per
    # settlement period (MW / 2), which the `* -2` below turns back into MW.
    disbsad_records = disbsad_df.to_dict("records") if not disbsad_df.empty else []
    totals = total_mw_by_period(neso_trades, disbsad_records)
    combined = pd.DataFrame(
        [{"settlementPeriod": sp, "settlementDate": d, "ng_vol": mw / 2} for (d, sp), mw in sorted(totals.items())],
        columns=["settlementPeriod", "settlementDate", "ng_vol"],
    )
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


def _strip_pair_id_suffix(bm_unit: str) -> str:
    """stack.py's build_price_stack appends "_<pairId>" (a small integer,
    possibly negative, e.g. "_-2") to every bmUnit after its six-case
    BOD-band split (`combined["bmUnit"] = combined["bmUnit"] + "_" +
    combined["pairId"].astype(str)`) -- this reverses that. Robust to the
    original bmUnit itself containing underscores (common in Elexon's own
    codes, e.g. "2__NSMAE001"): only strips the last segment, and only if
    it's actually numeric, so a bmUnit lacking the suffix passes through
    unchanged instead of getting corrupted.
    """
    base, sep, suffix = bm_unit.rpartition("_")
    if sep and suffix.lstrip("-").isdigit():
        return base
    return bm_unit


def pricing_stack_niv_by_period(pricing_stack_niv_rows: list[dict]) -> pd.DataFrame:
    """`pricing_stack_niv_rows` (from db.pricing_stack_niv_by_period) --
    the pricing stack's own real, already-settled NIV per (settlementDate,
    settlementPeriod), for the "Delta table"'s `niv_sp_max` row (see the
    real the reference app delta-table.js, which reads this from a `spot_niv.pkl`
    handoff from the sibling notebook -- here it's the same in-process read
    already established for `market_gen`, via a different pricing-stack
    column).
    """
    columns = ["settlementDate", "settlementPeriod", "niv_sp_max"]
    if not pricing_stack_niv_rows:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame(pricing_stack_niv_rows)
    df["settlementDate"] = pd.to_datetime(df["settlement_date"])
    return df.rename(columns={"settlement_period": "settlementPeriod"})[columns]


def pricing_stack_niv_spot_time(pricing_stack_niv_spot_time_rows: list[dict]) -> pd.DataFrame:
    """`pricing_stack_niv_spot_time_rows` (from
    db.pricing_stack_niv_spot_time_by_period) -- the pricing stack's own
    genuinely per-minute NIV trajectory (engine/stack.py's spot_time_niv()),
    the real `niv_spot_time_max` this module's Delta Chart `delta` line
    needs (see build_aggregated()'s own docstring): unlike
    `pricing_stack_niv_by_period` above (one flat figure per whole period),
    this varies minute to minute with actual bid/offer acceptances, exactly
    the way the original notebook's own `misik_cast_delta` (its `spot_niv.pkl`
    handoff) did.
    """
    columns = ["settlementDate", "settlementPeriod", "spot_time", "niv_spot_time_max"]
    if not pricing_stack_niv_spot_time_rows:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame(pricing_stack_niv_spot_time_rows)
    df["settlementDate"] = pd.to_datetime(df["settlement_date"])
    df["spot_time"] = pd.to_datetime(df["spot_time"], utc=True)
    return df.rename(columns={"settlement_period": "settlementPeriod"})[columns]


def exploded_boalf_with_fuel_type(exploded: pd.DataFrame, fuel_ref: pd.DataFrame) -> pd.DataFrame:
    """engine/stack.py's exploded_boalf_by_unit() -> the same rows (minus
    the pairId band split, re-summed away) plus a `fuel_bucket` column for
    the chart's colour-coding. By request, this uses Elexon's own full
    fuel-type set (ELEXON_FUEL_TYPES above -- whatever fuel_ref's own `FT`
    already carries) rather than collapsing down onto the reference app's narrower
    8-bucket palette; the web page owns the colour-per-bucket mapping.

    `exploded`'s own bmUnit still carries the "_<pairId>" suffix
    build_price_stack() appends during its six-case BOD-band split
    (stack.py's own `combined` is mutated in place before
    exploded_boalf_by_unit() ever sees it) -- stripped here via
    `_strip_pair_id_suffix()`, the same helper every other bmUnit -> FT
    join in this module already needs for exactly this reason (e.g.
    `pricing_stack_delta_by_fuel_5min()` below). A unit priced across
    several bands in the same minute would otherwise fragment into one
    chart dataset per band instead of the one real per-unit series
    the reference app's own chart groups by -- re-summed by (bmUnit, spot_time) here
    to collapse those bands back together first.

    Any unit whose fuel type is an interconnector code (INTERCONNECTOR_FUEL_PREFIX,
    e.g. INTFR/INTNED/...) is dropped entirely, by request -- interconnectors
    don't appear as real balancing actions on this chart in practice.

    Synthetic DISBSAD rows (bmUnit LIKE 'disbsad_%', blend_disbsad()'s own
    National-Grid-balancing-action rows -- the suffix-stripped prefix
    survives regardless of what follows it) have no fuel_ref match at
    all -- mapped to 'NATGRID' directly, same as the reference app's own chart does
    for them. Anything else fuel_ref has no row for (a real BM unit
    outside `bm_unit_reference`, or one whose own fuel type isn't in
    ELEXON_FUEL_TYPES at all) falls to 'NO_FUEL' -- distinct from the real
    Elexon category 'OTHER', which is a genuine fuel_ref-backed match.
    """
    columns = ["settlementDate", "settlementPeriod", "bmUnit", "spot_time", "delta", "fuel_bucket"]
    if exploded.empty:
        return pd.DataFrame(columns=columns)
    df = exploded.copy()
    df["bmUnit"] = df["bmUnit"].map(_strip_pair_id_suffix)
    df = df.groupby(["settlementDate", "settlementPeriod", "bmUnit", "spot_time"])["delta"].sum().reset_index()
    df = pd.merge(df, fuel_ref[["bmUnit", "FT"]].drop_duplicates(), how="left", on="bmUnit")
    # Interconnectors never appear as balancing actions in practice (they're
    # not dispatchable BM units in the sense this chart cares about) -- by
    # request, dropped from this chart entirely rather than given their own
    # fuel_bucket colour.
    df = df[~df["FT"].fillna("").str.startswith(INTERCONNECTOR_FUEL_PREFIX)]
    is_disbsad = df["bmUnit"].str.startswith("disbsad_")
    df["fuel_bucket"] = df["FT"].fillna("NO_FUEL")
    df.loc[is_disbsad, "fuel_bucket"] = "NATGRID"
    return df.drop(columns=["FT"])


def pricing_stack_delta_by_fuel(pricing_stack_delta_rows: list[dict], fuel_ref: pd.DataFrame) -> pd.DataFrame:
    """`pricing_stack_delta_rows` (from db.pricing_stack_delta_by_bm_unit)
    -> real accepted-offer/reduced-accepted-bid volume per (settlementDate,
    settlementPeriod, FT) -- see this module's own docstring on why
    `market_gen` needs this rather than `explode_and_merge`'s own delta.

    `pricing_stack_rows.delta` is in **MWh**, not MW: stack.py's own
    `compute_stack` sums per-minute MW-level deltas and then divides by 60
    (`misik_stack["delta"] = misik_stack["delta"] / 60`, MW-minutes ->
    MWh) -- a settlement period is 30 minutes, so that MWh figure is only
    half of the average MW that produced it. Every caller here combines
    this with FUELINST's own instantaneous MW reading (`market_gen =
    fuelinst_generation - pricing_stack_delta`), so it's converted back to
    an average-MW-over-the-period figure (`* 2`) right here, once, rather
    than at each call site -- confirmed directly against real data: the
    unconverted MWh figure read as roughly half the real MW deviation
    (e.g. a genuine ~664 MW net CCGT deviation was computing as ~332).
    """
    columns = ["settlementDate", "settlementPeriod", "FT", "pricing_stack_delta"]
    if not pricing_stack_delta_rows:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame(pricing_stack_delta_rows)
    df["bmUnit"] = df["bm_unit"].map(_strip_pair_id_suffix)
    df["settlementDate"] = pd.to_datetime(df["settlement_date"])
    df = df.rename(columns={"settlement_period": "settlementPeriod"})
    df = pd.merge(df, fuel_ref[["bmUnit", "FT"]].drop_duplicates(), how="left", on="bmUnit")
    grouped = df.groupby(["settlementDate", "settlementPeriod", "FT"])["delta"].sum().reset_index()
    grouped["delta"] = grouped["delta"] * 2
    return grouped.rename(columns={"delta": "pricing_stack_delta"})


def pricing_stack_delta_by_fuel_5min(pricing_stack_unit_delta_5min_rows: list[dict], fuel_ref: pd.DataFrame) -> pd.DataFrame:
    """`pricing_stack_unit_delta_5min_rows` (from
    db.pricing_stack_unit_delta_5min_by_period) -> real accepted-offer/
    reduced-accepted-bid volume per (startTime, FT) -- one row per FUELINST
    5-minute bucket, unlike `pricing_stack_delta_by_fuel` above (one flat
    figure for the whole settlement period). Feeds
    compute_generation_by_fuel()'s `_d` column with the actual volume that
    landed in each specific bucket, so it shows how BM actions build up
    through a period instead of the same period-total repeated six times.

    Source is engine/stack.py's spot_time_bm_unit_delta_5min() -- the same
    reversal/CADL-aware `delta` `pricing_stack_delta_by_fuel` uses, just
    kept per-bmUnit-per-5-minutes instead of collapsed to one acceptance-
    level MWh figure. Already in MW (spot_time_bm_unit_delta_5min() sums
    `combined`'s own per-minute delta directly, before build_price_stack()'s
    /60 MWh collapse) -- unlike `pricing_stack_delta_by_fuel`, no `* 2`
    conversion applies here.
    """
    columns = ["startTime", "FT", "pricing_stack_delta"]
    if not pricing_stack_unit_delta_5min_rows:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame(pricing_stack_unit_delta_5min_rows)
    df["bmUnit"] = df["bm_unit"].map(_strip_pair_id_suffix)
    df["startTime"] = pd.to_datetime(df["start_time"], utc=True)
    df = pd.merge(df, fuel_ref[["bmUnit", "FT"]].drop_duplicates(), how="left", on="bmUnit")
    grouped = df.groupby(["startTime", "FT"])["delta"].sum().reset_index()
    return grouped.rename(columns={"delta": "pricing_stack_delta"})


def blend_generation_and_smooth(
    by_fuel: pd.DataFrame, fuelinst_df: pd.DataFrame, interconnector_rows_df: pd.DataFrame, natgrid_rows_df: pd.DataFrame,
    pricing_stack_delta_df: pd.DataFrame,
) -> pd.DataFrame:
    """Rounds each fuel-type row's `spot_time` onto FUELINST's own 5-minute
    grid, blends in real generation (`market_gen`/`market_gen_vs_fpn`/
    `market_gen_vs_adj_fpn`/`mel_mil_drop`), smooths the three trickiest
    fuel types, then appends the interconnector and NATGRID synthetic rows
    (which are *not* smoothed or FUELINST-blended -- ported as-is, same
    order the notebook does it in).

    `market_gen` is computed from `pricing_stack_delta_df` (the real
    pricing stack's own accepted-volume figure, broadcast across every
    minute of its settlement period), not `by_fuel`'s own `delta` column --
    see module docstring.
    """
    by_fuel = by_fuel.copy()
    by_fuel["startTime"] = (by_fuel["spot_time"] - pd.Timedelta(seconds=1.51 * 60)).dt.round("5min")

    if not interconnector_rows_df.empty:
        interconnector_rows_df = interconnector_rows_df.copy()
        # Computed fresh here (not carried over from wherever this frame
        # came from) so interconnectors line up with FUELINST's own 5-minute
        # blocks the same way every other fuel type does -- without this,
        # concatenating them in with no `startTime` at all (or a mismatched
        # one) means the FUELINST merge below can never match them, and
        # `market_gen`/`market_gen_vs_adj_fpn` silently stay blank for
        # every interconnector despite FUELINST reporting real generation
        # for each of them.
        interconnector_rows_df["startTime"] = (interconnector_rows_df["spot_time"] - pd.Timedelta(seconds=1.51 * 60)).dt.round("5min")
        by_fuel = pd.concat([by_fuel, interconnector_rows_df], ignore_index=True)

    if not fuelinst_df.empty:
        fuelinst = fuelinst_df.rename(columns={"fuelType": "FT", "generation": "fuelinst_generation"})
        # Joined on (FT, startTime) only -- NOT settlementDate/settlementPeriod
        # too. Elexon's own FUELINST records carry their own settlementPeriod
        # label, computed independently of `startTime`; it doesn't always
        # agree with this module's own settlementPeriod (derived from
        # `spot_time` via stack.py's London-local conversion), and requiring
        # both to match silently dropped an entire period's worth of real
        # generation to NaN whenever they disagreed -- confirmed live (a
        # settlement period with fpn_generation_by_fuel fully populated but
        # fpn_by_fuel's own fuelinst_generation NaN for every minute of it).
        # `startTime` alone already uniquely identifies one 5-minute FUELINST
        # block.
        fuelinst = fuelinst[["FT", "fuelinst_generation", "startTime"]].copy()
        fuelinst["startTime"] = pd.to_datetime(fuelinst["startTime"], utc=True).dt.round("5min")
        by_fuel["FT"] = by_fuel["FT"].replace({"ELECLINK": "INTELEC"})
        by_fuel["settlementDate"] = pd.to_datetime(by_fuel["settlementDate"])
        by_fuel = pd.merge(by_fuel, fuelinst, on=["FT", "startTime"], how="left")
        by_fuel = pd.merge(by_fuel, pricing_stack_delta_df, on=["settlementDate", "settlementPeriod", "FT"], how="left")
        by_fuel["pricing_stack_delta"] = by_fuel["pricing_stack_delta"].fillna(0)
        by_fuel["market_gen"] = by_fuel["fuelinst_generation"] - by_fuel["pricing_stack_delta"]
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

def build_aggregated(
    by_fuel: pd.DataFrame, demand: pd.DataFrame,
    niv_by_period: pd.DataFrame | None = None, niv_spot_time: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """One row per `spot_time`: total generation-vs-plan, demand-side
    error, and the `niv_estimate` decision figure -- the notebook's
    `aggregated_fpn_mel_boalf`/final `df` merged into a single table.

    `niv_spot_time`: (settlementDate, settlementPeriod, spot_time,
    niv_spot_time_max) -- the pricing stack's own genuinely per-minute NIV
    trajectory (engine/stack.py's spot_time_niv()), and the real driver of
    `delta` here. This is the same reversal/CADL-aware `delta`
    `pricing_stack_rows` itself is built from, just grouped by minute
    instead of by acceptance, and it's already in MW (see
    spot_time_niv()'s own docstring) -- no unit conversion needed, unlike
    `niv_by_period` below. Ported directly from the original notebook's own
    `misik_cast_delta` (its `spot_niv.pkl` handoff from the sibling pricing-
    stack notebook, merged onto `aggregated_fpn_mel_boalf` the exact same
    way here). A minute with no matching row -- no accepted volume active
    that minute -- is a real, honest zero (no BM action, no deviation), the
    same convention `explode_and_merge`'s own `delta` already uses; fillna(0)
    reflects that, not a guess.

    `niv_by_period`: (settlementDate, settlementPeriod, niv_sp_max) -- the
    pricing stack's own real, already-settled *period-total* NIV (see
    pricing_stack_niv_by_period), read directly from `pricing_stack_rows.
    total_delta`. Kept only as its own persisted column (FPN_AGGREGATED_
    COLUMNS) -- it no longer drives `delta`/`delta_av` themselves now that
    `niv_spot_time` supplies the real per-minute figure those are actually
    built from (`delta_av` below is this table's own period-average of
    `delta`, exactly as the notebook computes it, not a read of
    `niv_sp_max`). That column is in MWh, not MW (stack.py's own
    `compute_stack` sums per-minute MW-level deltas and divides by 60), so
    it isn't directly comparable to `delta`/`delta_av` here without the same
    `* 2` conversion `pricing_stack_delta_by_fuel` applies for its own callers.
    """
    group_cols = ["settlementDate", "settlementPeriod", "spot_time"]
    sum_cols = ["fpn_spot_vol", "fuelinst_generation", "market_gen", "mel_reduced_fpn", "market_gen_vs_fpn", "mel_mil_drop", "market_gen_vs_adj_fpn", "adjusted_fpn"]
    present = [c for c in sum_cols if c in by_fuel.columns]
    aggregated = by_fuel.groupby(group_cols)[present].sum(min_count=1).reset_index()

    if niv_spot_time is not None and not niv_spot_time.empty:
        aggregated = pd.merge(aggregated, niv_spot_time, on=group_cols, how="left")
        aggregated["delta"] = aggregated["niv_spot_time_max"].fillna(0)
    else:
        aggregated["delta"] = 0.0

    if niv_by_period is not None and not niv_by_period.empty:
        aggregated = pd.merge(aggregated, niv_by_period, on=["settlementDate", "settlementPeriod"], how="left")
    else:
        aggregated["niv_sp_max"] = np.nan

    nuke_drop = by_fuel.loc[by_fuel["FT"] == "NUCLEAR", ["spot_time", "mel_mil_drop"]].rename(columns={"mel_mil_drop": "nuke_mel_drop"})
    aggregated = pd.merge(aggregated, nuke_drop, on="spot_time", how="left")
    aggregated["nuke_mel_drop"] = aggregated["nuke_mel_drop"].fillna(0)
    aggregated["nn_mel_drop"] = aggregated["mel_mil_drop"] - aggregated["nuke_mel_drop"]

    aggregated["misco_indo"] = aggregated["fuelinst_generation"].rolling(5).mean()
    aggregated["control_room_gen"] = aggregated["fpn_spot_vol"] + aggregated["delta"]
    aggregated["fpn_vol_bar_nuke_drop"] = aggregated["fpn_spot_vol"] + aggregated["nuke_mel_drop"]

    aggregated = pd.merge(aggregated, demand, on=group_cols, how="outer")
    # Rows this outer merge adds from `demand` alone (a spot_time with no
    # generation-side data at all) have no real delta either.
    aggregated["delta"] = aggregated["delta"].fillna(0)

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

def compute_generation_by_fuel(fuelinst_records: list[dict], pricing_stack_unit_delta_5min_rows: list[dict], fuel_ref: pd.DataFrame) -> pd.DataFrame:
    """Splits FUELINST's real generation, by fuel type, into "market-driven"
    (`market_gen`) and "BM-action-driven" (`delta_gen`) portions, using the
    pricing stack's own real, already-computed per-unit accepted-volume
    delta -- not this module's own independently re-derived per-minute
    delta from `explode_and_merge`, which this used previously. That own-
    derived delta disagreed with the pricing stack page's own figures for
    the same real units/period (confirmed directly by the user: current
    CCGT actions on the pricing stack page did not match this table's own
    CCGT deviation, which instead matched the *old* notebook-era approach
    this replaces) -- this module's own delta docstring already called this
    out for `market_gen` generally, but this function alone had not yet
    been switched over.

    Uses `pricing_stack_delta_by_fuel_5min` (per-fuel-type, per-5-minute-
    bucket), not `pricing_stack_delta_by_fuel` (one flat period-total
    figure) -- confirmed against the user's own direct request: repeating
    the same period-total `_d` value across all six of a period's rows hid
    exactly when new BM actions actually landed within it. Both sources are
    the same underlying reversal/CADL-aware delta the pricing stack itself
    uses; `pricing_stack_delta_by_fuel`'s own period-total figure still
    backs `blend_generation_and_smooth`'s "Market Gen vs Adjusted Fpn" table
    (a different, SP-level view) and this module's `build_aggregated`.

    Returns a long (TS, settlement_period, fuel_type, real_gen, market_gen,
    delta_gen) frame, replacing the notebook's wide `_r`/`_m`/`_d`-suffixed
    pivot -- `market_gen = real_gen - delta` is the exact same formula the
    notebook itself uses (its own `temp['market_gen'] = temp['fuelinst_generation']
    - temp['delta']`): for WIND, `delta` is almost always negative (a
    bid/curtailment, accepted volume below FPN), so subtracting it *adds*
    the curtailed volume back; for every other fuel type the same
    subtraction nets off both bid-driven reductions (added back) and
    offer-driven increases (subtracted) -- one formula, not fuel-type-
    specific branching.
    """
    fuelinst = pd.DataFrame(fuelinst_records)
    if fuelinst.empty:
        return pd.DataFrame(columns=["TS", "settlement_period", "fuel_type", "real_gen", "market_gen", "delta_gen"])

    fuelinst["startTime"] = pd.to_datetime(fuelinst["startTime"], utc=True).dt.round("5min")
    fuelinst = fuelinst.rename(columns={"generation": "fuelinst_generation", "fuelType": "FT"})
    settlement = fuelinst["startTime"].map(utc_to_settlement)
    fuelinst["settlementPeriod"] = settlement.map(lambda t: t[1])

    # Joined on (FT, startTime) alone -- not settlementDate/settlementPeriod
    # too -- same reasoning as blend_generation_and_smooth()'s own FUELINST
    # merge: startTime alone already uniquely identifies one 5-minute
    # FUELINST block, and Elexon's own settlementPeriod label on a record
    # doesn't always agree with this module's own derivation of it.
    delta_by_fuel_5min = pricing_stack_delta_by_fuel_5min(pricing_stack_unit_delta_5min_rows or [], fuel_ref)
    merged = pd.merge(fuelinst, delta_by_fuel_5min, how="left", on=["startTime", "FT"])
    merged["pricing_stack_delta"] = merged["pricing_stack_delta"].fillna(0)
    merged["market_gen"] = merged["fuelinst_generation"] - merged["pricing_stack_delta"]

    return pd.DataFrame({
        "TS": merged["startTime"],
        "settlement_period": merged["settlementPeriod"],
        "fuel_type": merged["FT"],
        "real_gen": merged["fuelinst_generation"],
        "market_gen": merged["market_gen"],
        "delta_gen": merged["pricing_stack_delta"],
    })


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def compute_tail(
    state: dict, pricing_stack_delta_rows: list[dict], pricing_stack_niv_rows: list[dict],
    pricing_stack_niv_spot_time_rows: list[dict], pricing_stack_unit_delta_5min_rows: list[dict],
) -> dict:
    """The stack-dependent end of compute(): blend the pricing stack's accepted
    volumes into the per-fuel table, rebuild the aggregated table and the decision
    table (`niv_estimate`), and the generation-by-fuel series. `state` is the
    `tail_state` a full compute() returned; nothing in it is modified, so the same
    state can be reused every time the stack saves new rows."""
    timings: dict[str, float] = {}
    last = time.perf_counter()

    def lap(name: str) -> None:
        nonlocal last
        now_ = time.perf_counter()
        timings[name] = round(now_ - last, 2)
        last = now_

    fuel_ref = state["fuel_ref"]
    pricing_stack_delta_df = pricing_stack_delta_by_fuel(pricing_stack_delta_rows or [], fuel_ref)
    by_fuel = blend_generation_and_smooth(
        state["by_fuel"].copy(), state["fuelinst_df"].copy(), state["interconnectors"].copy(), state["natgrid"].copy(),
        pricing_stack_delta_df,
    )
    lap("blend_smooth")

    niv_by_period = pricing_stack_niv_by_period(pricing_stack_niv_rows or [])
    niv_spot_time = pricing_stack_niv_spot_time(pricing_stack_niv_spot_time_rows or [])
    aggregated = build_aggregated(by_fuel, state["demand"], niv_by_period, niv_spot_time)
    lap("aggregated")
    decision = build_decision_table(by_fuel, aggregated)
    lap("decision_table")

    group_cols = ["settlementDate", "settlementPeriod"]
    aggregated = pd.merge(
        aggregated, decision[group_cols + ["wind_deviation", "other_gen_deviation", "niv_estimate"]], on=group_cols, how="left"
    )

    generation_by_fuel = compute_generation_by_fuel(state["fuelinst_records"], pricing_stack_unit_delta_5min_rows, fuel_ref)
    lap("generation_by_fuel")
    return {"by_fuel": by_fuel, "aggregated": aggregated, "generation_by_fuel": generation_by_fuel, "timings": timings}


def compute(
    pn_df: pd.DataFrame, boalf_df: pd.DataFrame, mel_df: pd.DataFrame, mil_df: pd.DataFrame, disbsad_df: pd.DataFrame,
    fuelinst_records: list[dict], ndf: list[dict], tsdf: list[dict], indo: list[dict], itsdo: list[dict], da_ndf: list[dict],
    neso_trades: list[dict], bm_unit_reference: pd.DataFrame, demand_window: tuple[pd.Timestamp, pd.Timestamp],
    pricing_stack_delta_rows: list[dict] | None = None,
    now: datetime | None = None,
    pricing_stack_niv_rows: list[dict] | None = None,
    pricing_stack_niv_spot_time_rows: list[dict] | None = None,
    pricing_stack_unit_delta_5min_rows: list[dict] | None = None,
    other_fallback: bool = False,
    trip_state: dict[str, float] | None = None,
    bod_df: pd.DataFrame | None = None,
    bm_stack_enabled: bool = False,
    trip_seed_only: bool = False,
) -> dict[str, object]:
    """Runs the whole pipeline for the current rolling window and returns
    the frames FpnRunner persists: `by_fuel`, `worst_deviants`,
    `aggregated` (per-minute, with the decision-table's `niv_estimate` and
    its drivers broadcast onto every minute of their settlement period --
    the SP-level table the notebook itself only computes once per period),
    and `generation_by_fuel`. Empty dict if there's no PN data to explode
    yet (e.g. a fresh startup).

    `other_fallback`: see fuel_type_reference()'s own docstring -- opt-in
    (config.py's `fpn_other_fallback_enabled`, dev-only until validated)
    so units tagged BATTERIES/LOAD RESPONSE/GAS/DIESEL/SOLAR/INTERCONNECTOR
    show up under OTHER instead of being silently excluded.
    """
    # Step timings (seconds), returned under "timings" and logged by the runner so a
    # slow cycle shows which step it was.
    timings: dict[str, float] = {}
    _last = [time.perf_counter()]

    def _lap(name: str) -> None:
        now_ = time.perf_counter()
        timings[name] = round(now_ - _last[0], 2)
        _last[0] = now_

    fuel_ref = fuel_type_reference(bm_unit_reference, other_fallback=other_fallback)
    sets = unit_sets(fuel_ref)

    merged = explode_and_merge(pn_df, boalf_df, mel_df, mil_df, fuel_ref, sets)
    _lap("explode_merge")
    timings["explode_merge_detail"] = dict(getattr(merged, "attrs", {}).get("timings", {}))
    if merged.empty:
        return {}

    by_unit, by_fuel = aggregate_by_fuel(merged, fuel_ref)
    _lap("aggregate_by_fuel")
    worst_deviants = compute_worst_deviants(by_unit, now=now)
    _lap("worst_deviants")
    trips, trip_recoveries, trip_state = detect_trips(by_unit, trip_state or {}, now=now, seed_only=trip_seed_only)
    _lap("detect_trips")
    cur_period = current_period(now)
    wb_series = worst_behaviour_series(
        by_unit, trip_state, window_around(cur_period.settlement_date, cur_period.settlement_period),
        now or datetime.now(timezone.utc),
    )
    _lap("wb_series")

    bm_stack = pd.DataFrame()
    if bm_stack_enabled:
        cur = current_period(now)
        bm_stack = compute_bm_stack(
            by_unit, bod_df, fuel_ref, bm_unit_reference,
            window_around(cur.settlement_date, cur.settlement_period),
            exclude_fuel_prefix=INTERCONNECTOR_FUEL_PREFIX,
        )
    _lap("bm_stack")

    spot_times = merged[["settlementDate", "settlementPeriod", "spot_time"]].drop_duplicates()
    interconnectors = interconnector_rows(pn_df, fuel_ref, sets, spot_times)
    natgrid = natgrid_trade_rows(neso_trades, disbsad_df, spot_times)
    _lap("interconnectors_natgrid")

    fuelinst_df = pd.DataFrame(fuelinst_records)
    start, end = demand_window
    demand = build_demand_frame(ndf, tsdf, indo, itsdo, da_ndf, start, end)
    _lap("demand")

    # Everything above is the heavy per-unit work. What follows depends on the pricing
    # stack's saved rows, which change far more often -- so it is a separate function
    # (compute_tail) that FpnRunner can re-run on its own, from this saved state, the
    # moment the stack saves, instead of redoing the per-unit work.
    tail_state = {
        "by_fuel": by_fuel, "fuelinst_df": fuelinst_df, "interconnectors": interconnectors, "natgrid": natgrid,
        "demand": demand, "fuel_ref": fuel_ref, "fuelinst_records": fuelinst_records,
    }
    tail = compute_tail(
        tail_state, pricing_stack_delta_rows, pricing_stack_niv_rows, pricing_stack_niv_spot_time_rows,
        pricing_stack_unit_delta_5min_rows,
    )
    by_fuel, aggregated, generation_by_fuel = tail["by_fuel"], tail["aggregated"], tail["generation_by_fuel"]
    timings["tail"] = tail["timings"]

    return {
        "timings": timings,
        "tail_state": tail_state,
        "by_fuel": by_fuel,
        "worst_deviants": worst_deviants,
        "aggregated": aggregated,
        "generation_by_fuel": generation_by_fuel,
        "trips": trips,
        "trip_recoveries": trip_recoveries,
        "trip_state": trip_state,
        "wb_series": wb_series,
        "bm_stack": bm_stack,
    }
