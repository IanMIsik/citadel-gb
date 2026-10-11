from __future__ import annotations

import numpy as np
import pandas as pd

from citadel.engine import demand_features as feat
from citadel.engine import demand_live as live
from tests.test_demand_features import synthetic_base


class FakeBooster:
    def __init__(self, value):
        self.value = value

    def predict(self, X):
        return np.full(len(X), self.value, dtype=float)


def fake_model(base, offsets=None):
    columns = list(feat.lead_features(base, 0)[0].columns)
    return live.LoadedModel(columns, {0.1: FakeBooster(-100.0), 0.5: FakeBooster(10.0), 0.9: FakeBooster(120.0)}, offsets or {}, "2026-10-01")


def at(base, i):
    return base.index[i].to_pydatetime()


def test_lead_bucket_rounds_up_to_the_next_trained_lead():
    now = pd.Timestamp("2026-10-11 10:00", tz="UTC").to_pydatetime()
    plus = lambda minutes: now + pd.Timedelta(minutes=minutes)          # noqa: E731
    assert live.lead_bucket_for(plus(95), now) == 120                    # 95 min away uses the 120-minute row
    assert live.lead_bucket_for(plus(120), now) == 120
    assert live.lead_bucket_for(plus(121), now) == 240
    assert live.lead_bucket_for(plus(0), now) == 0
    assert live.lead_bucket_for(plus(-45), now) == 0                     # already started or ended
    assert live.lead_bucket_for(plus(1080), now) == 1080
    assert live.lead_bucket_for(plus(1081), now) is None


def test_predictions_cover_the_window_use_the_right_lead_and_widen_the_band():
    base = synthetic_base()
    now_idx = 48 * 30
    now = at(base, now_idx) + pd.Timedelta(minutes=10)                  # ten minutes into a period
    base.iloc[now_idx + 1:, base.columns.get_loc("indo")] = np.nan      # nothing beyond the period in progress is published
    recs = live.predict_periods(base, fake_model(base, {0: 25.0, 240: 40.0}), now, leads=(0, 240))
    starts = [pd.Timestamp(r["start"]) for r in recs]
    assert starts == sorted(starts) and starts[0] >= pd.Timestamp(now) - pd.Timedelta(hours=3, minutes=30)
    by = {pd.Timestamp(r["start"]): r for r in recs}
    current = by[base.index[now_idx]]
    assert current["lead_used_min"] == 0 and current["pred_error"] == 10.0
    assert current["pred_error_low"] == -125.0 and current["pred_error_high"] == 145.0       # widened by the lead-0 offset
    assert current["pred_demand"] == current["ndf"] + 10.0
    ahead = by[base.index[now_idx + 4]]                                  # two hours ahead -> only 0 and 240 leads trained -> 240
    assert ahead["lead_used_min"] == 240 and ahead["pred_error_low"] == -140.0 and ahead["actual_error"] is None
    past = by[base.index[now_idx - 3]]
    assert past["lead_used_min"] == 0 and past["actual_error"] is not None


def test_records_are_json_safe_and_carry_settlement_fields():
    import json

    base = synthetic_base()
    recs = live.predict_periods(base, fake_model(base), at(base, 48 * 30) + pd.Timedelta(minutes=5), leads=(0, 240))
    json.dumps(recs, allow_nan=False)
    assert {"settlement_date", "settlement_period", "start", "lead_used_min", "ndf", "pred_error"} <= set(recs[0])


def test_periods_without_an_ndf_are_skipped_not_guessed():
    base = synthetic_base()
    now_idx = 48 * 30
    base.loc[base.index[now_idx + 2], ["ndf_L0", "ndf_L240"]] = np.nan
    recs = live.predict_periods(base, fake_model(base), at(base, now_idx) + pd.Timedelta(minutes=5), leads=(0, 240))
    assert pd.Timestamp(base.index[now_idx + 2]).isoformat() not in [r["start"] for r in recs]


def test_no_trained_leads_in_the_data_gives_no_predictions():
    base = synthetic_base()
    assert live.predict_periods(base, fake_model(base), at(base, 48 * 30), leads=(60,)) == []


def test_route_starts_the_predictor_lazily_and_returns_its_state():
    import asyncio
    from types import SimpleNamespace

    from citadel.api import demand_forecast as route

    class FakePredictor:
        def __init__(self, pool):
            self.state, self.started = {"enabled": True, "status": "ok", "periods": []}, 0

        async def ensure_started(self):
            self.started += 1

    route.DemandPredictor = FakePredictor
    app = SimpleNamespace(state=SimpleNamespace(pool=object()))
    request = SimpleNamespace(app=app)
    first = asyncio.run(route.demand_forecast(request))
    asyncio.run(route.demand_forecast(request))
    assert first["status"] == "ok" and app.state.demand_predictor.started == 2
