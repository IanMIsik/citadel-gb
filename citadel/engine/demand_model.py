"""The first demand-error model (dev only): gradient-boosted quantile regression.

It predicts error = INDO - NDF(lead) for a period, as a median plus a 10-90% band, from the as-of features in
demand_features. One pooled model per quantile, with the lead time as a feature. Evaluation is walk-forward:
each test year is predicted by a model trained only on earlier data (with a two-day gap), and scored against the
published NDF and the best simple baseline on exactly the same periods.

Needs LightGBM (`pip install lightgbm`, the `forecast` extra); the app itself never imports this module.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from citadel.engine import demand_features as feat

QUANTILES = (0.1, 0.5, 0.9)
PURGE = pd.Timedelta(days=2)
DEFAULT_PARAMS = dict(
    objective="quantile", learning_rate=0.05, num_leaves=63, min_data_in_leaf=200, bagging_fraction=0.8, bagging_freq=1,
    feature_fraction=0.8, lambda_l2=1.0, verbosity=-1, num_threads=0, seed=7,
)
DEFAULT_ROUNDS = 400


def _lgb():
    try:
        import lightgbm
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("LightGBM is required: .venv/Scripts/python.exe -m pip install lightgbm") from e
    return lightgbm


def fit_quantiles(train: pd.DataFrame, columns: list[str], params: dict | None = None, quantiles=QUANTILES) -> dict[float, object]:
    """One LightGBM booster per quantile (native API, so scikit-learn is not needed). `params` may carry
    `num_boost_round`; everything else overrides DEFAULT_PARAMS."""
    lgb = _lgb()
    params = dict(params or {})
    rounds = int(params.pop("num_boost_round", DEFAULT_ROUNDS))
    models = {}
    for q in quantiles:
        data = lgb.Dataset(train[columns], label=train["y"], feature_name=list(columns), free_raw_data=False)
        models[q] = lgb.train({**DEFAULT_PARAMS, **params, "alpha": q}, data, num_boost_round=rounds)
    return models


def predict_quantiles(models: dict[float, object], X: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    out = pd.DataFrame({f"q{int(q * 100)}": m.predict(X[columns]) for q, m in models.items()}, index=X.index)
    # Independently trained quantiles can cross; sort each row so the band is always ordered.
    cols = sorted(out.columns, key=lambda c: int(c[1:]))
    out[cols] = np.sort(out[cols].to_numpy(), axis=1)
    return out


def fold_bounds(years: list[int]) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    return [(pd.Timestamp(f"{y}-01-01", tz="UTC"), pd.Timestamp(f"{y + 1}-01-01", tz="UTC")) for y in years]


def walk_forward(ds: pd.DataFrame, test_years: list[int], params: dict | None = None, quantiles=QUANTILES) -> tuple[pd.DataFrame, dict]:
    """Predict each test year with a model trained only on data that ended `PURGE` before it started.
    Returns (predictions frame, {year: fitted models}). The frame has y, ndf, err_known_0 (the persistence
    baseline's correction), lead_min, fold and q10/q50/q90."""
    columns = feat.feature_columns(ds)
    preds, fitted = [], {}
    for year, (lo, hi) in zip(test_years, fold_bounds(test_years)):
        train = ds[ds.index < lo - PURGE]
        test = ds[(ds.index >= lo) & (ds.index < hi)]
        if train.empty or test.empty:
            continue
        models = fit_quantiles(train, columns, params, quantiles)
        p = predict_quantiles(models, test, columns)
        p["y"], p["ndf"], p["lead_min"] = test["y"], test["ndf"], test["lead_min"]
        p["persist"] = test["err_known_0"]
        p["fold"] = year
        preds.append(p)
        fitted[year] = models
    return (pd.concat(preds) if preds else pd.DataFrame()), fitted


def score_predictions(p: pd.DataFrame, by: list[str] | None = None) -> pd.DataFrame:
    """MAE/RMSE/bias per method, plus the band's coverage, on the rows where every method has a number."""
    base = p.dropna(subset=["y", "q50", "persist"])
    keys = by or ["lead_min"]
    rows = []
    for key, g in base.groupby(keys):
        key = key if isinstance(key, tuple) else (key,)
        resid = {
            "ndf_as_published": g["y"],
            "persist_last": g["y"] - g["persist"],
            "model_q50": g["y"] - g["q50"],
        }
        for name, r in resid.items():
            rows.append({**dict(zip(keys, key)), "method": name, "n": len(r), "mae": r.abs().mean(),
                         "rmse": float(np.sqrt((r ** 2).mean())), "bias": r.mean()})
        rows.append({**dict(zip(keys, key)), "method": "band_coverage_10_90", "n": len(g), "mae": np.nan, "rmse": np.nan, "bias": np.nan,
                     "coverage": float(((g["y"] >= g["q10"]) & (g["y"] <= g["q90"])).mean())})
    out = pd.DataFrame(rows)
    ref = out[out["method"] == "ndf_as_published"].set_index(keys)["mae"]
    out["mae_vs_ndf_pct"] = [
        100.0 * (r["mae"] / ref.loc[tuple(r[k] for k in keys) if len(keys) > 1 else r[keys[0]]] - 1.0) if r["method"] != "band_coverage_10_90" else np.nan
        for r in out.to_dict("records")]
    return out


def feature_importance(models: dict[float, object], columns: list[str], top: int = 20) -> pd.DataFrame:
    """Gain-based importance of the median model, as a share of the total."""
    gain = pd.Series(models[0.5].feature_importance(importance_type="gain"), index=columns)
    share = (gain / gain.sum()).sort_values(ascending=False).head(top)
    return share.rename("share_of_gain").reset_index().rename(columns={"index": "feature"})


def save_models(models: dict[float, object], columns: list[str], directory: str | Path, meta: dict | None = None) -> list[Path]:
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    paths = []
    for q, m in models.items():
        path = d / f"demand_gbm_q{int(q * 100)}.txt"
        m.save_model(str(path))
        paths.append(path)
    (d / "demand_gbm_meta.json").write_text(json.dumps({"features": columns, "quantiles": list(models), **(meta or {})}, indent=2))
    return paths


# ---------------------------------------------------------------------------------------------
# Loading everything from the database
# ---------------------------------------------------------------------------------------------

async def load_base(pool, start=None, end=None) -> pd.DataFrame:
    """Read the demand_* tables and join them into the half-hourly base frame."""
    async def q(sql: str, *args) -> pd.DataFrame:
        return pd.DataFrame([dict(r) for r in await pool.fetch(sql, *args)])

    rng = "AND ($1::timestamptz IS NULL OR {col} >= $1) AND ($2::timestamptz IS NULL OR {col} < $2)"
    ndf = await q("SELECT target_start, lead_min, value FROM demand_forecast_vintages WHERE dataset = 'NDF' " + rng.format(col="target_start"), start, end)
    windfor = await q("SELECT target_start, lead_min, value FROM demand_forecast_vintages WHERE dataset = 'WINDFOR' " + rng.format(col="target_start"), start, end)
    embedded = await q("SELECT target_start, lead_min, wind_forecast, wind_capacity, solar_forecast, solar_capacity FROM demand_embedded_vintages WHERE true "
                       + rng.format(col="target_start"), start, end)
    outturn = await q("SELECT target_start, indo FROM demand_outturn WHERE true " + rng.format(col="target_start"), start, end)
    pv = await q("SELECT ts, pv_live FROM demand_pv_live WHERE true " + rng.format(col="ts"), start, end)
    fuel = await q("SELECT start_time, total_mw, (by_fuel->>'WIND')::float8 AS wind_mw FROM demand_fuelinst_5min WHERE true " + rng.format(col="start_time"), start, end)
    weather = await q("SELECT point, ts, temperature_2m, cloud_cover, shortwave_radiation, wind_speed_100m FROM demand_weather WHERE true " + rng.format(col="ts"), start, end)
    hol = await q("SELECT day FROM demand_calendar WHERE region = 'england-and-wales'")
    return feat.build_base(ndf=ndf, outturn=outturn, embedded=embedded, windfor=windfor, pv_live=pv, fuel5=fuel,
                           weather=weather, holidays=hol["day"].tolist() if not hol.empty else [])
