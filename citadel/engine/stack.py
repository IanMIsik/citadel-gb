"""Core pricing-stack computation engine -- a cleaned, faithful port of the
original notebook's per-cycle pandas pipeline
(pricing_stack_development_continous.ipynb, cells 3-4 and the main loop in
cell 10).

This module is deliberately literal about the arithmetic: it reproduces the
same sequence of explode/merge/groupby steps as the original rather than a
fresh reinterpretation of Elexon's imbalance-price methodology, because the
whole point of this port is to preserve the ~90%-accurate-against-Elexon
behaviour of the original tool while changing everything *around* it
(polling -> streaming, Google Sheets -> Postgres). A handful of safe,
behaviour-preserving cleanups were made (vectorizing the per-row marginal-
delta loop, using proper Europe/London tz conversion instead of a whole-hour
UTC-relabeling trick) -- both produce numerically identical output to the
original, just without its Python-loop / label-mismatch mechanics.

Three discrepancies against a literal reading of Elexon's imbalance guide
were found while porting this and are surfaced as toggles rather than
silently fixed -- see `include_case_6` and `use_mel_gate` below, and the
module docstring note on MIL. Use scripts/backtest_sp.py to A/B test each
one against real settlement prices.

Pipeline:
  1. explode acceptances/PN/MEL from (timeFrom, timeTo, levelFrom, levelTo)
     ramps into one row per minute (`spot_time`), centre-of-minute linear
     interpolation.
  2. join accepted volume (BOALF) to baseline volume (PN) per minute,
     derive each row's `marginal_delta` against the *previous* accepted
     level for that unit/minute.
  3. join to BOD to find the bid/offer ladder around that band, and
     classify the marginal delta into one of six geometric cases (which
     band(s) it actually spans) to get `vol_to_price`.
  4. blend in DISBSAD (non-BM balancing actions / STOR) as synthetic rows.
  5. build the settlement-period price stack: rank every row by price
     (direction-aware), then derive the PAR-weighted (Price Averaging
     Reference, 1 MWh) marginal price the way Elexon's own System Price
     methodology works -- only actions on the same side as total NIV count,
     and only the last PAR MWh of cumulative volume nearest NIV sets price.

MIL (Maximum Import Limit) is fetched by ingest/elexon_rest.py for dataset
parity but -- matching the original notebook exactly -- is never actually
used anywhere in this pipeline (only MEL gates the BOALF/PN join). Import-
direction ramp constraints (batteries/pumped-storage charging) aren't
applied. Worth investigating as an accuracy lever; not changed here.
"""

from __future__ import annotations

import time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

LONDON = ZoneInfo("Europe/London")

STACK_COLUMNS = [
    "settlementDate", "settlementPeriod", "max_ta", "bmUnit", "storFlag", "deemedBoFlag",
    "soFlag", "cadlFlag", "acceptanceNumber", "reversal", "ap_mult_vol", "delta", "vol_to_price",
    "disbsad_cost", "m_orig_price", "total_delta", "delta_sign", "action_sign",
    "vol_for_price", "misik_imb_price", "total_misik_price",
]

# The pricing stack's own genuinely per-minute NIV trajectory -- the
# original notebook's `spot_niv` (pricing_stack_development_continous.ipynb,
# cell 10: `fpn_boalf_disbsad_spot.groupby([...,'spot_time'])['delta'].sum()`),
# handed to the sibling FPN notebook via `spot_niv.pkl` as `misik_cast_delta`
# and merged onto its own per-minute aggregated table there. Distinct from
# `total_delta`/`niv_sp_max` above (STACK_COLUMNS's per-acceptance summary,
# collapsed to one MWh figure per settlement period) -- this is the SAME
# underlying reversal/CADL-aware `delta` build_price_stack() uses, just
# grouped by minute instead of by acceptance, and confirmed already in MW
# (not MWh): each row of `combined` already represents one bmUnit's accepted
# volume for exactly that one minute, so summing across bmUnits at a given
# spot_time needs no /60 or *2 conversion the way the acceptance-level
# figures do.
SPOT_NIV_COLUMNS = ["settlementDate", "settlementPeriod", "spot_time", "niv_spot_time_max"]

# Same source, kept per-bmUnit and bucketed onto FUELINST's own 5-minute
# grid instead of summed system-wide across the whole minute -- see
# spot_time_bm_unit_delta_5min()'s own docstring for what this feeds.
UNIT_DELTA_5MIN_COLUMNS = ["settlementDate", "settlementPeriod", "bmUnit", "startTime", "delta"]

# Raw, unbucketed per-minute counterpart of UNIT_DELTA_5MIN_COLUMNS -- feeds
# the "All Plants Exploded BOALF" chart (engine/fpn.py's
# exploded_boalf_with_fuel_type()), which needs the real per-minute
# trajectory the reference app's own reference chart plots, not a 5-minute average.
EXPLODED_BOALF_COLUMNS = ["settlementDate", "settlementPeriod", "bmUnit", "spot_time", "delta"]


def custom_round(x: float, base: int = 5) -> int:
    return int(base * round(float(x) / base))


def _settlement_period_col(spot_time_utc: pd.Series) -> pd.Series:
    """Settlement period (1-based, 30-minute) for a UTC-aware instant --
    derived via proper Europe/London conversion so this is correct across
    the BST transition, unlike a fixed offset.
    """
    local = spot_time_utc.dt.tz_convert(LONDON)
    return local.dt.hour * 2 + (local.dt.minute // 30) + 1


def vectorized_exploder(df: pd.DataFrame) -> pd.DataFrame:
    """One row per minute between timeFrom/timeTo, linearly interpolating
    levelFrom -> levelTo, sampled at the centre of each minute.
    """
    if df.empty:
        return pd.DataFrame(columns=[*df.columns, "spot_time", "spot_level"])

    df = df.copy()
    start = pd.to_datetime(df["timeFrom"], utc=True)
    end = pd.to_datetime(df["timeTo"], utc=True)
    minutes = ((end - start).dt.total_seconds() / 60).astype(int)

    valid = minutes > 0
    if not valid.any():
        return pd.DataFrame(columns=[*df.columns, "spot_time", "spot_level"])

    df = df[valid].reset_index(drop=True)
    start = start[valid].reset_index(drop=True)
    minutes = minutes[valid].reset_index(drop=True)

    exploded = df.loc[df.index.repeat(minutes)].reset_index(drop=True)
    minute_offset = np.concatenate([np.arange(m) for m in minutes])
    spot_time = start.repeat(minutes).reset_index(drop=True) + pd.to_timedelta(minute_offset * 60, unit="s")

    level_from_rep = df["levelFrom"].repeat(minutes).values
    level_to_rep = df["levelTo"].repeat(minutes).values
    total_move = level_to_rep - level_from_rep
    steps = total_move / minutes.repeat(minutes).values
    spot_level = level_from_rep + (minute_offset + 0.5) * steps

    exploded["spot_time"] = spot_time
    exploded["spot_level"] = spot_level.astype(float)
    return exploded.reset_index(drop=True)


def boalf_exploder(boalf_df: pd.DataFrame, overlap_resolution: str = "latest_wins") -> pd.DataFrame:
    """One row per (settlementDate, spot_time, bmUnit[, acceptanceNumber
    in "independent" mode]): the accepted volume in force at that minute.

    `overlap_resolution`:
      - "latest_wins" (default, validated) -- when two acceptances for the
        same bmUnit cover the same minute, the one with the later
        acceptanceTime wins that minute outright; the earlier one
        disappears from that minute onward once superseded (matches the
        original notebook exactly).
      - "independent" -- each acceptance keeps its own governance over its
        own declared timeFrom/timeTo window regardless of whether a later
        acceptance also covers part of it, so two overlapping acceptances
        for the same unit can both produce their own rows for the same
        minute. NOT a standalone top-level mode -- confirmed against real
        settlement data that using this for EVERY row roughly doubles the
        period's total accepted volume (a same-level revision like an
        acceptance re-confirming its predecessor's level counts twice).
        It's an internal building block for
        build_marginal_deltas()'s own "reversal_aware" mode, which uses it
        to find just the specific extra rows worth keeping -- see
        _reversal_tail_rows()'s docstring for the full worked example.

    Ties on `acceptanceTime` are broken by `acceptanceNumber` (assigned in
    true submission order even when the coarser timestamp can't tell two
    acceptances apart) -- confirmed live against Elexon's real settlement
    stack: T_GRAI-7's acceptances 164254/164255 (SP35, 2026-09-23) share
    the identical acceptanceTime 16:01:00Z, and a plain `idxmax()` breaks
    that tie by whichever row happens to come first in fetch order,
    silently dropping one of two genuinely real, separately-priced
    acceptances (164255 alone: -22.5 MWh in Elexon's own ISPSTACK).
    """
    if boalf_df.empty:
        return pd.DataFrame()

    exploded = vectorized_exploder(boalf_df)
    if exploded.empty:
        return pd.DataFrame()

    exploded["acceptanceTime"] = pd.to_datetime(exploded["acceptanceTime"], utc=True)
    exploded = exploded.sort_values(["acceptanceTime", "acceptanceNumber"])

    if overlap_resolution == "independent":
        # Only collapse literal duplicate rows for the SAME acceptance at
        # the SAME minute (e.g. re-fetched identically across overlapping
        # settlement-period queries) -- never collapse a DIFFERENT
        # acceptanceNumber's row just because it shares a minute.
        dedupe_cols = ["settlementDate", "spot_time", "bmUnit", "acceptanceNumber"]
        exploded = exploded.drop_duplicates(subset=dedupe_cols, keep="last")
    else:
        grp1 = ["settlementDate", "settlementPeriodFrom", "settlementPeriodTo", "spot_time", "bmUnit"]
        exploded = exploded.drop_duplicates(subset=grp1, keep="last")
        exploded = exploded.drop_duplicates(subset=["settlementDate", "spot_time", "bmUnit"], keep="last")

    cols_to_drop = [
        "timeFrom", "timeTo", "levelFrom", "levelTo",
        "deemedBoFlag", "soFlag", "storFlag", "rrFlag",
        "settlementPeriodTo", "settlementPeriodFrom",
    ]
    exploded = exploded.drop(columns=[c for c in cols_to_drop if c in exploded.columns])
    exploded = exploded.rename(columns={"spot_level": "boalf_spot_vol"})
    exploded["settlementPeriod"] = _settlement_period_col(exploded["spot_time"])
    return exploded.reset_index(drop=True)


# BSC Continuous Acceptance Duration Limit (guide, "CADL Flagging"): the
# time limit below which a BOA is flagged as a potentially system (rather
# than energy) balancing action. Currently 10 minutes (reduced from 15 on
# 1 April 2019); the Panel can alter it with Ofgem's approval.
CADL_MINUTES = 10


def compute_cadl_flags(boalf_df: pd.DataFrame, cadl_minutes: int = CADL_MINUTES) -> pd.DataFrame:
    """Elexon's REST API doesn't expose a raw CADL flag on the live
    acceptances feed (`/balancing/acceptances/all`) -- confirmed against
    real records, which only carry deemedBoFlag/soFlag/storFlag/rrFlag.
    Elexon DOES publish the real flag, but only on
    `/balancing/settlement/stack/all/{bid|offer}/{date}/{period}`, a
    POST-SETTLEMENT endpoint published well after the period closes --
    unusable for a live pricing tool. So this computes it, validated
    against that real endpoint (see the PR notes: 99.4% agreement, 1806
    real flagged/unflagged acceptances sampled across 9 settlement
    periods on a real trading day).

    The key thing an earlier, reverted attempt got wrong: "duration"
    is NOT how long one specific acceptanceNumber stays the governing one
    for its bmUnit. A single sustained balancing action is routinely
    carried out as a rolling SERIES of short-lived acceptances, each one
    just extending or lightly revising the one before it (confirmed by
    inspecting real chains: e.g. a unit held at one level via acceptances
    handed off every 4-8 minutes for 30+ minutes straight) -- none of
    those get CADL flagged in Elexon's own data, because the *episode*
    they're part of is long, even though each individual acceptance's own
    governing window is short. "Duration" means the length of that
    unbroken episode: how many consecutive minutes a bmUnit has ANY
    accepted volume in force at all, regardless of which acceptanceNumber
    is currently winning -- boalf_exploder() already resolves, minute by
    minute, which acceptance governs (a later one superseding an earlier
    one on overlap); this groups those resolved minutes into runs broken
    only by an actual gap (a minute with no accepted volume for that
    bmUnit at all), and flags every acceptance that appears anywhere in a
    run shorter than `cadl_minutes`.

    An acceptance that never wins even a single minute (superseded by a
    later one before it ever took effect) defaults to NOT flagged --
    empirically it's almost always a revision folded into an ongoing long
    episode, not a genuine isolated short action.

    Returns one row per (bmUnit, acceptanceNumber) with `cadl_flag`.
    """
    exploded = boalf_exploder(boalf_df)
    if exploded.empty:
        return pd.DataFrame(columns=["bmUnit", "acceptanceNumber", "cadl_flag"])

    df = exploded.sort_values(["bmUnit", "spot_time"])[["bmUnit", "spot_time", "acceptanceNumber"]].copy()

    # An episode breaks on an actual time gap (a minute with no accepted
    # volume for this bmUnit) or a change of bmUnit -- NOT on a change of
    # acceptanceNumber, which is the point: a rolling hand-off between
    # acceptances doesn't reset the episode.
    same_unit = df["bmUnit"] == df["bmUnit"].shift()
    consecutive_minute = df["spot_time"].diff().dt.total_seconds() == 60
    episode_continues = same_unit & consecutive_minute
    df["episode_id"] = (~episode_continues).cumsum()

    episode_minutes = df.groupby(["bmUnit", "episode_id"])["spot_time"].transform("size")
    df["cadl_flag"] = episode_minutes < cadl_minutes
    return df.groupby(["bmUnit", "acceptanceNumber"])["cadl_flag"].any().reset_index()


def mel_exploder(mel_df: pd.DataFrame) -> pd.DataFrame:
    if mel_df.empty:
        return pd.DataFrame()
    exploded = vectorized_exploder(mel_df)
    if exploded.empty:
        return pd.DataFrame()

    exploded["notificationTime"] = pd.to_datetime(exploded["notificationTime"], utc=True)
    exploded = exploded.loc[exploded.groupby(["bmUnit", "spot_time"])["notificationTime"].idxmax()]

    cols_to_drop = ["timeFrom", "timeTo", "dataset", "levelFrom", "levelTo", "nationalGridBmUnit",
                     "settlementDate", "settlementPeriod", "notificationTime", "notificationSequence"]
    exploded = exploded.drop(columns=[c for c in cols_to_drop if c in exploded.columns])
    exploded = exploded.rename(columns={"spot_level": "mel_spot_vol"})
    exploded["settlementPeriod"] = _settlement_period_col(exploded["spot_time"])
    return exploded


def mil_exploder(mil_df: pd.DataFrame) -> pd.DataFrame:
    """Same shape as `mel_exploder`, for MIL (Maximum Import Limit) -- used
    by engine/fpn.py's adjusted-FPN clamp for import-direction units. Not
    used anywhere in this module's own pricing pipeline (see module
    docstring on MIL).
    """
    exploded = mel_exploder(mil_df)
    if exploded.empty:
        return exploded
    return exploded.rename(columns={"mel_spot_vol": "mil_spot_vol"})


def fpn_exploder(pn_df: pd.DataFrame) -> pd.DataFrame:
    """Unlike boalf_exploder() (latest-acceptanceTime-wins per minute) and
    mel_exploder() (latest-notificationTime-wins per minute), this had NO
    per-minute consolidation at all -- confirmed as the actual cause of a
    real, live ~23 MWh SP24 NIV discrepancy (2026-09-29): engine/runner.py's
    WindowBuffers.add() (the IRIS live-push path) only dedupes whole-record
    duplicates, so a PN revision that re-declares an overlapping window
    with DIFFERENT levels for the same bmUnit accumulates ALONGSIDE the
    stale one rather than replacing it -- confirmed directly for
    T_KEAD-2/acceptance 46124: the full pipeline (exploder through
    build_price_stack), run in isolation against freshly-fetched real
    PN/BOD/MEL with no duplicates, reproduces Elexon's own settled 81.08
    MWh exactly, while the live system (whose PN buffer had accumulated
    across multiple IRIS revisions) reported 190.00 for the same
    acceptance/period -- a ~2.3x inflation consistent with
    build_marginal_deltas()'s own `pd.merge(exploded_boalf_df,
    exploded_pn_df, how="inner", on=["bmUnit","spot_time"])` fanning out
    into one row per duplicate PN candidate for the same minute, each
    carrying a different `fpn_spot_vol` baseline, instead of exactly one.

    PN's raw feed carries no revision/publish timestamp (unlike BOALF's
    acceptanceTime or MEL's notificationTime) to sort by -- arrival order
    into WindowBuffers.add() is used as the practical "latest wins" proxy
    instead (IRIS delivers messages in real chronological order, and
    pandas preserves row order through vectorized_exploder's own
    index.repeat()), via drop_duplicates(keep="last").
    """
    if pn_df.empty:
        return pd.DataFrame()
    exploded = vectorized_exploder(pn_df)
    exploded = exploded.drop(columns=["timeFrom", "timeTo", "dataset", "levelFrom", "levelTo",
                                       "nationalGridBmUnit", "settlementDate", "settlementPeriod"], errors="ignore")
    exploded = exploded.rename(columns={"spot_level": "fpn_spot_vol"})
    exploded = exploded.drop_duplicates(subset=["bmUnit", "spot_time"], keep="last")
    exploded["settlementPeriod"] = _settlement_period_col(exploded["spot_time"])
    return exploded


def _detect_acceptance_reversals(fpn_all_actions: pd.DataFrame) -> pd.DataFrame:
    """Genuine, ACCEPTANCE-granular reversal detection -- deliberately
    separate from the per-minute `band_reversal` signal above (see that
    docstring for why per-minute is the wrong granularity: a single
    acceptance's own ramp segments routinely go up then back down, which
    is not a "reversal" in the BSC sense).

    Within each (bmUnit, settlement period), order the DISTINCT governing
    acceptances by acceptanceTime. Each acceptance's own net effect is the
    level it leaves the unit at by the last minute it governs *in this
    period*, compared to whatever level was in force right before it
    started (the previous acceptance's own resulting level, or the FPN
    baseline for the period's first acceptance). An acceptance is a
    reversal (`reversal == -1`) if that net effect moves opposite to the
    direction the period's first acceptance set -- i.e. "an offer accepted
    then later reversed, partially or fully, by a subsequent acceptance."

    Returns one row per (bmUnit, settlementDate, settlementPeriod,
    acceptanceNumber): `reversal` (+1/-1), `origin_acceptance_number`
    (the period's first acceptance -- whose SO/STOR/deemed-BO flags a
    reversal should be priced under instead of its own, per BSC
    convention that the flag travels with the action being undone), and
    `base_before` (the level in force right before this acceptance started
    -- see build_marginal_deltas()'s `reversal_side_fix` param for what
    this feeds).
    """
    period_cols = ["bmUnit", "settlementDate", "settlementPeriod"]
    acc_cols = [*period_cols, "acceptanceNumber"]

    ordered = fpn_all_actions.sort_values("spot_time")
    # This acceptance's own resulting level, as observed within this
    # period (its ramp may continue past the period boundary -- only the
    # level it leaves behind here matters for this period's sequence).
    end_level = ordered.groupby(acc_cols, as_index=False).last()[[*acc_cols, "acceptanceTime", "boalf_spot_vol"]]
    start_fpn = ordered.groupby(acc_cols, as_index=False).first()[[*acc_cols, "fpn_spot_vol"]]
    acc = pd.merge(end_level, start_fpn, on=acc_cols)
    acc = acc.sort_values([*period_cols, "acceptanceTime"])

    acc["base_before"] = acc.groupby(period_cols)["boalf_spot_vol"].shift(1)
    acc["base_before"] = acc["base_before"].fillna(acc["fpn_spot_vol"])
    acc["direction"] = np.sign(acc["boalf_spot_vol"] - acc["base_before"])

    first_acc = acc.groupby(period_cols, as_index=False).first()[[*period_cols, "acceptanceNumber", "direction"]]
    first_acc = first_acc.rename(columns={"acceptanceNumber": "origin_acceptance_number", "direction": "initial_direction"})

    acc = pd.merge(acc, first_acc, on=period_cols)
    acc["reversal"] = np.sign(acc["direction"] * acc["initial_direction"])
    acc.loc[acc["reversal"] == 0, "reversal"] = 1.0  # no net movement is not a reversal
    return acc[[*acc_cols, "reversal", "origin_acceptance_number", "base_before"]]


def _reversal_tail_rows(
    boalf_df: pd.DataFrame, exploded_pn_df: pd.DataFrame, exploded_boalf_latest: pd.DataFrame, minute_cols: list[str]
) -> pd.DataFrame:
    """For `overlap_resolution="reversal_aware"`: the extra rows a
    superseded acceptance still contributes.

    Two hypotheses were tested against Elexon's real settlement stack
    (/balancing/settlement/stack/all) for a worked example -- T_CDCL-1,
    SP35 2026-09-20, acceptance 183621 (flat at 190MW, superseded by a
    later acceptance at minute 16:20, while FPN briefly rose above 190
    between 16:26-16:29, which "latest_wins" can never see since 183621
    has already fully disappeared from the reconstruction by then):

    1. Every acceptance counts fully and independently over its own
       declared window, superseded or not (`boalf_exploder`'s own
       "independent" mode). Confirmed WRONG: it reproduces 183621's -1.9
       MWh exactly, but it also makes an EARLIER acceptance that's simply
       revised to an identical level (183620 -> 183621, both flat at 190,
       a no-op revision) count TWICE, and the total accepted volume for
       the period came out ~84% too high against Elexon's own aggregate
       (totalAcceptedOfferVolume) -- a real, large regression.
    2. A superseded acceptance's own remaining declared window ("tail")
       only contributes a row for the minutes where its own marginal
       delta's SIGN differs from the sign it had at its own first
       governed minute (a genuine reversal relative to its own original
       direction) -- confirmed EXACT: -1.9 MWh for 183621, and it
       correctly produces NOTHING for 183620's tail (same level the whole
       time, sign never differs, so no double-count).

    This implements hypothesis 2. Returns rows shaped like
    `fpn_all_actions` (bmUnit/spot_time/boalf_spot_vol/fpn_spot_vol/... )
    for exactly those reversal-tail minutes; empty if none exist.
    """
    exploded_full = boalf_exploder(boalf_df, overlap_resolution="independent")
    if exploded_full.empty:
        return pd.DataFrame()
    full_fpn = pd.merge(exploded_full, exploded_pn_df, how="inner", on=minute_cols)
    full_fpn = full_fpn.drop_duplicates()
    if full_fpn.empty:
        return pd.DataFrame()

    governing = exploded_boalf_latest[[*minute_cols, "acceptanceNumber"]].rename(
        columns={"acceptanceNumber": "_governing_acceptance"}
    )
    full_fpn = pd.merge(full_fpn, governing, on=minute_cols, how="left")

    # Anchored PER SETTLEMENT PERIOD, not across the acceptance's whole
    # declared window: BSC acceptance volumes are per period, and a window
    # that spans a period boundary can legitimately flip sign there when
    # the FPN baseline itself moves (confirmed 2026-10-02 SP21,
    # T_WBURB-1/-3: acceptances 159190/163429 were raised against a 385 MW
    # FPN in SP20 -- marginal negative -- then ran on into SP21 where the
    # unit's FPN is 0 -- marginal positive. Anchored to SP20's negative
    # sign, SP21's ordinary positive ramp-down looked like a "reversal"
    # and was added on top of the governing acceptance, +23.2 MWh per
    # unit, ~46 MWh of spurious NIV against Elexon's 100.35). Periods are
    # 30-minute aligned in UTC too, so a UTC floor is a safe period key.
    full_fpn["_sp_key"] = full_fpn["spot_time"].dt.floor("30min")
    anchor_cols = ["bmUnit", "acceptanceNumber", "_sp_key"]
    # The FIRST NON-ZERO deviation, not literally the first minute: an
    # acceptance routinely starts with a run of minutes at exactly its FPN
    # baseline (e.g. held at 0 while FPN is also 0, before FPN starts
    # ramping away) -- anchoring to a genuinely zero initial_sign would
    # make ANY later deviation, in either direction, look like a
    # "reversal" of nothing, which was confirmed to wrongly re-add an
    # already-governing acceptance's own ordinary (non-reversing) volume
    # once it's later superseded (T_GLNDO-1 SP15 2026-09-18, acceptance
    # 115979 -- caused a real regression before this fix).
    full_fpn["_marginal"] = full_fpn["boalf_spot_vol"] - full_fpn["fpn_spot_vol"]
    first_deviation = full_fpn[full_fpn["_marginal"] != 0].sort_values("spot_time").groupby(anchor_cols, as_index=False).first()
    first_deviation["_initial_sign"] = np.sign(first_deviation["_marginal"])
    full_fpn = pd.merge(full_fpn, first_deviation[[*anchor_cols, "_initial_sign"]], on=anchor_cols, how="left")
    full_fpn["_initial_sign"] = full_fpn["_initial_sign"].fillna(0.0)  # never deviates from FPN at all

    marginal = full_fpn["_marginal"]
    is_governing = full_fpn["acceptanceNumber"] == full_fpn["_governing_acceptance"]
    sign_differs = np.sign(marginal) != full_fpn["_initial_sign"]
    tail_mask = (~is_governing) & sign_differs & (marginal != 0)

    return full_fpn[tail_mask].drop(columns=["_governing_acceptance", "_initial_sign", "_marginal", "_sp_key"]).reset_index(drop=True)


def build_marginal_deltas(
    boalf_df: pd.DataFrame, pn_df: pd.DataFrame, overlap_resolution: str = "reversal_aware",
    reversal_side_fix: bool = False,
) -> pd.DataFrame:
    """Per-minute accepted volume vs. its own previous accepted level
    (`base_value`): how much *this* acceptance moved the unit by, and
    whether that move continued or reversed the unit's very first
    deviation from its FPN baseline (`initial_delta_sign` /
    `band_reversal`) -- this is what feeds allocate_volumes()'s six-case
    BOD-band classification, and is unrelated to the genuine acceptance-
    level `reversal` signal added below.

    `overlap_resolution="latest_wins"` (default): `group_cols` is
    `["bmUnit", "spot_time"]`, and boalf_exploder() has already collapsed
    each minute down to exactly one governing acceptance before this
    function runs -- so this grouping always produces singleton groups,
    `base_value` always falls back to the FPN baseline, and `band_reversal`
    always evaluates to +1 (sign of a square). That's intentional: this is
    the exact behaviour every backtested accuracy number in this project
    (see scripts/backtest_sp.py) is pinned against, and changing it to
    compare consecutive minutes instead was tried and confirmed to regress
    accuracy (ordinary ramp-direction changes within one acceptance's own
    linear interpolation get misread as reversals at minute granularity).

    `overlap_resolution="reversal_aware"`: keeps the exact same
    "latest_wins" rows as the primary layer (this is what the aggregate
    accepted-volume totals are validated against -- see
    _reversal_tail_rows()'s own docstring on why a simpler "every
    acceptance counts independently" version was tried and rejected), and
    ADDS extra rows for a superseded acceptance's own remaining declared
    window wherever ITS OWN marginal delta genuinely reverses sign
    relative to its own original direction (see _reversal_tail_rows()).
    Both the primary and the extra rows are priced directly against FPN
    (`base_value = fpn_spot_vol`, `band_reversal` hardcoded to +1, so
    cases 3-6 stay out of this -- that's still a separate, unfixed gap);
    the extra rows' own sign is what routes them into the correct
    (usually opposite-direction) BOD band via allocate_volumes()'s
    existing case 1/2 logic, no reversal-specific case needed.

    `reversal_side_fix` (accepted for backward compat with config.py's
    `reversal_side_fix_enabled`, but currently a NO-OP -- see below):
    original intent was to price a genuine reversal (per
    `_detect_acceptance_reversals()`) against its own `base_before` (the
    level in force right before it started) instead of FPN, since Elexon
    prices T_FERRB-1 acceptance 14433 (SP14 2026-09-24) as a +2.87 MWh
    offer @ GBP165.42 while this engine's FPN-relative model gives -0.58
    MWh bid @ GBP135.42 -- a genuine wrong-side-of-stack miss, confirmed on
    45/370 reversal-flagged acceptances sampled that day.

    THREE ATTEMPTS AT A FIX, ALL CONFIRMED WORSE LIVE, root cause is NOT
    where it first looked:
    1. Override `base_value` to `base_before` only for reversal-flagged
       rows: `allocate_volumes()`'s case_1/case_2 masks need `base_value`
       FPN-relative (`lower_bound`/`higher_bound` are built from
       `fpn_spot_vol`); overriding it breaks that for every flagged row,
       most of which are ordinary continuing bids/offers, not genuine
       cross-side cases -- their volume silently vanished instead of being
       re-priced. NIV error got worse every period tested (SP14 4.6->148
       MWh; SP15 1.2->22.7; SP16 4.3->6.6; SP17 0.3->20.3).
    2. Re-anchor `lower_bound`/`higher_bound` to `base_value` too, so the
       band geometry stays internally consistent with whichever baseline
       priced the row: also confirmed worse (SP14 4.6->210; SP15 1.2->56;
       SP18 0.08->28) -- the geometry was never the actual bug.
    3. Price EVERY row (not just reversal-flagged ones) against its own
       `base_before` instead of FPN, on the theory that "how much this
       acceptance itself moved the unit" is more physically meaningful
       than "how far the unit sits from FPN": far worse again, and this
       time the period TOTAL flipped sign (SP15 actual +167.7 MWh vs
       computed -97.7; SP16 +146.7 vs -122.6; SP17 +71.2 vs -120.5).
       FPN-relative-per-governing-minute is the physically correct measure
       of delivered-vs-planned volume for ordinary continuing revisions;
       it's only wrong for the rare genuine cross-side case.

    A pairId-availability theory was also chased and ruled out empirically,
    not just assumed: BOD's own pairId=-1 row for T_FERRB-1 SP14 carries
    BOTH prices (bid=135.42, offer=165.42) for the same band, and the real
    ISPSTACK confirms acceptances 14433/14434 (the offers) reference that
    SAME pairId=-1, not a different one -- so `allocate_volumes()`'s
    existing `ap` formula (offer price if `sign(marginal_delta)>0`, bid
    price otherwise) already resolves bid-vs-offer correctly for ANY row
    that reaches the right pairId band; no per-acceptance pairId field is
    needed for that part. The real puzzle is `volume` magnitude, not
    pairId: hand-computing 14433's physical deviation from FPN over its
    actual governing window (bmUnit level ramps 0->70 while FPN holds flat
    at 70, from the real BOALF chain fetched live 2026-09-24) gives ~-0.58
    MWh -- matching this engine's current (unfixed) output almost exactly,
    i.e. our number IS a correct measure of *something real*, just not the
    same quantity Elexon's ISPSTACK `volume` field reports (+2.87 MWh,
    neither this number's negation nor a rescaling of it). That mismatch in
    kind, not just sign, means Elexon's real per-acceptance settlement
    volume follows an allocation rule (likely involving cross-period
    continuation -- 14434's own declared window runs past this period's
    boundary into SP15 -- or acceptance-sequencing detail not reflected in
    a per-minute "latest wins across this period" model) that isn't
    reverse-engineerable from BOALF/BOD/PN geometry alone, however it's
    sliced. Fixing this for real needs either the actual documented BSC
    settlement algorithm for acceptance volume allocation (not just the
    Imbalance Pricing Guidance already implemented in imbalance_price.py,
    which covers NIV/price, not per-acceptance volume attribution), or
    ingesting Elexon's post-settlement ISPSTACK directly as a reconciliation
    pass over completed periods (it carries the true per-acceptance
    `volume` and `bidOfferPairId`, but only after a settlement delay, so it
    can correct completed periods, not the live in-progress view).
    """
    exploded_boalf_df = boalf_exploder(boalf_df)
    if exploded_boalf_df.empty:
        return pd.DataFrame()

    unit_list = boalf_df["bmUnit"]
    usable_pn = pn_df[pn_df["bmUnit"].isin(unit_list)]
    exploded_pn_df = fpn_exploder(usable_pn)
    if exploded_pn_df.empty:
        return pd.DataFrame()

    minute_cols = ["bmUnit", "spot_time"]
    # Both exploders compute their own 'settlementPeriod' identically from
    # spot_time (see _settlement_period_col) -- dropping one side here
    # avoids pandas silently suffixing both into settlementPeriod_x/_y
    # (no bare 'settlementPeriod' surviving) purely because the column name
    # collides outside the join key, not because the values differ.
    exploded_pn_df = exploded_pn_df.drop(columns=["settlementPeriod"])
    fpn_all_actions = pd.merge(exploded_boalf_df, exploded_pn_df, how="inner", on=minute_cols)
    fpn_all_actions = fpn_all_actions.drop_duplicates()
    if fpn_all_actions.empty:
        return pd.DataFrame()

    if overlap_resolution == "reversal_aware":
        tail_rows = _reversal_tail_rows(boalf_df, exploded_pn_df, exploded_boalf_df, minute_cols)
        df = pd.concat([fpn_all_actions, tail_rows], ignore_index=True) if not tail_rows.empty else fpn_all_actions.copy()
        df["initial_delta_sign"] = 1.0
        df["base_value"] = df["fpn_spot_vol"]
    else:
        group_cols = minute_cols
        joiner = exploded_boalf_df.groupby(group_cols)["acceptanceTime"].min().reset_index()
        initial_delta = pd.merge(joiner, fpn_all_actions)
        initial_delta["initial_delta_sign"] = np.sign(initial_delta["boalf_spot_vol"] - initial_delta["fpn_spot_vol"])
        initial_delta = initial_delta[["bmUnit", "spot_time", "initial_delta_sign"]].copy()

        df = pd.merge(fpn_all_actions, initial_delta)
        df["rank"] = df.groupby(group_cols)["acceptanceTime"].rank()
        df = df.sort_values("acceptanceTime")

        prev = df.copy()
        prev["rank"] = prev["rank"] + 1
        prev = prev.rename(columns={"boalf_spot_vol": "base_value"})[["bmUnit", "spot_time", "rank", "base_value"]]
        df = pd.merge(df, prev, how="left")
        df["base_value"] = df["base_value"].fillna(df["fpn_spot_vol"])
        df = df.sort_values(["bmUnit", "spot_time", "acceptanceTime"])
        df = df.drop(columns=["rank"])

    # Vectorized equivalent of the original notebook's row-by-row
    # marginal_delta_calc() Python loop -- same formula, no cross-row
    # dependency, so this is a pure performance change.
    df["marginal_delta"] = df["boalf_spot_vol"] - df["base_value"]
    df["init_dir_agreement"] = np.sign(df["marginal_delta"]) * df["initial_delta_sign"]
    df["band_reversal"] = 1.0 if overlap_resolution == "reversal_aware" else np.sign(df["marginal_delta"] * df["initial_delta_sign"])

    reversal_info = _detect_acceptance_reversals(fpn_all_actions)
    df = pd.merge(df, reversal_info, on=["bmUnit", "settlementDate", "settlementPeriod", "acceptanceNumber"], how="left")
    df["reversal"] = df["reversal"].fillna(1.0)
    df["origin_acceptance_number"] = df["origin_acceptance_number"].fillna(df["acceptanceNumber"])

    return df.drop(columns=["base_before"], errors="ignore")


def merge_mel_gate(fpn_boalf_df: pd.DataFrame, mel_df: pd.DataFrame, use_mel_gate: bool = True) -> pd.DataFrame:
    """The original inner-joins every row against exploded MEL data even
    though MEL's own value is never read again afterward -- it acts purely
    as a gate: any accepted-volume minute with no matching MEL row for that
    same bmUnit+minute is silently dropped from the stack. That's a
    plausible source of the "missing actions" the original tool sometimes
    shows. `use_mel_gate=False` skips the gate (keeps every row instead) so
    scripts/backtest_sp.py can A/B test whether that recovers accuracy.
    """
    if fpn_boalf_df.empty:
        return fpn_boalf_df
    if not use_mel_gate:
        return fpn_boalf_df.drop_duplicates()

    unit_list = fpn_boalf_df["bmUnit"].unique()
    mel_df = mel_df[mel_df["bmUnit"].isin(unit_list)].drop_duplicates()
    exploded_mel_df = mel_exploder(mel_df)
    if exploded_mel_df.empty:
        return pd.DataFrame()
    # fpn_boalf_df already carries its own 'settlementPeriod' (from
    # build_marginal_deltas) -- drop MEL's identical, independently
    # recomputed one rather than let pandas silently suffix both into
    # settlementPeriod_x/_y (see build_marginal_deltas' own comment on the
    # same pattern).
    exploded_mel_df = exploded_mel_df.drop(columns=["settlementPeriod"])
    merged = pd.merge(fpn_boalf_df, exploded_mel_df, how="inner", on=["bmUnit", "spot_time"])
    return merged.drop_duplicates()


def build_bod_ladder(bod_df: pd.DataFrame, unit_list) -> pd.DataFrame:
    """Attaches each bid/offer pair's FOUR neighbouring bands each side
    (levelTo1/2/3/4) so the six-case allocation below can tell which price
    band(s) a marginal delta actually spans, not just its own band.

    Four, not three: confirmed against the BSC's own documented limit (a
    BM unit may submit at most 10 Bid-Offer Pairs total -- 5 per side,
    pairId +-1 through +-5), so the 5th (outermost, and entirely ordinary
    -- not a rare edge case) band on either side needs its FULL cumulative
    lower/higher bound computed from all 4 bands before it. A previous
    version of this function only went up to 3 (`range(1, 4)`), which is
    enough for bands 1-4 but silently drops the nearest band's own level
    for band 5 specifically -- confirmed live, T_LIONB-3/SP22 2026-09-30:
    band -5's lower_bound computed as -25 (summing only bands -2/-3/-4)
    instead of the correct -35 (summing -1/-2/-3/-4), making band -5's own
    range overlap band -4's instead of starting where it ends, and
    allocate_volumes() then attributed 5 MWh of real volume to band -5's
    own price when Elexon's real settlement never allocates anything there
    at all for that acceptance (confirmed against the real ISPSTACK: every
    real row for this unit/period has parAdjustedVolume 0, and band -5
    never appears in the accepted stack).
    """
    bod_df = bod_df[bod_df["bmUnit"].isin(unit_list)]
    bod_df = bod_df[["settlementDate", "settlementPeriod", "bmUnit", "bid", "offer", "levelTo", "pairId"]].copy()

    for x in range(1, 5):
        temp = bod_df[["settlementDate", "settlementPeriod", "bmUnit", "levelTo", "pairId"]].copy()
        temp["pairId"] = temp["pairId"] + np.sign(temp["pairId"]) * x
        temp = temp.rename(columns={"levelTo": f"levelTo{x}"})
        bod_df = pd.merge(bod_df, temp, how="left")
        bod_df[f"levelTo{x}"] = bod_df[f"levelTo{x}"].fillna(0)

    return bod_df


def allocate_volumes(fpn_mel_boalf: pd.DataFrame, bod_df: pd.DataFrame, include_case_6: bool = False) -> pd.DataFrame:
    """For every accepted marginal delta, work out which bid/offer band(s)
    it actually spans on the BOD ladder (six geometric cases: forward move,
    reverse move, and moves that cross the FPN line either direction, with
    or without a "reversal" of the unit's very first deviation) and how
    much volume to price at that band's rate (`vol_to_price`).

    `include_case_6` defaults to False to match the original notebook's
    behaviour exactly: it computes case_6 (a negative reversal that passes
    fully through into non-reversal territory) but never includes it in
    the final concatenated result (`case_5`, its positive-direction
    mirror, *is* included). That asymmetry looks like an unintentional
    omission rather than a deliberate exclusion, and is a second plausible
    source of the ~10% gap against Elexon's real settlement price -- flip
    this to True to A/B test it.
    """
    if fpn_mel_boalf.empty:
        return pd.DataFrame()

    unit_list = fpn_mel_boalf["bmUnit"].unique()
    bod = build_bod_ladder(bod_df, unit_list)

    fpn_mel_boalf = fpn_mel_boalf.copy()
    fpn_mel_boalf["settlementDate"] = pd.to_datetime(fpn_mel_boalf["settlementDate"], utc=True)
    bod["settlementDate"] = pd.to_datetime(bod["settlementDate"], utc=True)

    temp_delta = fpn_mel_boalf[[
        "bmUnit", "settlementDate", "settlementPeriod", "acceptanceTime", "spot_time",
        "marginal_delta", "base_value", "band_reversal", "fpn_spot_vol", "boalf_spot_vol",
    ]].copy()

    dp = pd.merge(bod, temp_delta)
    if dp.empty:
        return pd.DataFrame()

    dp["ap"] = (np.sign(dp["marginal_delta"]) + 1) * dp["offer"] * 0.5 + (np.sign(dp["marginal_delta"]) * -1 + 1) * dp["bid"] * 0.5
    dp = dp[dp["marginal_delta"] != 0].copy()
    dp["ab_ex"] = abs(dp["pairId"])
    dp = dp.drop(columns=["bid", "offer"])
    dp["dir"] = np.sign(dp["marginal_delta"])

    sum_of_prev_vb = dp["levelTo1"] + dp["levelTo2"] + dp["levelTo3"] + dp["levelTo4"]
    dp["lower_bound"] = sum_of_prev_vb + dp["fpn_spot_vol"]
    dp["higher_bound"] = dp["lower_bound"] + dp["levelTo"]

    # Case 1: positive action (offer accepted), no reversal.
    mask = (dp["dir"] == 1) & (dp["band_reversal"] > -1) & (dp["boalf_spot_vol"] >= dp["lower_bound"]) & (dp["base_value"] < dp["higher_bound"])
    case_1 = dp[mask].copy()
    case_1["c"] = 1
    bop_vol_already_passed = np.maximum(case_1["base_value"] - case_1["lower_bound"], 0)
    space_in_this_level = case_1["higher_bound"] - case_1["lower_bound"] - bop_vol_already_passed
    vol_priced_by_earlier_bop = np.maximum(case_1["lower_bound"] - case_1["base_value"], 0)
    case_1["vol_to_price"] = np.minimum(case_1["marginal_delta"] - vol_priced_by_earlier_bop, space_in_this_level)

    # Case 2: negative action (bid accepted), no reversal.
    mask = (dp["dir"] == -1) & (dp["band_reversal"] > -1) & (dp["boalf_spot_vol"] <= dp["lower_bound"]) & (dp["base_value"] >= dp["higher_bound"])
    case_2 = dp[mask].copy()
    case_2["c"] = 2
    bop_vol_already_passed = np.minimum(case_2["base_value"] - case_2["lower_bound"], 0)
    space_in_this_level = case_2["higher_bound"] - case_2["lower_bound"] - bop_vol_already_passed
    vol_priced_by_earlier_bop = np.minimum(case_2["lower_bound"] - case_2["base_value"], 0)
    case_2["vol_to_price"] = -1 * np.maximum(case_2["marginal_delta"] - vol_priced_by_earlier_bop, space_in_this_level)

    # Case 3: positive action, WITH reversal.
    mask = (dp["dir"] == 1) & (dp["band_reversal"] == -1) & (np.sign(dp["pairId"]) == -1) & (dp["boalf_spot_vol"] >= dp["higher_bound"]) & (dp["base_value"] <= dp["lower_bound"])
    case_3 = dp[mask].copy()
    case_3["c"] = 3
    min_stage_1 = np.minimum(case_3["lower_bound"] - case_3["base_value"], case_3["marginal_delta"])
    case_3["vol_to_price"] = np.minimum(min_stage_1, case_3["boalf_spot_vol"] - case_3["higher_bound"])

    # Case 4: negative action, WITH reversal.
    mask = (dp["dir"] == -1) & (dp["band_reversal"] == -1) & (np.sign(dp["pairId"]) == 1) & (dp["boalf_spot_vol"] >= dp["lower_bound"]) & (dp["base_value"] <= dp["higher_bound"])
    case_4 = dp[mask].copy()
    case_4["c"] = 4
    case_4["vol_to_price"] = abs(np.maximum(case_4["lower_bound"] - case_4["higher_bound"], case_4["marginal_delta"]))

    # Case 5: positive reversal that passes through into non-reversal territory.
    mask = (dp["dir"] == 1) & (dp["band_reversal"] == -1) & (np.sign(dp["pairId"]) == 1) & (dp["boalf_spot_vol"] >= dp["lower_bound"])
    case_5 = dp[mask].copy()
    case_5["c"] = 5
    vol_already_used = np.maximum(case_5["lower_bound"] - case_5["base_value"], 0)
    space_in_this_level = case_5["higher_bound"] - case_5["lower_bound"]
    case_5["vol_to_price"] = np.minimum(case_5["marginal_delta"] - vol_already_used, space_in_this_level)

    # Case 6: negative reversal that passes through into non-reversal territory
    # -- computed but excluded by default, see docstring.
    mask = (dp["dir"] == -1) & (dp["band_reversal"] == -1) & (np.sign(dp["pairId"]) == -1)
    case_6 = dp[mask].copy()
    case_6["c"] = 6
    vol_already_used = np.maximum(case_6["lower_bound"] - case_6["base_value"], 0)
    case_6["vol_to_price"] = case_6["marginal_delta"] - vol_already_used

    cases = [case_1, case_2, case_3, case_4, case_5] + ([case_6] if include_case_6 else [])
    cases = [c for c in cases if not c.empty]
    if not cases:
        return pd.DataFrame()
    dp = pd.concat(cases, ignore_index=True)

    dp["ap_mult_vol"] = dp["ap"] * abs(dp["vol_to_price"])
    dp["delta"] = dp["vol_to_price"] * dp["dir"]
    return dp


def blend_disbsad(
    fpn_mel_boalf_dp: pd.DataFrame, disbsad_df: pd.DataFrame, flags_df: pd.DataFrame,
    disaggregate_disbsad: bool = False,
) -> pd.DataFrame:
    """Adds non-BM balancing actions (DISBSAD -- includes STOR) as
    synthetic per-(settlement period, SO/STOR flag, rounded price) rows
    alongside the real per-unit acceptances, and re-attaches the acceptance-
    level flags (deemedBoFlag/soFlag/storFlag/rrFlag) that boalf_exploder
    dropped early on.

    Volume recovery mechanism (unaffected by `disaggregate_disbsad` below):
    each synthetic disbsad row is repeated once for every distinct
    `spot_time` (minute) already present in `fpn_mel_boalf_dp` for that
    (settlementDate, settlementPeriod, soFlag, storFlag) group, carrying a
    flat per-minute MW-equivalent value (period volume x2, i.e. MWh/0.5h).
    build_price_stack()'s own groupby then sums across those repeated
    per-minute rows and divides by 60, exactly like a real per-minute MW
    reading would be -- recovering the true period MWh total. This mirrors
    real acceptances' own per-minute-then-/60 accounting, not a bug.

    `disaggregate_disbsad` (opt-in, see config.py's
    `disbsad_disaggregation_enabled`): by default every DISBSAD action
    sharing a (soFlag, storFlag) is pre-summed into ONE blended row with
    ONE volume-weighted average price, before PAR Tagging ever runs --
    engine/imbalance_price.py's `_par_tag()` then only ever sees that single
    blended price, even when PAR's 1 MWh boundary genuinely falls inside
    the combined block. Confirmed live, SP39 2026-09-25: 34 separate
    DISBSAD actions (all soFlag=False, storFlag=False) ranging GBP212.50-
    235.00/MWh get collapsed into one row at their blended average,
    discarding exactly the per-action price detail PAR Tagging needs to
    price the genuinely marginal MWh correctly. When enabled, each DISBSAD
    action keeps its own row (own price, own `id`-qualified synthetic
    bmUnit) all the way through Classification/NIV/PAR Tagging -- the same
    granularity real per-unit acceptances already get -- and only merges
    same-priced rows for display afterward (engine/view.py's
    `_merge_duplicates()`, already applied to every other row type).
    """
    df = fpn_mel_boalf_dp.copy()
    df["settlementDate"] = pd.to_numeric(df["settlementDate"])

    empty_cols = ["spot_time", "settlementDate", "settlementPeriod", "delta", "disbsad_cost", "soFlag", "storFlag", "price", "rounded_price", "bmUnit"]
    if disbsad_df.empty:
        exploded_disbsad = pd.DataFrame(columns=empty_cols)
    else:
        # Distinct minutes already present in df -- df has no soFlag/storFlag
        # of its own yet (those are only attached below, via the flags_df
        # merge), so -- matching the original merge's actual join key --
        # this crosses onto every disbsad action-row sharing the same
        # (settlementDate, settlementPeriod) regardless of which unit
        # governs that minute, made explicit here so the disaggregated
        # branch below can do it without an accidental many-to-many fan-out
        # (see `disaggregate_disbsad`'s own note).
        spot_times = df[["settlementDate", "settlementPeriod", "spot_time"]].drop_duplicates()

        if disaggregate_disbsad:
            action_rows = disbsad_df[["settlementDate", "settlementPeriod", "soFlag", "storFlag", "volume", "cost", "id"]].copy()
        else:
            action_rows = disbsad_df.groupby(["settlementDate", "settlementPeriod", "soFlag", "storFlag"])[["volume", "cost"]].sum().reset_index()
            action_rows["id"] = 0

        action_rows["volume"] = action_rows["volume"] * 2
        action_rows["cost"] = action_rows["cost"] * 2
        action_rows["settlementDate"] = pd.to_numeric(pd.to_datetime(action_rows["settlementDate"], utc=True))

        exploded_disbsad = pd.merge(spot_times, action_rows, on=["settlementDate", "settlementPeriod"], how="inner")
        exploded_disbsad = exploded_disbsad.rename(columns={"volume": "delta", "cost": "disbsad_cost"})
        exploded_disbsad["storFlag"] = exploded_disbsad["storFlag"].map({False: "F", True: "STOR"})
        exploded_disbsad["price"] = (exploded_disbsad["disbsad_cost"] / exploded_disbsad["delta"]).replace([np.inf, -np.inf], np.nan).fillna(0)
        exploded_disbsad["rounded_price"] = exploded_disbsad["price"].apply(custom_round)
        exploded_disbsad["storFlag"] = exploded_disbsad["storFlag"].fillna("empty")
        exploded_disbsad["bmUnit"] = (
            "disbsad_" + exploded_disbsad["storFlag"] + "_" + exploded_disbsad["rounded_price"].astype(str)
            + "_" + exploded_disbsad["id"].astype(str)
        )

    df["spot_time"] = pd.to_numeric(df["spot_time"])
    df["acceptanceTime"] = pd.to_numeric(df["acceptanceTime"])

    # NOTE: an earlier version of this merge inherited the ORIGINAL
    # acceptance's SO/STOR/deemed-BO flags for any row whose governing
    # acceptance was a detected reversal (`reversal == -1`, see
    # _detect_acceptance_reversals()). That inheritance has no basis in
    # the guide -- the word "reversal" doesn't appear anywhere in it -- and
    # was confirmed wrong against real data: Elexon's own settlement stack
    # (/balancing/settlement/stack/all) showed a reversed acceptance
    # (T_DINO-3 SP33, acceptanceNumber 148809) as genuinely CADL-flagged
    # on its own, while the inheritance was overriding it back to its
    # unflagged origin's status. Every acceptance is merged on its own
    # acceptanceNumber here; `origin_acceptance_number` is still computed
    # and carried on `df` (dropped just below) for potential future
    # display use, but no longer affects Classification.
    df = df.drop(columns=["origin_acceptance_number"])
    df = pd.merge(df, flags_df, on=["bmUnit", "acceptanceNumber"])

    combined = pd.concat([df, exploded_disbsad], ignore_index=True)
    combined["settlementDate"] = pd.to_datetime(combined["settlementDate"], utc=True)
    combined["spot_time"] = pd.to_datetime(combined["spot_time"], utc=True)
    combined["acceptanceTime"] = pd.to_datetime(combined["acceptanceTime"], utc=True)

    combined["pairId"] = combined["pairId"].fillna(0).astype(int)
    combined["bmUnit"] = combined["bmUnit"] + "_" + combined["pairId"].astype(str)
    combined["storFlag"] = combined["storFlag"].replace("F", False).infer_objects()
    combined["deemedBoFlag"] = combined["deemedBoFlag"].fillna(False).infer_objects()
    combined["rrFlag"] = combined["rrFlag"].fillna(False).infer_objects()
    combined["reversal"] = combined["reversal"].fillna(1)
    combined["acceptanceNumber"] = combined["acceptanceNumber"].fillna(0)
    # Synthetic DISBSAD rows never went through compute_stack()'s own
    # firstStageFlag/cadlFlag computation (CADL doesn't apply to non-BM
    # actions) -- their own soFlag is already the right classification
    # input, and cadlFlag is simply False for them.
    combined["firstStageFlag"] = combined["firstStageFlag"].fillna(combined["soFlag"])
    combined["cadlFlag"] = combined["cadlFlag"].fillna(False)
    return combined


def spot_time_niv(combined: pd.DataFrame) -> pd.DataFrame:
    """Per-(settlementDate, settlementPeriod, spot_time) sum of `combined`'s
    own `delta` -- the real-time NIV trajectory as it actually built up
    minute by minute from live bid/offer acceptances, before
    build_price_stack()'s acceptance-level groupby collapses the same
    column into one MWh figure per acceptance. Ported directly from the
    original notebook's own `spot_niv` (see SPOT_NIV_COLUMNS's docstring).
    """
    if combined.empty:
        return pd.DataFrame(columns=SPOT_NIV_COLUMNS)
    grouped = combined.groupby(["settlementDate", "settlementPeriod", "spot_time"])["delta"].sum().reset_index()
    return grouped.rename(columns={"delta": "niv_spot_time_max"})


def spot_time_bm_unit_delta_5min(combined: pd.DataFrame) -> pd.DataFrame:
    """Per-(settlementDate, settlementPeriod, bmUnit), 5-minute-bucketed sum
    of `combined`'s own `delta` -- the same real, reversal/CADL-aware figure
    `spot_time_niv()` sums system-wide, kept per-unit here and bucketed onto
    FUELINST's own 5-minute grid instead of one flat period-total.

    This module deliberately has no notion of fuel type (see its own module
    docstring), so it stops at bmUnit -- engine/fpn.py's own
    pricing_stack_delta_by_fuel_5min() does the bmUnit -> fuel-type join and
    the final per-fuel-type sum, the same split of responsibility
    pricing_stack_delta_by_fuel() already has with `pricing_stack_rows`.

    The `-1.51 minutes then round to 5min` bucketing matches
    engine/fpn.py's own `blend_generation_and_smooth()`/
    `compute_generation_by_fuel()` exactly, so a bmUnit's minute-level
    delta lands in the same 5-minute FUELINST block those functions already
    align everything else onto.

    Excludes `combined`'s own synthetic DISBSAD rows (`bmUnit LIKE
    'disbsad_%'`, from blend_disbsad()) -- same exclusion
    db.pricing_stack_delta_by_bm_unit's own SQL already applies for the
    period-level figure, for the same reason: those rows represent National
    Grid's own non-BM balancing actions, not a specific generating unit, so
    they have no fuel type to ever join onto and would just be dead rows
    here.

    Divides the summed delta by 5 after bucketing -- ported directly from
    the original Fuelinst notebook's own `non_market_gen` block
    (Fuelinst_Development_Continous.ipynb, cell 13:
    `non_market_gen['delta'] = non_market_gen['delta']/5`, applied right
    after its own equivalent `groupby(['bmUnit','startTime'])['delta'].sum()`).
    `combined` carries one row per (bmUnit, minute), so summing raw across a
    5-minute bucket adds up to five ~1x MW-level minute readings instead of
    averaging them -- a confirmed ~5x overcount whenever an acceptance was
    active for most/all of the bucket, exactly the "missing /5 averaging"
    bug this project's own history already hit once in an earlier version
    of this same table (see engine/fpn.py's compute_generation_by_fuel()
    module history). The notebook divides by a flat 5, not by however many
    of the bucket's minutes actually had data -- replicated as-is here for
    fidelity, not "corrected" to a per-actual-minute average.
    """
    if combined.empty:
        return pd.DataFrame(columns=UNIT_DELTA_5MIN_COLUMNS)
    df = combined[~combined["bmUnit"].str.startswith("disbsad_")].copy()
    if df.empty:
        return pd.DataFrame(columns=UNIT_DELTA_5MIN_COLUMNS)
    df["startTime"] = (df["spot_time"] - pd.Timedelta(seconds=1.51 * 60)).dt.round("5min")
    grouped = df.groupby(["settlementDate", "settlementPeriod", "bmUnit", "startTime"])["delta"].sum().reset_index()
    grouped["delta"] = grouped["delta"] / 5
    return grouped


def exploded_boalf_by_unit(combined: pd.DataFrame) -> pd.DataFrame:
    """Per-(settlementDate, settlementPeriod, bmUnit, spot_time) sum of
    `combined`'s own `delta` -- the raw, unbucketed per-minute trajectory
    each BM unit's accepted volume actually took, INCLUDING synthetic
    DISBSAD rows (bmUnit LIKE 'disbsad_%', unlike
    spot_time_bm_unit_delta_5min()'s own exclusion of them above). the reference app's
    "All Plants Exploded Boalf" chart colour-codes those synthetic rows
    under its own NATGRID fuel-type bucket rather than dropping them, and
    this is the one view here that needs that inclusion. Feeds
    engine/fpn.py's own exploded_boalf_with_fuel_type() for the bmUnit ->
    fuel-type join, same split of responsibility every other cross-module
    row type in this pipeline already has (this module has no notion of
    fuel type -- see the module docstring above).
    """
    if combined.empty:
        return pd.DataFrame(columns=EXPLODED_BOALF_COLUMNS)
    grouped = combined.groupby(["settlementDate", "settlementPeriod", "bmUnit", "spot_time"])["delta"].sum().reset_index()
    return grouped


def build_price_stack(
    combined: pd.DataFrame,
    max_ta,
    pricing_method: str = "guide",
    par_band_method: str = "interval",
    market_index_prices: dict[tuple, float] | None = None,
    bm_rows: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Groups every priced row (real acceptances + synthetic DISBSAD rows)
    into the final settlement-period price stack, then derives the
    period's energy imbalance price.

    `pricing_method`:
      - "guide" (default) -- Elexon's own documented methodology (De
        Minimis/Arbitrage/Classification/NIV/Replacement-Price/PAR Tagging;
        see engine/imbalance_price.py's module docstring for exactly what
        is and isn't implemented). Per-row `so_flag` is used properly here
        -- flagged actions only count toward price if Classification
        reclassifies them as effectively unflagged.
      - "legacy" -- the original notebook's own NIV/PAR-band heuristic,
        which never distinguished flagged from unflagged actions at all.
        Kept only for A/B comparison via scripts/backtest_sp.py; `soFlag`
        is completely ignored by this path.

    `par_band_method` only applies to `pricing_method="legacy"`: "interval"
    (corrected) or "ratio" (the original formula, which has a confirmed
    sign bug -- see the inline comment where it's used, below).

    `market_index_prices`: {(settlement_date, settlement_period): price}
    -- only used by `pricing_method="guide"`, fed to
    engine.imbalance_price.compute_imbalance_price() as its Replacement
    Price fallback (see that module's docstring). Periods missing from
    this dict fall back to leaving flagged rows at their own price.
    """
    if combined.empty:
        return pd.DataFrame(columns=STACK_COLUMNS)

    df = combined.copy()
    df["max_ta"] = max_ta

    misik_stack = df.groupby(
        ["settlementDate", "settlementPeriod", "max_ta", "bmUnit", "storFlag", "deemedBoFlag", "soFlag", "cadlFlag", "firstStageFlag", "acceptanceNumber", "reversal"]
    )[["ap_mult_vol", "delta", "vol_to_price", "disbsad_cost"]].sum().reset_index()

    misik_stack["delta"] = misik_stack["delta"] / 60
    misik_stack["disbsad_cost"] = misik_stack["disbsad_cost"] / 60

    m_orig_price_unit = (misik_stack["ap_mult_vol"] / misik_stack["vol_to_price"]).replace([np.inf, -np.inf], np.nan).fillna(0)
    m_orig_price_disbsad = (misik_stack["disbsad_cost"] / misik_stack["delta"]).replace([np.inf, -np.inf], np.nan).fillna(0)
    misik_stack["m_orig_price"] = m_orig_price_unit + m_orig_price_disbsad

    if bm_rows is not None:
        # Acceptance model (engine/acceptance_volumes.py): the BM rows come from the previous-
        # acceptance rule and bid-offer band split instead of the FPN-relative minute model; the
        # synthetic DISBSAD rows are kept exactly as built above.
        non_bm = misik_stack[misik_stack["bmUnit"].astype(str).str.startswith("disbsad_")].copy()
        non_bm["dmatKey"] = non_bm["bmUnit"]
        misik_stack = pd.concat([non_bm, bm_rows], ignore_index=True)
        summary_niv = misik_stack.groupby(["settlementDate", "settlementPeriod"])["delta"].sum().reset_index()
    else:
        summary_niv = df[["settlementDate", "settlementPeriod", "delta"]].groupby(["settlementDate", "settlementPeriod"])["delta"].sum().reset_index()
        summary_niv["delta"] = summary_niv["delta"] / 60
    summary_niv["delta_sign"] = np.sign(summary_niv["delta"])
    summary_niv = summary_niv.rename(columns={"delta": "total_delta"})

    misik_stack = pd.merge(misik_stack, summary_niv)
    misik_stack["sorter"] = misik_stack["m_orig_price"] * misik_stack["delta_sign"]
    misik_stack = misik_stack.sort_values(["settlementDate", "settlementPeriod", "sorter"]).drop(columns=["sorter"])

    misik_stack = misik_stack[misik_stack["delta"] != 0].copy()
    if misik_stack.empty:
        return pd.DataFrame(columns=STACK_COLUMNS)

    misik_stack["action_sign"] = np.sign(misik_stack["delta"])
    misik_stack["m_orig_price"] = misik_stack["m_orig_price"].round(2)
    misik_stack["vol_for_price"] = np.nan  # legacy-only concept, see below

    if pricing_method == "guide":
        return _price_via_guide_methodology(misik_stack, market_index_prices or {})

    # Same-priced actions on the same side of NIV are collapsed to a single
    # cumulative-volume value (max for offers, min for bids among ties) --
    # equivalent to the original's `spd_sign`-encoded groupby trick, done
    # here with an explicit (settlementDate, settlementPeriod, action_sign)
    # group key instead.
    group_key = ["settlementDate", "settlementPeriod", "action_sign"]
    misik_stack = misik_stack.sort_values(group_key + ["m_orig_price"])
    misik_stack["vol_for_price"] = misik_stack.groupby(group_key)["delta"].cumsum()

    positive = misik_stack[misik_stack["action_sign"] == 1]
    negative = misik_stack[misik_stack["action_sign"] == -1]
    positive_tied = positive.groupby(group_key + ["m_orig_price"])["vol_for_price"].max().reset_index()
    negative_tied = negative.groupby(group_key + ["m_orig_price"])["vol_for_price"].min().reset_index()
    tied = pd.concat([positive_tied, negative_tied], ignore_index=True)

    misik_stack = misik_stack.drop(columns=["vol_for_price"])
    misik_stack = pd.merge(misik_stack, tied, how="left", on=group_key + ["m_orig_price"])

    par = 1
    misik_stack["action_in_price_stack"] = (np.sign(misik_stack["delta"]) * misik_stack["delta_sign"] + 1) / 2
    misik_stack["vol_to_cumm"] = misik_stack["action_in_price_stack"] * abs(misik_stack["delta"])

    misik_stack["abs_total_delta"] = abs(misik_stack["total_delta"])
    misik_stack["m_par"] = np.minimum(par, misik_stack["abs_total_delta"])

    sp_key = ["settlementDate", "settlementPeriod"]
    misik_stack["cumm_pricing_vol"] = misik_stack.groupby(sp_key)["vol_to_cumm"].cumsum()

    temp_a = misik_stack["cumm_pricing_vol"] - misik_stack["vol_to_cumm"]
    misik_stack["niv_sub_par"] = misik_stack["abs_total_delta"] - misik_stack["m_par"]

    if par_band_method == "ratio":
        # The original notebook's formula, verbatim. BUG (found while
        # backtesting this port, not introduced by it -- confirmed against
        # a real settlement period where this produced a computed price
        # off by >100x): for any row before the [niv_sub_par, abs_total_delta]
        # PAR band is reached -- which is nearly every row, for any NIV
        # bigger than a few MWh, i.e. nearly always -- `denom`/`denom2` are
        # both deeply negative, so `vol_intersection` correctly comes out
        # ~0, but `np.minimum(cumm_pricing_vol - niv_sub_par, vol_to_cumm *
        # vol_intersection)` then picks the deeply-negative FIRST operand
        # instead of the intended-zero second one (min() doesn't gate on
        # vol_intersection, it just takes whichever operand is smaller).
        # That deeply negative `niv_vol_pt_1` then poisons the running
        # cumsum (`niv_vol_pt_1_cumsum_sub_1`) for every subsequent row,
        # which is exactly the mechanism behind the >100x price error.
        # Kept only for A/B comparison via scripts/backtest_sp.py.
        denom = temp_a - misik_stack["abs_total_delta"]
        vol_lower_than_niv = (np.minimum(denom, 0) / denom).replace([np.inf, -np.inf], np.nan).fillna(0)
        denom2 = misik_stack["cumm_pricing_vol"] - misik_stack["niv_sub_par"]
        vol_more_than_niv_sub_par = (np.maximum(denom2, 0) / denom2).replace([np.inf, -np.inf], np.nan).fillna(0)
        vol_intersection = vol_lower_than_niv * vol_more_than_niv_sub_par
        misik_stack["niv_vol_pt_1"] = np.minimum(
            misik_stack["cumm_pricing_vol"] - misik_stack["niv_sub_par"],
            misik_stack["vol_to_cumm"] * vol_intersection,
        )
    else:
        # Corrected version: direct clamped interval overlap between this
        # row's own cumulative-volume span [temp_a, cumm_pricing_vol] and
        # the PAR band [niv_sub_par, abs_total_delta] -- the same
        # "PAR-weighted average price of the last PAR MWh nearest NIV"
        # intent the original formula was going for, without the sign
        # pitfall above. Mathematically guaranteed to land in
        # [0, min(vol_to_cumm, m_par)] for every row, so it can never
        # exceed the 1 MWh PAR budget the way the ratio version does.
        band_lo = misik_stack["niv_sub_par"]
        band_hi = misik_stack["abs_total_delta"]
        overlap = np.minimum(misik_stack["cumm_pricing_vol"], band_hi) - np.maximum(temp_a, band_lo)
        misik_stack["niv_vol_pt_1"] = np.maximum(overlap, 0)

    misik_stack["niv_vol_pt_1_cumsum_sub_1"] = (
        misik_stack.groupby(sp_key)["niv_vol_pt_1"].cumsum() - misik_stack["niv_vol_pt_1"]
    )
    misik_stack["par_adj_vol"] = np.maximum(
        np.minimum(misik_stack["m_par"] - misik_stack["niv_vol_pt_1_cumsum_sub_1"], misik_stack["niv_vol_pt_1"]), 0
    )
    misik_stack["misik_imb_price"] = (
        (misik_stack["par_adj_vol"] * misik_stack["m_orig_price"] / misik_stack["m_par"])
        .replace([np.inf, -np.inf], np.nan).fillna(0)
    )

    joiner = misik_stack.groupby(sp_key)[["misik_imb_price"]].sum().reset_index()
    joiner = joiner.rename(columns={"misik_imb_price": "total_misik_price"})
    misik_stack = pd.merge(misik_stack, joiner)

    misik_stack = misik_stack[misik_stack["delta"] != 0]
    return misik_stack[STACK_COLUMNS].reset_index(drop=True)


def _price_via_guide_methodology(misik_stack: pd.DataFrame, market_index_prices: dict[tuple, float]) -> pd.DataFrame:
    """Prices every settlement period in `misik_stack` using
    engine/imbalance_price.py's Classification/NIV/PAR-Tagging pipeline
    instead of the legacy heuristic below. `misik_stack` must already have
    settlementDate/settlementPeriod/delta/m_orig_price/soFlag and every
    other STACK_COLUMNS field except misik_imb_price/total_misik_price.
    """
    from .imbalance_price import compute_imbalance_price

    misik_stack = misik_stack.copy()
    misik_stack["misik_imb_price"] = 0.0
    misik_stack["total_misik_price"] = 0.0
    per_acceptance_dmat = "dmatKey" in misik_stack.columns

    for (sd, sp), group in misik_stack.groupby(["settlementDate", "settlementPeriod"]):
        rows = [
            # Classification eligibility uses `firstStageFlag` (raw soFlag
            # OR CADL-flagged, see compute_stack()'s own comment) -- the
            # *displayed* soFlag column stays raw BOALF and is never used
            # for pricing decisions.
            {"delta": r.delta, "m_orig_price": r.m_orig_price, "so_flag": bool(r.firstStageFlag)}
            for r in group.itertuples()
        ]
        sd_date = sd.date() if hasattr(sd, "date") else sd
        mip = market_index_prices.get((sd_date, int(sp)))
        if per_acceptance_dmat:
            # Elexon's de-minimis tagging looks at a whole acceptance (all its bid-offer pairs on one
            # side), not at each row: drop acceptances under 0.1 MWh here, then price without the
            # per-row rule.
            totals = group.assign(_v=group["delta"].abs()).groupby("dmatKey")["_v"].sum()
            keep = [row for row, key in zip(rows, group["dmatKey"]) if totals[key] >= 0.1]
            price, contributions = compute_imbalance_price(keep, market_index_price=mip, apply_de_minimis=False)
        else:
            price, contributions = compute_imbalance_price(rows, market_index_price=mip)
        idx = group.index
        misik_stack.loc[idx, "misik_imb_price"] = [contributions.get(id(r), 0.0) for r in rows]
        misik_stack.loc[idx, "total_misik_price"] = price

    return misik_stack[STACK_COLUMNS].reset_index(drop=True)


def compute_stack(
    boalf_df: pd.DataFrame,
    bod_df: pd.DataFrame,
    pn_df: pd.DataFrame,
    mel_df: pd.DataFrame,
    disbsad_df: pd.DataFrame,
    include_case_6: bool = False,
    use_mel_gate: bool = True,
    pricing_method: str = "guide",
    par_band_method: str = "interval",
    market_index_prices: dict[tuple, float] | None = None,
    overlap_resolution: str = "reversal_aware",
    return_spot_niv: bool = False,
    return_exploded_boalf: bool = False,
    reversal_side_fix: bool = False,
    disaggregate_disbsad: bool = False,
    acceptance_model: bool = False,
) -> pd.DataFrame | tuple[pd.DataFrame, ...]:
    """End-to-end: raw per-dataset DataFrames in, the final price-stack
    DataFrame out (columns: STACK_COLUMNS). Empty at any stage short-
    circuits to an empty result rather than raising -- a quiet settlement
    period (no acceptances) is the normal case, not an error.

    `market_index_prices`: see build_price_stack()'s own docstring.
    `overlap_resolution`: see build_marginal_deltas()'s own docstring --
    "reversal_aware" (default) fixed a real case Elexon's own settlement
    data disagreed with us on (T_CDCL-1 SP35 2026-09-20) and was validated
    against 15 real settlement periods across three separate days with
    zero regressions before being made the default; "latest_wins" is kept
    for comparison via scripts/backtest_sp.py.

    `return_spot_niv`: additionally returns spot_time_niv()'s per-minute NIV
    trajectory and spot_time_bm_unit_delta_5min()'s per-unit, 5-minute-
    bucketed delta (columns: SPOT_NIV_COLUMNS, UNIT_DELTA_5MIN_COLUMNS) as a
    second and third value, both computed from the same `combined` frame
    this function already builds internally -- opt-in and defaulted off so
    every existing caller (tests included) sees the exact same single-
    DataFrame return as before.

    `return_exploded_boalf`: additionally returns exploded_boalf_by_unit()'s
    raw per-minute, per-bmUnit delta (columns: EXPLODED_BOALF_COLUMNS) as
    one more value, appended after whatever `return_spot_niv` already
    contributes -- feeds the "All Plants Exploded BOALF" chart. Also
    computed from the same `combined` frame; opt-in and defaulted off for
    the same backward-compatibility reason as `return_spot_niv`.

    `reversal_side_fix`: see build_marginal_deltas()'s own docstring --
    opt-in (config.py's `reversal_side_fix_enabled`, dev-only until
    validated) fix for a genuine reversal landing on the wrong side of the
    bid/offer stack.

    `disaggregate_disbsad`: see blend_disbsad()'s own docstring -- opt-in
    (config.py's `disbsad_disaggregation_enabled`, dev-only until
    validated) fix so PAR Tagging sees each DISBSAD action's own price
    instead of one blended average across every action sharing a
    (soFlag, storFlag).

    `acceptance_model`: opt-in (config.py's `acceptance_model_enabled`, dev-only until validated).
    Prices the stack from engine/acceptance_volumes.py's rows -- each acceptance measured against
    the previous acceptance in force, split across the bid-offer bands -- instead of the FPN-relative
    `latest_wins`/reversal-aware minute model, which nets away the upward moves Elexon books as
    offers. Reproduced Elexon's published price for 24/24 periods on 2026-10-08 and 46/48 on
    2026-10-07. Everything else (spot NIV, per-unit deltas, the exploded BOALF chart) still comes
    from the minute model, whose net volumes are identical.
    """
    empty_stack = pd.DataFrame(columns=STACK_COLUMNS)
    empty_niv = pd.DataFrame(columns=SPOT_NIV_COLUMNS)
    empty_unit_delta = pd.DataFrame(columns=UNIT_DELTA_5MIN_COLUMNS)
    empty_exploded = pd.DataFrame(columns=EXPLODED_BOALF_COLUMNS)

    def _empty():
        if not return_spot_niv and not return_exploded_boalf:
            return empty_stack
        extras = (empty_niv, empty_unit_delta) if return_spot_niv else ()
        extras += (empty_exploded,) if return_exploded_boalf else ()
        return (empty_stack, *extras)

    if boalf_df.empty:
        return _empty()

    boalf_df = boalf_df.drop_duplicates()
    max_ta = pd.to_datetime(boalf_df["acceptanceTime"], utc=True).max()
    flags_df = boalf_df[["bmUnit", "deemedBoFlag", "soFlag", "storFlag", "rrFlag", "acceptanceNumber"]].drop_duplicates()

    # `soFlag` (the field that decides which side of the stack a row is
    # DISPLAYED under -- so_flagged vs unflagged, see view.py's
    # split_and_sort) is left exactly as the raw BOALF dataset carries it.
    # A CADL-flagged action whose own raw soFlag is False belongs on the
    # unflagged side of the displayed stack -- confirmed against Elexon's
    # own real settlement stack, which likewise publishes soFlag and
    # cadlFlag as two separate fields (SP19, 2026-09-30, T_MCMRO002
    # acceptance 69649: `soFlag: False, cadlFlag: True`).
    #
    # Classification/PAR Tagging *eligibility* is a different question from
    # display, though: the guide's "First Stage Flagged" = raw soFlag OR
    # CADL-flagged, and build_price_stack() uses `firstStageFlag` (computed
    # here, separately from the displayed `soFlag`) for that -- confirmed
    # live this is what reproduces Elexon's own SP19 price (GBP176.35) via
    # this session's own testing: folding CADL in for classification alone
    # (leaving the *displayed* soFlag raw) gives the same GBP176.35 as
    # folding it into soFlag everywhere used to, while ALSO putting a
    # CADL-flagged-but-raw-unflagged action on the correct (unflagged) side
    # of the displayed stack, which the fully-folded version did not.
    #
    # `cadlFlag` is kept as its own visible column throughout (see
    # STACK_COLUMNS) so it's never just folded away and lost either way.
    # Step timings (seconds) ride on the returned frame's .attrs so the runner can log
    # which step a slow cycle spent its time in.
    timings: dict[str, float] = {}
    _last = [time.perf_counter()]

    def _lap(name: str) -> None:
        now_ = time.perf_counter()
        timings[name] = round(now_ - _last[0], 2)
        _last[0] = now_

    cadl_flags = compute_cadl_flags(boalf_df)
    _lap("cadl_flags")
    flags_df = pd.merge(flags_df, cadl_flags, on=["bmUnit", "acceptanceNumber"], how="left")
    flags_df["cadl_flag"] = flags_df["cadl_flag"].fillna(False)
    flags_df["firstStageFlag"] = flags_df["soFlag"] | flags_df["cadl_flag"]
    flags_df = flags_df.rename(columns={"cadl_flag": "cadlFlag"})

    fpn_boalf = build_marginal_deltas(boalf_df, pn_df, overlap_resolution=overlap_resolution, reversal_side_fix=reversal_side_fix)
    _lap("marginal_deltas")
    if fpn_boalf.empty:
        return _empty()

    fpn_mel_boalf = merge_mel_gate(fpn_boalf, mel_df, use_mel_gate=use_mel_gate)
    _lap("mel_gate")
    if fpn_mel_boalf.empty:
        return _empty()
    # allocate_volumes() converts settlementDate to datetime on its own
    # local copy of fpn_mel_boalf -- done here too so *this* frame stays
    # dtype-consistent with `dp` for the merge below (unlike the original
    # notebook's flat script, a plain variable mutation inside a function
    # doesn't propagate back to the caller's own reference).
    fpn_mel_boalf["settlementDate"] = pd.to_datetime(fpn_mel_boalf["settlementDate"], utc=True)

    dp = allocate_volumes(fpn_mel_boalf, bod_df, include_case_6=include_case_6)
    _lap("allocate_volumes")
    if dp.empty:
        return _empty()

    fpn_mel_boalf_dp = pd.merge(fpn_mel_boalf, dp)
    if fpn_mel_boalf_dp.empty:
        return _empty()

    combined = blend_disbsad(fpn_mel_boalf_dp, disbsad_df, flags_df, disaggregate_disbsad=disaggregate_disbsad)
    _lap("blend_disbsad")
    bm_rows = None
    if acceptance_model and pricing_method == "guide":
        from .acceptance_volumes import acceptance_stack_rows

        bm_rows = acceptance_stack_rows(
            boalf_df, pn_df, bod_df, flags_df, combined[["settlementDate", "settlementPeriod"]].drop_duplicates(), max_ta)
        _lap("acceptance_rows")
    stack_result = build_price_stack(
        combined, max_ta, pricing_method=pricing_method, par_band_method=par_band_method,
        market_index_prices=market_index_prices, bm_rows=bm_rows,
    )
    _lap("price_stack")
    if not return_spot_niv and not return_exploded_boalf:
        return stack_result
    extras = (spot_time_niv(combined), spot_time_bm_unit_delta_5min(combined)) if return_spot_niv else ()
    extras += (exploded_boalf_by_unit(combined),) if return_exploded_boalf else ()
    _lap("extras")
    stack_result.attrs["timings"] = timings
    return (stack_result, *extras)
