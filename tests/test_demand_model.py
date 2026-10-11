from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("lightgbm")

from citadel.engine import demand_features as feat  # noqa: E402
from citadel.engine import demand_model as dm  # noqa: E402

FAST = {"num_boost_round": 40, "learning_rate": 0.3, "num_leaves": 8, "min_data_in_leaf": 20, "num_threads": 2}


def synthetic_dataset(days=900, seed=5):
    """The error is driven by one visible feature (gen_nowcast_err) plus noise, so a model should find it."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2022-01-01", periods=48 * days, freq="30min", tz="UTC", name="target_start")
    signal = rng.normal(0, 300, len(idx))
    ds = pd.DataFrame({"lead_min": 0, "ndf": 30000.0, "gen_nowcast_err": signal + rng.normal(0, 30, len(idx)),
                       "noise_a": rng.normal(size=len(idx)), "err_known_0": rng.normal(0, 300, len(idx))}, index=idx)
    ds["y"] = signal + rng.normal(0, 100, len(idx))
    return ds


def test_walk_forward_trains_only_on_earlier_data_and_beats_the_forecast():
    ds = synthetic_dataset()
    preds, fitted = dm.walk_forward(ds, [2024], FAST)
    assert set(preds["fold"]) == {2024} and preds.index.min() >= pd.Timestamp("2024-01-01", tz="UTC")
    s = dm.score_predictions(preds).set_index("method")
    assert s.loc["model_q50", "mae"] < 0.6 * s.loc["ndf_as_published", "mae"]
    assert s.loc["model_q50", "mae_vs_ndf_pct"] < -40


def test_quantiles_are_ordered_and_the_band_is_roughly_calibrated():
    preds, _ = dm.walk_forward(synthetic_dataset(), [2024], {**FAST, "num_boost_round": 60})
    assert (preds["q10"] <= preds["q50"]).all() and (preds["q50"] <= preds["q90"]).all()
    cov = dm.score_predictions(preds).set_index("method").loc["band_coverage_10_90", "coverage"]
    assert 0.7 < cov < 0.9


def test_a_fold_without_earlier_data_is_skipped():
    ds = synthetic_dataset(days=100)
    preds, fitted = dm.walk_forward(ds, [2022], FAST)
    assert preds.empty and fitted == {}


def test_importance_finds_the_informative_feature_and_models_round_trip(tmp_path):
    ds = synthetic_dataset(days=300)
    cols = feat.feature_columns(ds)
    models = dm.fit_quantiles(ds, cols, FAST)
    imp = dm.feature_importance(models, cols)
    assert imp.iloc[0]["feature"] == "gen_nowcast_err"
    paths = dm.save_models(models, cols, tmp_path, {"rows": len(ds)})
    assert len(paths) == 3 and (tmp_path / "demand_gbm_meta.json").exists()
    import lightgbm as lgb

    reloaded = lgb.Booster(model_file=str(paths[1]))
    np.testing.assert_allclose(reloaded.predict(ds[cols].head(5)), models[0.5].predict(ds[cols].head(5)))


def narrow_band_predictions(days=500, seed=9):
    """Outcomes ~ N(0, 500) but the model's band is only +-300, so it covers well under 80%."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2024-01-01", periods=48 * days, freq="30min", tz="UTC", name="target_start")
    p = pd.DataFrame({"y": rng.normal(0, 500, len(idx)), "q10": -300.0, "q50": 0.0, "q90": 300.0, "lead_min": 0}, index=idx)
    p["fold"] = np.where(idx < pd.Timestamp("2024-09-01", tz="UTC"), 2024, 2025)
    return p


def test_static_offsets_widen_a_band_that_is_too_narrow_and_leave_a_good_one_alone():
    p = narrow_band_predictions()
    assert dm.static_offsets(p).loc[0] > 100                       # needs widening by a few hundred MW
    wide = p.assign(q10=-900.0, q90=900.0)
    assert dm.static_offsets(wide).loc[0] < 0                      # already too wide: the band may shrink


def test_calibrated_band_reaches_the_target_coverage_out_of_sample():
    cal = dm.calibrated_band(narrow_band_predictions())
    held_out = cal[cal["fold"] == 2025]
    s = dm.band_summary(held_out).set_index("band")
    assert s.loc["raw", "coverage"] < 0.6
    assert s.loc["roll", "coverage"] == pytest.approx(0.8, abs=0.03)
    assert s.loc["static", "coverage"] == pytest.approx(0.8, abs=0.04)
    assert s.loc["roll", "width_mw"] > s.loc["raw", "width_mw"]


def test_rolling_offset_never_uses_outcomes_that_were_not_yet_published():
    from citadel.engine import demand_eval as ev

    p = narrow_band_predictions(days=120)
    base = dm.rolling_offsets(p)
    i = 48 * 90 + 5
    steps = ev.known_steps(0)
    changed = p.copy()
    changed.iloc[i - steps + 1:, changed.columns.get_loc("y")] += 50_000.0     # periods not yet published at decision time
    after = dm.rolling_offsets(changed)
    key = (p.index[i], 0)
    assert base.loc[key] == after.loc[key]
    assert base.loc[(p.index[i + 20], 0)] != after.loc[(p.index[i + 20], 0)]    # but later rows do see them


def test_band_is_still_ordered_after_calibration():
    cal = dm.calibrated_band(narrow_band_predictions())
    assert (cal["q10_roll"] <= cal["q90_roll"]).all() and (cal["q10_static"] <= cal["q90_static"]).all()
