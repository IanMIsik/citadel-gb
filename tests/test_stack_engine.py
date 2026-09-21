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

from citadel.engine.stack import compute_cadl_flags, compute_stack, custom_round, vectorized_exploder


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


def test_compute_stack_empty_boalf_returns_empty_frame():
    empty = pd.DataFrame()
    result = compute_stack(empty, empty, empty, empty, empty)
    assert result.empty


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


def test_compute_cadl_flags_folds_into_so_flag():
    """compute_stack() must OR the computed CADL flag into `soFlag` itself
    (per the guide, SO-Flagged and CADL Flagged actions are both First
    Stage Flagged and get identical downstream treatment) rather than
    tracking it separately.
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
    # Raw soFlag on the acceptance itself is False -- only the computed
    # CADL flag should make the resulting row's soFlag True.
    assert bool(result.iloc[0]["soFlag"]) is True


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
