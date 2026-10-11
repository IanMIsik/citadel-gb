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
