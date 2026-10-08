"""Unit tests for the ported pricing engine (citadel/engine/stack.py).

The end-to-end scenario below is hand-derived (see the PR/commit notes) --
a single BM unit with one accepted offer, no DISBSAD, no MEL/MIL
constraint, entirely within its BOD offer band -- to pin down that the full
pipeline reproduces the expected £75/MWh marginal price for a NIV well
under the 1 MWh PAR. Anchoring this now, against known-good arithmetic,
protects the whole port from silent regressions during later refactors.
"""

from __future__ import annotations

import pandas as pd
import pytest

from citadel.engine.stack import (
    allocate_volumes,
    build_bod_ladder,
    build_marginal_deltas,
    compute_cadl_flags,
    compute_stack,
    custom_round,
    fpn_exploder,
    spot_time_bm_unit_delta_5min,
    spot_time_niv,
    vectorized_exploder,
)


def test_custom_round():
    assert custom_round(12.4) == 10
    assert custom_round(13, base=5) == 15
    assert custom_round(-3) == -5


def test_vectorized_exploder_centres_linear_interpolation():
    df = pd.DataFrame([{
        "timeFrom": "2026-01-01T00:00:00Z", "timeTo": "2026-01-01T00:02:00Z",
        "levelFrom": 0.0, "levelTo": 20.0, "bmUnit": "T_TEST-1",
    }])
    out = vectorized_exploder(df)
    assert list(out["spot_level"]) == pytest.approx([5.0, 15.0])
    assert out["spot_time"].iloc[0] == pd.Timestamp("2026-01-01T00:00:00Z")
    assert out["spot_time"].iloc[1] == pd.Timestamp("2026-01-01T00:01:00Z")


def test_vectorized_exploder_empty_input_has_expected_columns():
    df = pd.DataFrame(columns=["timeFrom", "timeTo", "levelFrom", "levelTo"])
    out = vectorized_exploder(df)
    assert out.empty
    assert "spot_time" in out.columns and "spot_level" in out.columns


def test_fpn_exploder_keeps_only_the_latest_revision_per_minute():
    """A PN revision that re-declares an overlapping window for the same
    bmUnit (no explicit publish timestamp exists on the raw feed to sort
    by, unlike BOALF's acceptanceTime or MEL's notificationTime) must not
    accumulate alongside the stale one -- confirmed as the actual cause of
    a real ~2.3x SP24 NIV inflation live (2026-09-29, T_KEAD-2/acceptance
    46124): WindowBuffers.add()'s IRIS path only dedupes whole-record
    duplicates, so a stale and a revised PN row for the same minute both
    survived into this exploder, fanning out build_marginal_deltas()'s
    BOALF join into two rows per minute instead of one.
    """
    pn_df = pd.DataFrame([
        {"bmUnit": "T_X-1", "timeFrom": "2026-01-01T00:00:00Z", "timeTo": "2026-01-01T00:02:00Z", "levelFrom": 100, "levelTo": 100},
        # A later-arriving revision of the same window at a different level --
        # the stale row above should no longer govern either minute.
        {"bmUnit": "T_X-1", "timeFrom": "2026-01-01T00:00:00Z", "timeTo": "2026-01-01T00:02:00Z", "levelFrom": 40, "levelTo": 40},
    ])
    out = fpn_exploder(pn_df)
    assert len(out) == 2  # one row per minute, not four
    assert (out["fpn_spot_vol"] == 40).all()


def test_build_marginal_deltas_does_not_fan_out_on_a_stale_pn_revision():
    boalf_df = pd.DataFrame([_flat_acceptance("T_X-1", 140)])
    pn_df = pd.DataFrame([
        {"bmUnit": "T_X-1", "timeFrom": "2026-01-01T00:00:00Z", "timeTo": "2026-01-01T00:01:00Z", "levelFrom": 100, "levelTo": 100},
        {"bmUnit": "T_X-1", "timeFrom": "2026-01-01T00:00:00Z", "timeTo": "2026-01-01T00:01:00Z", "levelFrom": 0, "levelTo": 0},
    ])
    out = build_marginal_deltas(boalf_df, pn_df)
    assert len(out) == 1  # exactly one governing minute, not two
    assert out["marginal_delta"].iloc[0] == 140  # against the latest PN (0), not the stale one (100)


def _flat_acceptance(bm_unit: str, level: float, sd: str = "2026-01-01") -> dict:
    return {
        "bmUnit": bm_unit, "timeFrom": f"{sd}T00:00:00Z", "timeTo": f"{sd}T00:01:00Z",
        "levelFrom": level, "levelTo": level, "acceptanceTime": f"{sd}T00:00:00Z",
        "acceptanceNumber": 1, "settlementDate": sd, "settlementPeriodFrom": 1,
        "settlementPeriodTo": 1, "deemedBoFlag": False, "soFlag": False,
        "storFlag": False, "rrFlag": False,
    }


def test_compute_stack_single_offer_within_par():
    """One BM unit accepted from an FPN baseline of 0 up to a flat 10 MW for
    one minute, entirely inside a [0, 20] MW offer band priced at £75/MWh.
    NIV for the period is 10/60 MWh, well under the 1 MWh PAR, so the whole
    action should set the marginal price: expect total_misik_price == 75.0.
    """
    sd = "2026-01-01"
    boalf_df = pd.DataFrame([_flat_acceptance("T_TEST-1", 10.0, sd)])
    pn_df = pd.DataFrame([{
        "bmUnit": "T_TEST-1", "timeFrom": f"{sd}T00:00:00Z", "timeTo": f"{sd}T00:01:00Z",
        "levelFrom": 0.0, "levelTo": 0.0, "dataset": "PN", "nationalGridBmUnit": "T_TEST-1",
        "settlementDate": sd, "settlementPeriod": 1,
    }])
    mel_df = pd.DataFrame([{
        "bmUnit": "T_TEST-1", "timeFrom": f"{sd}T00:00:00Z", "timeTo": f"{sd}T00:01:00Z",
        "levelFrom": 100.0, "levelTo": 100.0, "dataset": "MELS", "nationalGridBmUnit": "T_TEST-1",
        "settlementDate": sd, "settlementPeriod": 1, "notificationTime": f"{sd}T00:00:00Z",
        "notificationSequence": 1,
    }])
    bod_df = pd.DataFrame([{
        "settlementDate": sd, "settlementPeriod": 1, "bmUnit": "T_TEST-1",
        "bid": -50.0, "offer": 75.0, "levelTo": 20.0, "pairId": 1,
    }])
    disbsad_df = pd.DataFrame(columns=["settlementDate", "settlementPeriod", "soFlag", "storFlag", "volume", "cost"])

    result = compute_stack(boalf_df, bod_df, pn_df, mel_df, disbsad_df)

    assert len(result) == 1
    row = result.iloc[0]
    assert row["m_orig_price"] == pytest.approx(75.0)
    assert row["total_misik_price"] == pytest.approx(75.0)
    assert row["delta"] == pytest.approx(10 / 60)
    assert row["bmUnit"] == "T_TEST-1_1"  # pairId suffix appended in blend_disbsad


def test_compute_stack_return_spot_niv_gives_per_minute_niv_before_the_mwh_collapse():
    """`return_spot_niv=True` exposes the same reversal-aware `delta`
    build_price_stack() itself uses, but grouped by minute instead of by
    acceptance -- so for this one-minute, 10 MW acceptance the per-minute
    value is 10.0 (MW), not `10 / 60` (MWh, what the acceptance-level
    `result['delta']` shows once build_price_stack() sums the full
    acceptance and divides by 60). Confirms spot_time_niv() needs no
    separate unit conversion of its own. Also confirms the third value,
    spot_time_bm_unit_delta_5min()'s own per-unit/5-minute-bucketed sibling,
    attributes that same 10 MW to its own bmUnit (with the pairId suffix
    blend_disbsad() appends) instead of summing system-wide -- then divides
    by 5 (ported from the original notebook's own `non_market_gen`
    averaging step), giving 2.0 for this single one-minute acceptance.
    """
    sd = "2026-01-01"
    boalf_df = pd.DataFrame([_flat_acceptance("T_TEST-1", 10.0, sd)])
    pn_df = pd.DataFrame([{
        "bmUnit": "T_TEST-1", "timeFrom": f"{sd}T00:00:00Z", "timeTo": f"{sd}T00:01:00Z",
        "levelFrom": 0.0, "levelTo": 0.0, "dataset": "PN", "nationalGridBmUnit": "T_TEST-1",
        "settlementDate": sd, "settlementPeriod": 1,
    }])
    mel_df = pd.DataFrame([{
        "bmUnit": "T_TEST-1", "timeFrom": f"{sd}T00:00:00Z", "timeTo": f"{sd}T00:01:00Z",
        "levelFrom": 100.0, "levelTo": 100.0, "dataset": "MELS", "nationalGridBmUnit": "T_TEST-1",
        "settlementDate": sd, "settlementPeriod": 1, "notificationTime": f"{sd}T00:00:00Z",
        "notificationSequence": 1,
    }])
    bod_df = pd.DataFrame([{
        "settlementDate": sd, "settlementPeriod": 1, "bmUnit": "T_TEST-1",
        "bid": -50.0, "offer": 75.0, "levelTo": 20.0, "pairId": 1,
    }])
    disbsad_df = pd.DataFrame(columns=["settlementDate", "settlementPeriod", "soFlag", "storFlag", "volume", "cost"])

    result, niv, unit_delta = compute_stack(boalf_df, bod_df, pn_df, mel_df, disbsad_df, return_spot_niv=True)

    assert result.iloc[0]["delta"] == pytest.approx(10 / 60)
    assert len(niv) == 1
    assert niv.iloc[0]["niv_spot_time_max"] == pytest.approx(10.0)
    assert niv.iloc[0]["spot_time"] == pd.Timestamp("2026-01-01T00:00:00Z")

    assert len(unit_delta) == 1
    assert unit_delta.iloc[0]["bmUnit"] == "T_TEST-1_1"
    assert unit_delta.iloc[0]["delta"] == pytest.approx(2.0)
    assert unit_delta.iloc[0]["startTime"] == pd.Timestamp("2026-01-01T00:00:00Z")


def test_compute_stack_without_return_spot_niv_keeps_the_original_single_frame_return():
    """Every existing caller relies on the plain-DataFrame return -- the new
    kwarg must be strictly opt-in.
    """
    sd = "2026-01-01"
    boalf_df = pd.DataFrame([_flat_acceptance("T_TEST-1", 10.0, sd)])
    pn_df = pd.DataFrame([{
        "bmUnit": "T_TEST-1", "timeFrom": f"{sd}T00:00:00Z", "timeTo": f"{sd}T00:01:00Z",
        "levelFrom": 0.0, "levelTo": 0.0, "dataset": "PN", "nationalGridBmUnit": "T_TEST-1",
        "settlementDate": sd, "settlementPeriod": 1,
    }])
    mel_df = pd.DataFrame([{
        "bmUnit": "T_TEST-1", "timeFrom": f"{sd}T00:00:00Z", "timeTo": f"{sd}T00:01:00Z",
        "levelFrom": 100.0, "levelTo": 100.0, "dataset": "MELS", "nationalGridBmUnit": "T_TEST-1",
        "settlementDate": sd, "settlementPeriod": 1, "notificationTime": f"{sd}T00:00:00Z",
        "notificationSequence": 1,
    }])
    bod_df = pd.DataFrame([{
        "settlementDate": sd, "settlementPeriod": 1, "bmUnit": "T_TEST-1",
        "bid": -50.0, "offer": 75.0, "levelTo": 20.0, "pairId": 1,
    }])
    disbsad_df = pd.DataFrame(columns=["settlementDate", "settlementPeriod", "soFlag", "storFlag", "volume", "cost"])

    result = compute_stack(boalf_df, bod_df, pn_df, mel_df, disbsad_df)
    assert isinstance(result, pd.DataFrame)


def test_spot_time_niv_sums_across_bm_units_per_minute():
    combined = pd.DataFrame([
        {"settlementDate": pd.Timestamp("2026-01-01"), "settlementPeriod": 1, "spot_time": pd.Timestamp("2026-01-01T00:00:00Z"), "delta": 10.0},
        {"settlementDate": pd.Timestamp("2026-01-01"), "settlementPeriod": 1, "spot_time": pd.Timestamp("2026-01-01T00:00:00Z"), "delta": -3.0},
        {"settlementDate": pd.Timestamp("2026-01-01"), "settlementPeriod": 1, "spot_time": pd.Timestamp("2026-01-01T00:01:00Z"), "delta": 5.0},
    ])
    niv = spot_time_niv(combined)
    by_minute = niv.set_index("spot_time")["niv_spot_time_max"]
    assert by_minute[pd.Timestamp("2026-01-01T00:00:00Z")] == pytest.approx(7.0)
    assert by_minute[pd.Timestamp("2026-01-01T00:01:00Z")] == pytest.approx(5.0)


def test_spot_time_niv_empty_input_has_expected_columns():
    niv = spot_time_niv(pd.DataFrame())
    assert niv.empty
    assert list(niv.columns) == ["settlementDate", "settlementPeriod", "spot_time", "niv_spot_time_max"]


def test_spot_time_bm_unit_delta_5min_keeps_bm_unit_and_buckets_onto_five_minutes():
    """Unlike spot_time_niv() (system-wide sum), this keeps each bmUnit's
    own delta separate, and groups by FUELINST's own 5-minute grid instead
    of the raw minute -- two units in the same 5-minute block stay separate
    rows; a unit's own minutes within one block sum together, then divide
    by 5 (ported from the original Fuelinst notebook's own `non_market_gen`
    step -- see this function's own docstring): summing raw per-minute MW
    readings across a 5-minute bucket overcounts by up to 5x, since each
    row is already an instantaneous MW value, not an energy amount to add up.
    """
    combined = pd.DataFrame([
        {"settlementDate": pd.Timestamp("2026-01-01"), "settlementPeriod": 1, "bmUnit": "T_A-1", "spot_time": pd.Timestamp("2026-01-01T00:00:00Z"), "delta": 10.0},
        {"settlementDate": pd.Timestamp("2026-01-01"), "settlementPeriod": 1, "bmUnit": "T_A-1", "spot_time": pd.Timestamp("2026-01-01T00:01:00Z"), "delta": 4.0},
        {"settlementDate": pd.Timestamp("2026-01-01"), "settlementPeriod": 1, "bmUnit": "T_B-1", "spot_time": pd.Timestamp("2026-01-01T00:00:00Z"), "delta": -3.0},
        {"settlementDate": pd.Timestamp("2026-01-01"), "settlementPeriod": 1, "bmUnit": "T_A-1", "spot_time": pd.Timestamp("2026-01-01T00:05:00Z"), "delta": 100.0},
    ])
    out = spot_time_bm_unit_delta_5min(combined)
    by_key = {(r.bmUnit, r.startTime): r.delta for r in out.itertuples()}

    first_bucket = pd.Timestamp("2026-01-01T00:00:00Z")
    second_bucket = pd.Timestamp("2026-01-01T00:05:00Z")
    assert by_key[("T_A-1", first_bucket)] == pytest.approx(14.0 / 5)
    assert by_key[("T_B-1", first_bucket)] == pytest.approx(-3.0 / 5)
    assert by_key[("T_A-1", second_bucket)] == pytest.approx(100.0 / 5)


def test_spot_time_bm_unit_delta_5min_empty_input_has_expected_columns():
    out = spot_time_bm_unit_delta_5min(pd.DataFrame())
    assert out.empty
    assert list(out.columns) == ["settlementDate", "settlementPeriod", "bmUnit", "startTime", "delta"]


def test_compute_stack_empty_boalf_returns_empty_frame():
    empty = pd.DataFrame()
    result = compute_stack(empty, empty, empty, empty, empty)
    assert result.empty


def test_compute_stack_empty_boalf_with_return_spot_niv_returns_empty_tuple():
    empty = pd.DataFrame()
    result, niv, unit_delta = compute_stack(empty, empty, empty, empty, empty, return_spot_niv=True)
    assert result.empty
    assert niv.empty
    assert unit_delta.empty


def test_compute_stack_mel_gate_drops_unmatched_minute():
    """Without a matching MEL row for the same bmUnit+minute, the accepted
    action is dropped entirely when use_mel_gate=True (the default,
    matching the original notebook) -- and recovered when disabled.
    """
    sd = "2026-01-01"
    boalf_df = pd.DataFrame([_flat_acceptance("T_TEST-1", 10.0, sd)])
    pn_df = pd.DataFrame([{
        "bmUnit": "T_TEST-1", "timeFrom": f"{sd}T00:00:00Z", "timeTo": f"{sd}T00:01:00Z",
        "levelFrom": 0.0, "levelTo": 0.0, "dataset": "PN", "nationalGridBmUnit": "T_TEST-1",
        "settlementDate": sd, "settlementPeriod": 1,
    }])
    bod_df = pd.DataFrame([{
        "settlementDate": sd, "settlementPeriod": 1, "bmUnit": "T_TEST-1",
        "bid": -50.0, "offer": 75.0, "levelTo": 20.0, "pairId": 1,
    }])
    disbsad_df = pd.DataFrame(columns=["settlementDate", "settlementPeriod", "soFlag", "storFlag", "volume", "cost"])
    no_mel = pd.DataFrame(columns=["bmUnit", "timeFrom", "timeTo", "levelFrom", "levelTo", "notificationTime"])

    gated = compute_stack(boalf_df, bod_df, pn_df, no_mel, disbsad_df, use_mel_gate=True)
    assert gated.empty

    ungated = compute_stack(boalf_df, bod_df, pn_df, no_mel, disbsad_df, use_mel_gate=False)
    assert len(ungated) == 1


def _span_acceptance(bm_unit: str, acceptance_number: int, time_from: str, time_to: str, level: float, sd: str = "2026-01-01") -> dict:
    return {
        "bmUnit": bm_unit, "timeFrom": f"{sd}T{time_from}Z", "timeTo": f"{sd}T{time_to}Z",
        "levelFrom": level, "levelTo": level, "acceptanceTime": f"{sd}T{time_from}Z",
        "acceptanceNumber": acceptance_number, "settlementDate": sd, "settlementPeriodFrom": 1,
        "settlementPeriodTo": 1, "deemedBoFlag": False, "soFlag": False,
        "storFlag": False, "rrFlag": False,
    }


def test_compute_cadl_flags_flags_a_genuinely_isolated_short_episode():
    """A single acceptance governing only 5 minutes, with nothing before or
    after it for the same bmUnit, is a genuine isolated short action --
    CADL flagged (episode length 5 < 10).
    """
    boalf_df = pd.DataFrame([_span_acceptance("T_TEST-1", 1, "00:00:00", "00:05:00", 10.0)])
    flags = compute_cadl_flags(boalf_df)
    assert dict(zip(flags["acceptanceNumber"], flags["cadl_flag"])) == {1: True}


def test_compute_cadl_flags_does_not_flag_a_rolling_series_within_one_long_episode():
    """Real BOALF data routinely carries out ONE sustained action as a
    rolling series of short-lived acceptances, each handed off to the
    next every few minutes (confirmed against Elexon's own published
    cadlFlag via /balancing/settlement/stack/all -- see
    compute_cadl_flags()'s docstring). Three acceptances of 4, 3 and 5
    minutes each, back to back with no gaps, form one 12-minute episode --
    none of them should be flagged, even though each individual one is
    under the 10-minute CADL on its own.
    """
    boalf_df = pd.DataFrame([
        _span_acceptance("T_TEST-1", 1, "00:00:00", "00:04:00", 10.0),  # 4 min
        _span_acceptance("T_TEST-1", 2, "00:04:00", "00:07:00", 12.0),  # 3 min
        _span_acceptance("T_TEST-1", 3, "00:07:00", "00:12:00", 15.0),  # 5 min -- 12 min episode total
    ])
    flags = compute_cadl_flags(boalf_df)
    by_acceptance = dict(zip(flags["acceptanceNumber"], flags["cadl_flag"]))
    assert by_acceptance == {1: False, 2: False, 3: False}


def test_compute_cadl_flags_shown_separately_from_display_so_flag():
    """The *displayed* `soFlag` column must stay exactly as the raw BOALF
    dataset carries it (so a CADL-flagged-but-raw-unflagged action shows
    up on the unflagged side of the stack, per view.py's split_and_sort),
    with the computed CADL flag visible in its own `cadlFlag` column
    rather than folded into `soFlag` -- see compute_stack()'s own comment
    on why Classification/PAR Tagging eligibility still folds CADL in
    separately, via `firstStageFlag`, without touching this display field.
    """
    sd = "2026-01-01"
    boalf_df = pd.DataFrame([_span_acceptance("T_TEST-1", 1, "00:00:00", "00:05:00", 10.0, sd)])  # isolated 5 min -> CADL flagged
    pn_df = pd.DataFrame([{
        "bmUnit": "T_TEST-1", "timeFrom": f"{sd}T00:00:00Z", "timeTo": f"{sd}T00:05:00Z",
        "levelFrom": 0.0, "levelTo": 0.0, "dataset": "PN", "nationalGridBmUnit": "T_TEST-1",
        "settlementDate": sd, "settlementPeriod": 1,
    }])
    bod_df = pd.DataFrame([{
        "settlementDate": sd, "settlementPeriod": 1, "bmUnit": "T_TEST-1",
        "bid": -50.0, "offer": 75.0, "levelTo": 20.0, "pairId": 1,
    }])
    disbsad_df = pd.DataFrame(columns=["settlementDate", "settlementPeriod", "soFlag", "storFlag", "volume", "cost"])
    no_mel = pd.DataFrame(columns=["bmUnit", "timeFrom", "timeTo", "levelFrom", "levelTo", "notificationTime"])

    result = compute_stack(boalf_df, bod_df, pn_df, no_mel, disbsad_df, use_mel_gate=False)
    assert not result.empty
    # Raw soFlag on the acceptance itself is False, and stays False here --
    # the computed CADL flag is visible in its own column instead.
    assert bool(result.iloc[0]["soFlag"]) is False
    assert bool(result.iloc[0]["cadlFlag"]) is True


def test_reversal_aware_recovers_a_superseded_acceptances_own_reversal():
    """T_CDCL-1 SP35 2026-09-20 worked example, in miniature: acceptance 1
    holds flat at 100MW; a later acceptance (2) takes over at minute 5,
    raising the unit to 200MW. FPN ramps from 0 up to 150 over the whole
    window, crossing 100 partway through acceptance 1's own (superseded)
    remaining declared window -- "latest_wins" (default) can never see
    this, since acceptance 1 disappears from the reconstruction entirely
    once superseded at minute 5, but "reversal_aware" recovers it as a
    genuine bid-side contribution, exactly matching how Elexon's own real
    settlement stack treats this case (see build_marginal_deltas()'s own
    docstring).
    """
    sd = "2026-01-01"
    boalf_df = pd.DataFrame([
        _span_acceptance("T_TEST-1", 1, "00:00:00", "00:10:00", 100.0, sd),
        _span_acceptance("T_TEST-1", 2, "00:05:00", "00:15:00", 200.0, sd),
    ])
    pn_df = pd.DataFrame([{
        "bmUnit": "T_TEST-1", "timeFrom": f"{sd}T00:00:00Z", "timeTo": f"{sd}T00:10:00Z",
        "levelFrom": 0.0, "levelTo": 150.0, "dataset": "PN", "nationalGridBmUnit": "T_TEST-1",
        "settlementDate": sd, "settlementPeriod": 1,
    }])
    bod_df = pd.DataFrame([
        {"settlementDate": sd, "settlementPeriod": 1, "bmUnit": "T_TEST-1", "bid": -50.0, "offer": 100.0, "levelTo": 300.0, "pairId": 1},
        {"settlementDate": sd, "settlementPeriod": 1, "bmUnit": "T_TEST-1", "bid": 20.0, "offer": 100.0, "levelTo": -300.0, "pairId": -1},
    ])
    disbsad_df = pd.DataFrame(columns=["settlementDate", "settlementPeriod", "soFlag", "storFlag", "volume", "cost"])
    no_mel = pd.DataFrame(columns=["bmUnit", "timeFrom", "timeTo", "levelFrom", "levelTo", "notificationTime"])

    latest = compute_stack(boalf_df, bod_df, pn_df, no_mel, disbsad_df, use_mel_gate=False, overlap_resolution="latest_wins")
    aware = compute_stack(boalf_df, bod_df, pn_df, no_mel, disbsad_df, use_mel_gate=False)  # reversal_aware is now the default

    assert not (latest["bmUnit"] == "T_TEST-1_-1").any()

    bid_rows = aware[aware["bmUnit"] == "T_TEST-1_-1"]
    assert not bid_rows.empty
    assert (bid_rows["acceptanceNumber"] == 1).all()
    assert bid_rows["delta"].sum() < 0


def test_reversal_aware_does_not_double_count_a_same_level_revision():
    """A same-level revision (acceptance 2 re-confirms acceptance 1's own
    level, no actual change) must not count twice just because it's later
    superseded and independently priced against FPN -- this is exactly
    the failure mode a first attempt at this feature had (see
    _reversal_tail_rows()'s own docstring, hypothesis 1: it roughly
    doubled the period's total accepted volume). Total accepted volume
    under "reversal_aware" must match "latest_wins" exactly here, since
    neither acceptance's own marginal delta ever changes sign.
    """
    sd = "2026-01-01"
    boalf_df = pd.DataFrame([
        _span_acceptance("T_TEST-1", 1, "00:00:00", "00:10:00", 50.0, sd),
        _span_acceptance("T_TEST-1", 2, "00:05:00", "00:15:00", 50.0, sd),  # same level, no real change
    ])
    pn_df = pd.DataFrame([{
        "bmUnit": "T_TEST-1", "timeFrom": f"{sd}T00:00:00Z", "timeTo": f"{sd}T00:15:00Z",
        "levelFrom": 0.0, "levelTo": 10.0, "dataset": "PN", "nationalGridBmUnit": "T_TEST-1",
        "settlementDate": sd, "settlementPeriod": 1,
    }])
    bod_df = pd.DataFrame([
        {"settlementDate": sd, "settlementPeriod": 1, "bmUnit": "T_TEST-1", "bid": -50.0, "offer": 100.0, "levelTo": 300.0, "pairId": 1},
    ])
    disbsad_df = pd.DataFrame(columns=["settlementDate", "settlementPeriod", "soFlag", "storFlag", "volume", "cost"])
    no_mel = pd.DataFrame(columns=["bmUnit", "timeFrom", "timeTo", "levelFrom", "levelTo", "notificationTime"])

    latest = compute_stack(boalf_df, bod_df, pn_df, no_mel, disbsad_df, use_mel_gate=False, overlap_resolution="latest_wins")
    aware = compute_stack(boalf_df, bod_df, pn_df, no_mel, disbsad_df, use_mel_gate=False)  # reversal_aware is now the default

    assert aware["delta"].sum() == pytest.approx(latest["delta"].sum())


def test_reversal_side_fix_flag_is_currently_a_no_op():
    """`reversal_side_fix=True` is accepted (config.py's
    `reversal_side_fix_enabled` still wires through to it) but does nothing
    right now -- both tried implementations (override `base_value` only for
    reversal-flagged rows; override it for every row) were confirmed live
    to make the computed NIV worse against Elexon's real
    netImbalanceVolume, not better (see build_marginal_deltas()'s own
    docstring for the full evidence). Pending a real redesign, the flag is
    a pass-through so turning it on in .env.dev is safe -- it must produce
    byte-identical output to leaving it off.
    """
    sd = "2026-01-01"
    boalf_df = pd.DataFrame([
        _span_acceptance("T_TEST-1", 1, "00:00:00", "00:10:00", -20.0, sd),
        _span_acceptance("T_TEST-1", 2, "00:10:00", "00:20:00", 10.0, sd),
    ])
    pn_df = pd.DataFrame([{
        "bmUnit": "T_TEST-1", "timeFrom": f"{sd}T00:00:00Z", "timeTo": f"{sd}T00:20:00Z",
        "levelFrom": 50.0, "levelTo": 50.0, "dataset": "PN", "nationalGridBmUnit": "T_TEST-1",
        "settlementDate": sd, "settlementPeriod": 1,
    }])

    without_fix = build_marginal_deltas(boalf_df, pn_df, overlap_resolution="reversal_aware", reversal_side_fix=False)
    with_fix = build_marginal_deltas(boalf_df, pn_df, overlap_resolution="reversal_aware", reversal_side_fix=True)

    pd.testing.assert_frame_equal(without_fix, with_fix)


def test_disaggregate_disbsad_keeps_each_actions_own_price():
    """Two DISBSAD actions sharing (soFlag=False, storFlag=False): one at
    GBP100/MWh (2 MWh), one at GBP200/MWh (2 MWh) -- mirrors SP39
    2026-09-25 in miniature (34 real actions ranging GBP212.50-235.00/MWh,
    all blended into one row). Without the fix, blend_disbsad() pre-sums
    them into ONE row at the volume-weighted blend (GBP150/MWh here);
    PAR Tagging never sees the two real prices. With the fix, both
    survive as separate disbsad_ rows at their own real price -- confirmed
    live this reproduces Elexon's actual settlement price exactly on
    SP33/35/39 2026-09-25 (was off by GBP1.86-4.82/MWh without it).
    """
    sd = "2026-01-01"
    boalf_df = pd.DataFrame([_flat_acceptance("T_TEST-1", 10.0, sd)])
    pn_df = pd.DataFrame([{
        "bmUnit": "T_TEST-1", "timeFrom": f"{sd}T00:00:00Z", "timeTo": f"{sd}T00:01:00Z",
        "levelFrom": 0.0, "levelTo": 0.0, "dataset": "PN", "nationalGridBmUnit": "T_TEST-1",
        "settlementDate": sd, "settlementPeriod": 1,
    }])
    mel_df = pd.DataFrame([{
        "bmUnit": "T_TEST-1", "timeFrom": f"{sd}T00:00:00Z", "timeTo": f"{sd}T00:01:00Z",
        "levelFrom": 100.0, "levelTo": 100.0, "dataset": "MELS", "nationalGridBmUnit": "T_TEST-1",
        "settlementDate": sd, "settlementPeriod": 1, "notificationTime": f"{sd}T00:00:00Z",
        "notificationSequence": 1,
    }])
    bod_df = pd.DataFrame([{
        "settlementDate": sd, "settlementPeriod": 1, "bmUnit": "T_TEST-1",
        "bid": -50.0, "offer": 75.0, "levelTo": 20.0, "pairId": 1,
    }])
    disbsad_df = pd.DataFrame([
        {"id": 1, "settlementDate": sd, "settlementPeriod": 1, "soFlag": False, "storFlag": False, "volume": 2.0, "cost": 200.0},
        {"id": 2, "settlementDate": sd, "settlementPeriod": 1, "soFlag": False, "storFlag": False, "volume": 2.0, "cost": 400.0},
    ])

    blended = compute_stack(boalf_df, bod_df, pn_df, mel_df, disbsad_df, disaggregate_disbsad=False)
    disaggregated = compute_stack(boalf_df, bod_df, pn_df, mel_df, disbsad_df, disaggregate_disbsad=True)

    blended_disbsad = blended[blended["bmUnit"].str.startswith("disbsad_")]
    disaggregated_disbsad = disaggregated[disaggregated["bmUnit"].str.startswith("disbsad_")]

    assert len(blended_disbsad) == 1
    assert blended_disbsad.iloc[0]["m_orig_price"] == pytest.approx(150.0)  # volume-weighted blend

    assert len(disaggregated_disbsad) == 2
    assert sorted(disaggregated_disbsad["m_orig_price"]) == pytest.approx([100.0, 200.0])  # each action's own price


def test_allocate_volumes_fifth_band_gets_its_full_four_band_lookback():
    """build_bod_ladder()'s cumulative lower/higher bound for a unit's 5th
    (outermost, and entirely ordinary -- a BM unit may submit up to 5
    Bid-Offer Pairs per side, confirmed against Elexon's own documented
    limit) band must sum all FOUR bands before it, not just three.

    Reproduces T_LIONB-3's own real SP22 2026-09-30 bid ladder shape
    (bands -1..-4 at levels -10/-10/-10/-5, band -5 at level -100) and its
    real accepted level (-30, landing exactly on band -4's own boundary):
    with only a 3-band lookback, band -5's own lower_bound came out as -25
    (missing band -1's own -10) instead of the correct -35, wrongly
    overlapping band -4's range and letting -30 match band -5's own case-2
    mask -- attributing 5 MWh of real volume to a band Elexon's real
    settlement never allocates anything to for this unit/period at all
    (confirmed: every real row has parAdjustedVolume 0, band -5 never
    appears in the accepted stack).
    """
    sd = "2026-01-01"
    fpn_mel_boalf = pd.DataFrame([{
        "bmUnit": "T_LIONB-3", "settlementDate": sd, "settlementPeriod": 1,
        "acceptanceTime": f"{sd}T00:00:00Z", "spot_time": f"{sd}T00:00:00Z",
        "marginal_delta": -30.0, "base_value": 0.0, "band_reversal": 1.0,
        "fpn_spot_vol": 0.0, "boalf_spot_vol": -30.0,
    }])
    bod_df = pd.DataFrame([
        {"settlementDate": sd, "settlementPeriod": 1, "bmUnit": "T_LIONB-3", "bid": 87.0, "offer": 183.0, "levelTo": -10.0, "pairId": -1},
        {"settlementDate": sd, "settlementPeriod": 1, "bmUnit": "T_LIONB-3", "bid": 80.0, "offer": 183.0, "levelTo": -10.0, "pairId": -2},
        {"settlementDate": sd, "settlementPeriod": 1, "bmUnit": "T_LIONB-3", "bid": 80.0, "offer": 183.0, "levelTo": -10.0, "pairId": -3},
        {"settlementDate": sd, "settlementPeriod": 1, "bmUnit": "T_LIONB-3", "bid": 50.0, "offer": 183.0, "levelTo": -5.0, "pairId": -4},
        {"settlementDate": sd, "settlementPeriod": 1, "bmUnit": "T_LIONB-3", "bid": 0.0, "offer": 183.0, "levelTo": -100.0, "pairId": -5},
    ])

    dp = allocate_volumes(fpn_mel_boalf, bod_df)
    by_band = dict(zip(dp["pairId"], dp["vol_to_price"]))
    assert by_band.get(-5, 0) == 0  # no phantom volume in the untouched outermost band
    assert dp["delta"].sum() == pytest.approx(-30.0)  # full accepted volume still lands somewhere real


def test_build_bod_ladder_gives_the_fifth_band_its_own_four_prior_levels():
    sd = "2026-01-01"
    bod_df = pd.DataFrame([
        {"settlementDate": sd, "settlementPeriod": 1, "bmUnit": "T_X-1", "bid": 1.0, "offer": 1.0, "levelTo": -10.0, "pairId": -1},
        {"settlementDate": sd, "settlementPeriod": 1, "bmUnit": "T_X-1", "bid": 1.0, "offer": 1.0, "levelTo": -10.0, "pairId": -2},
        {"settlementDate": sd, "settlementPeriod": 1, "bmUnit": "T_X-1", "bid": 1.0, "offer": 1.0, "levelTo": -10.0, "pairId": -3},
        {"settlementDate": sd, "settlementPeriod": 1, "bmUnit": "T_X-1", "bid": 1.0, "offer": 1.0, "levelTo": -5.0, "pairId": -4},
        {"settlementDate": sd, "settlementPeriod": 1, "bmUnit": "T_X-1", "bid": 1.0, "offer": 1.0, "levelTo": -100.0, "pairId": -5},
    ])
    ladder = build_bod_ladder(bod_df, ["T_X-1"])
    band5 = ladder[ladder["pairId"] == -5].iloc[0]
    assert band5["levelTo1"] + band5["levelTo2"] + band5["levelTo3"] + band5["levelTo4"] == -35.0


def test_reversal_tail_sign_is_anchored_per_settlement_period():
    """T_WBURB-1 SP21 2026-10-02 in miniature: acceptance 1 holds 100MW
    across a period boundary while the FPN baseline drops from 300 (SP1:
    marginal negative) to 0 (SP2: marginal positive). Acceptance 2 then
    takes over at the same level partway into SP2. SP2's positive marginal
    is acceptance 1's ordinary first-direction in that period, NOT a
    reversal of SP1's negative one -- anchored across the whole window it
    looked like one and was added on top of the governing acceptance
    (+~46 MWh spurious NIV that live period). "reversal_aware" must match
    "latest_wins" exactly here.
    """
    sd = "2026-01-01"
    boalf_df = pd.DataFrame([
        _span_acceptance("T_TEST-1", 1, "00:10:00", "00:50:00", 100.0, sd),
        _span_acceptance("T_TEST-1", 2, "00:40:00", "01:10:00", 100.0, sd),
    ])
    pn_df = pd.DataFrame([
        {"bmUnit": "T_TEST-1", "timeFrom": f"{sd}T00:00:00Z", "timeTo": f"{sd}T00:30:00Z",
         "levelFrom": 300.0, "levelTo": 300.0, "dataset": "PN", "nationalGridBmUnit": "T_TEST-1",
         "settlementDate": sd, "settlementPeriod": 1},
        {"bmUnit": "T_TEST-1", "timeFrom": f"{sd}T00:30:00Z", "timeTo": f"{sd}T01:30:00Z",
         "levelFrom": 0.0, "levelTo": 0.0, "dataset": "PN", "nationalGridBmUnit": "T_TEST-1",
         "settlementDate": sd, "settlementPeriod": 2},
    ])

    latest = build_marginal_deltas(boalf_df, pn_df, overlap_resolution="latest_wins")
    aware = build_marginal_deltas(boalf_df, pn_df, overlap_resolution="reversal_aware")

    assert len(aware) == len(latest)
    assert (aware["boalf_spot_vol"] - aware["fpn_spot_vol"]).sum() == pytest.approx(
        (latest["boalf_spot_vol"] - latest["fpn_spot_vol"]).sum()
    )


def test_compute_stack_acceptance_model_prices_the_same_simple_case_and_keeps_the_stack_layout():
    sd = "2026-01-01"
    boalf_df = pd.DataFrame([_flat_acceptance("T_TEST-1", 10.0, sd)])
    pn_df = pd.DataFrame([{
        "bmUnit": "T_TEST-1", "timeFrom": f"{sd}T00:00:00Z", "timeTo": f"{sd}T00:01:00Z",
        "levelFrom": 0.0, "levelTo": 0.0, "dataset": "PN", "nationalGridBmUnit": "T_TEST-1",
        "settlementDate": sd, "settlementPeriod": 1,
    }])
    mel_df = pd.DataFrame([{
        "bmUnit": "T_TEST-1", "timeFrom": f"{sd}T00:00:00Z", "timeTo": f"{sd}T00:01:00Z",
        "levelFrom": 100.0, "levelTo": 100.0, "dataset": "MELS", "nationalGridBmUnit": "T_TEST-1",
        "settlementDate": sd, "settlementPeriod": 1, "notificationTime": f"{sd}T00:00:00Z", "notificationSequence": 1,
    }])
    bod_df = pd.DataFrame([{
        "settlementDate": sd, "settlementPeriod": 1, "bmUnit": "T_TEST-1",
        "bid": -50.0, "offer": 75.0, "levelTo": 20.0, "pairId": 1,
    }])
    disbsad_df = pd.DataFrame(columns=["settlementDate", "settlementPeriod", "soFlag", "storFlag", "volume", "cost"])

    result = compute_stack(boalf_df, bod_df, pn_df, mel_df, disbsad_df, acceptance_model=True)

    assert len(result) == 1
    row = result.iloc[0]
    assert row["m_orig_price"] == pytest.approx(75.0) and row["total_misik_price"] == pytest.approx(75.0)
    assert row["delta"] == pytest.approx(10 / 60, abs=1e-5) and row["total_delta"] == pytest.approx(10 / 60, abs=1e-5)
    assert row["bmUnit"] == "T_TEST-1_1"
    assert list(result.columns) == list(compute_stack(boalf_df, bod_df, pn_df, mel_df, disbsad_df).columns)
