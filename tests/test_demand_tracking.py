from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from citadel.engine import demand_tracking as tr

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def record(start_offset_min, lead=60, pred=10.0, ndf=25000.0, **over):
    start = NOW + timedelta(minutes=start_offset_min)
    base = {"start": start.isoformat(), "lead_used_min": lead, "ndf": ndf, "pred_error": pred, "pred_error_low": pred - 100,
            "pred_error_high": pred + 120, "raw_low": pred - 80, "raw_high": pred + 90}
    return {**base, **over}


def test_log_rows_carry_the_lead_at_prediction_and_skip_incomplete_records():
    rows = tr.log_rows([record(90, lead=120), record(-10, lead=0), record(30, pred_error=None)], NOW, "2026-10-01")
    assert len(rows) == 2
    ahead, current = rows
    assert ahead[1] == 120 and ahead[3] == 90.0 and ahead[5] == 10.0 and ahead[-1] == "2026-10-01"
    assert current[3] == -10.0                                        # made ten minutes after the period started


def scored(n=400, lead=60, bias=0.0, width=100.0, seed=1, lead_at=45.0, noise=300.0):
    rng = np.random.default_rng(seed)
    actual = rng.normal(0, noise, n)
    pred = np.zeros(n)
    return pd.DataFrame({"target_start": pd.date_range("2026-09-01", periods=n, freq="30min", tz="UTC"), "lead_min": lead,
                         "lead_at_prediction_min": lead_at, "pred_error": pred + bias, "actual_error": actual,
                         "raw_low": pred - width, "raw_high": pred + width, "pred_low": pred - width, "pred_high": pred + width})


def test_tracking_summary_compares_the_model_with_the_published_ndf():
    df = scored(bias=0.0, noise=300.0)
    df["actual_error"] = 200.0 + np.random.default_rng(2).normal(0, 20, len(df))      # a steady miss the model predicted
    df["pred_error"] = 200.0
    s = tr.tracking_summary(df).iloc[0]
    assert s["lead_min"] == 60 and s["n"] == 400
    assert s["ndf_mae"] == pytest.approx(200.0, rel=0.05) and s["model_mae"] < 25 and s["mae_vs_ndf_pct"] < -85
    assert abs(s["model_bias"]) < 5


def test_tracking_ignores_predictions_made_after_the_period_was_over():
    live = scored(n=50, lead_at=10.0)
    retrospective = scored(n=50, lead_at=-120.0, seed=3)
    both = pd.concat([live, retrospective])
    assert tr.tracking_summary(both).iloc[0]["n"] == 50
    assert tr.tracking_summary(retrospective).empty


def test_logged_offsets_need_enough_live_history_and_widen_a_band_that_is_too_narrow():
    narrow = scored(n=600, width=100.0, noise=500.0)                   # outcomes ~N(0,500) vs a +-100 band
    assert tr.logged_offsets(scored(n=100)) == {}                      # too little history: keep the model's own offset
    off = tr.logged_offsets(narrow)
    assert off[60] > 300
    wide = scored(n=600, width=2000.0, noise=500.0)
    assert tr.logged_offsets(wide)[60] < 0                             # an over-wide band may shrink


def test_logged_offsets_use_only_live_rows():
    df = pd.concat([scored(n=600, noise=500.0), scored(n=600, noise=5000.0, lead_at=-300.0, seed=4)])
    assert tr.logged_offsets(df)[60] < 1000                            # the hindsight rows with huge errors are not counted


def test_tracking_route_returns_the_summary(monkeypatch):
    from citadel.api import demand_forecast as route

    df = scored(n=40)

    async def fake_scores(pool, since):
        return df.to_dict("records")

    monkeypatch.setattr(route.db, "demand_prediction_scores", fake_scores)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(pool=object())))
    out = asyncio.run(route.demand_forecast_tracking(request, days=7))
    assert out["days"] == 7 and out["scored_live_predictions"] == 40 and out["by_lead"][0]["lead_min"] == 60


def test_predictor_records_include_the_raw_band_for_logging():
    from citadel.engine import demand_live as live
    from tests.test_demand_features import synthetic_base
    from tests.test_demand_live import at, fake_model

    base = synthetic_base()
    recs = live.predict_periods(base, fake_model(base, {0: 25.0}), at(base, 48 * 30) + pd.Timedelta(minutes=5), leads=(0, 240))
    cur = [r for r in recs if r["lead_used_min"] == 0][0]
    assert cur["raw_low"] == -100.0 and cur["raw_high"] == 120.0 and cur["band_widen_mw"] == 25.0
    assert cur["pred_error_low"] == cur["raw_low"] - 25.0
