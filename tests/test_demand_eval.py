from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from citadel.engine import demand_eval as ev

START = pd.Timestamp("2025-01-01", tz="UTC")


def make_frame(n_days=40, bias=0.0, noise=0.0, leads=(0, 240), seed=1):
    """INDO = true demand; every lead's NDF = INDO - error, where error = a constant bias plus noise."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(START, periods=48 * n_days, freq="30min")
    indo = 30000 + 4000 * np.sin(np.arange(len(idx)) * 2 * np.pi / 48)
    vint, out = [], pd.DataFrame({"target_start": idx, "indo": indo})
    for lead in leads:
        err = bias + rng.normal(0, noise, len(idx))
        vint.append(pd.DataFrame({"target_start": idx, "lead_min": lead, "value": indo - err}))
    return ev.build_error_frame(pd.concat(vint), out)


def test_known_steps_means_the_period_has_ended_and_published():
    assert ev.known_steps(0) == 2          # 30 + 15 min needed: two rows back, never the period in progress
    assert ev.known_steps(30) == 3
    assert ev.known_steps(240) == 10


def test_frame_is_gap_free_and_has_one_column_per_lead():
    frame = make_frame(n_days=2)
    assert list(frame.columns) == ["ndf_L0", "ndf_L240", "indo"] or set(frame.columns) == {"ndf_L0", "ndf_L240", "indo"}
    assert frame.index.is_monotonic_increasing and (frame.index.to_series().diff().dropna() == pd.Timedelta("30min")).all()
    assert ev.leads_in(frame) == [0, 240]


def test_gaps_become_nan_rows_so_shifts_stay_in_half_hours():
    idx = pd.date_range(START, periods=6, freq="30min")
    v = pd.DataFrame({"target_start": idx.delete(2), "lead_min": 0, "value": 1.0})
    o = pd.DataFrame({"target_start": idx, "indo": 2.0})
    frame = ev.build_error_frame(v, o)
    assert len(frame) == 6 and np.isnan(frame["ndf_L0"].iloc[2])


def test_constant_bias_is_learned_by_the_trailing_methods():
    frame = make_frame(bias=500.0, noise=0.0)
    table = ev.evaluation_table(frame, leads=[0])
    s = ev.score(table).set_index("method")
    assert s.loc["ndf_as_published", "mae"] == pytest.approx(500.0)
    for m in ("persist_last", "mean_last_6", "ewm_hl4", "same_sp_28d"):
        assert s.loc[m, "mae"] < 1.0, m
    assert s.loc["persist_last", "mae_vs_ndf_pct"] < -99


def test_pure_noise_is_not_improved_by_chasing_it():
    frame = make_frame(bias=0.0, noise=400.0, n_days=60)
    s = ev.score(ev.evaluation_table(frame, leads=[0])).set_index("method")
    assert s.loc["persist_last", "mae"] > s.loc["ndf_as_published", "mae"]       # persistence adds noise
    assert s.loc["mean_last_6", "mae"] < s.loc["persist_last", "mae"]            # averaging helps


def test_corrections_never_use_a_period_that_was_not_yet_known():
    idx = pd.date_range(START, periods=200, freq="30min")
    err = pd.Series(0.0, index=idx)
    err.iloc[150] = 1000.0                          # one huge miss at row 150
    steps = ev.known_steps(240)                     # 10 rows
    corr = ev.corrections(err, steps)
    assert corr["persist_last"].iloc[150 + steps - 1] == 0.0       # not yet visible
    assert corr["persist_last"].iloc[150 + steps] == 1000.0        # visible exactly `steps` rows later
    assert corr["mean_last_6"].iloc[150 + steps - 1] == 0.0


def test_same_sp_needs_enough_history_and_stays_causal():
    idx = pd.date_range(START, periods=48 * 40, freq="30min")
    err = pd.Series(np.where(np.arange(len(idx)) % 48 == 10, 300.0, 0.0), index=idx)
    corr = ev.corrections(err, ev.known_steps(0))["same_sp_28d"]
    assert corr.iloc[:48 * 7].isna().all()                          # fewer than 7 days of history
    assert corr.iloc[48 * 20 + 10] == pytest.approx(300.0)           # the recurring miss at SP11 is learned
    assert corr.iloc[48 * 20 + 11] == pytest.approx(0.0)


def test_score_compares_methods_on_the_same_periods():
    frame = make_frame(bias=100.0, noise=50.0)
    s = ev.score(ev.evaluation_table(frame, leads=[0, 240]))
    counts = s.groupby("lead_min")["n"].nunique()
    assert (counts == 1).all()


def test_compare_with_live_reports_differences():
    t = pd.date_range(START, periods=5, freq="min")
    rep = pd.DataFrame({"spot_time": t, "misco_indo": [1.0, 2, 3, 4, 5], "misco_ndf": [1.0, 2, 3, 4, 5], "misco_indo_vs_ndf": [0.0] * 5})
    live = pd.DataFrame({"spot_time": t, "misco_indo": [1.0, 2, 3, 4, 105], "misco_ndf": [1.0, 2, 3, 4, 5], "misco_indo_vs_ndf": [0.0] * 5})
    res = ev.compare_with_live(rep, live).set_index("column")
    assert res.loc["misco_indo", "mean_abs_diff_mw"] == pytest.approx(20.0)
    assert res.loc["misco_indo", "within_50mw_pct"] == pytest.approx(80.0)
    assert res.loc["misco_ndf", "mean_abs_diff_mw"] == 0.0
