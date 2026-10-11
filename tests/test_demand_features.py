from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from citadel.engine import demand_eval as ev
from citadel.engine import demand_features as feat

START = pd.Timestamp("2025-03-01", tz="UTC")
N = 48 * 40


def synthetic_base(seed=3):
    rng = np.random.default_rng(seed)
    idx = pd.date_range(START, periods=N, freq="30min", name="target_start")
    t = np.arange(N)
    base = pd.DataFrame(index=idx)
    base["indo"] = 30000 + 4000 * np.sin(t * 2 * np.pi / 48) + rng.normal(0, 300, N)
    for lead in (0, 240):
        base[f"ndf_L{lead}"] = base["indo"] + rng.normal(0, 400, N)
        base[f"ewind_L{lead}"] = rng.uniform(500, 3000, N)
        base[f"esolar_L{lead}"] = np.clip(rng.normal(2000, 800, N), 0, None)
        base[f"wfor_L{lead}"] = rng.uniform(1000, 9000, N)
    base["ewind_cap"], base["esolar_cap"] = 6500.0, 16000.0
    base["pv"] = np.clip(rng.normal(2000, 900, N), 0, None)
    base["fuel_mean"] = base["indo"] + 600 + rng.normal(0, 200, N)
    base["fuel_wind_mean"] = rng.uniform(1000, 9000, N)
    for c in ("temp", "cloud", "rad", "wspd", "cloud_spread"):
        base[c] = rng.normal(10, 3, N)
    base.attrs["holidays"] = [pd.Timestamp("2025-03-10").date()]
    return base


@pytest.mark.parametrize("lead", [0, 240])
def test_features_do_not_see_the_future(lead):
    """For a row q and lead L, change every value that is NOT yet known at decision time T = start(q) - L
    and check row q's features are identical."""
    base = synthetic_base()
    X0, _ = feat.lead_features(base, lead)
    i = 48 * 25 + 7                                  # one period, well inside the data
    steps, h = ev.known_steps(lead), lead // 30
    changed = base.copy()
    # INDO and PV_Live of periods not yet published (the newest `steps - 1` periods before q, q itself and later)
    for col in ("indo", "pv"):
        changed.iloc[i - steps + 1:, changed.columns.get_loc(col)] += 12345.0
    # FUELINST only runs up to T: the period that ended at T is the last one visible
    for col in ("fuel_mean", "fuel_wind_mean"):
        changed.iloc[i - h:, changed.columns.get_loc(col)] += 12345.0
    # forecasts for later periods and every vintage of periods after q
    after = i + 1
    for col in [c for c in base.columns if c.startswith(("ndf_L", "ewind_L", "esolar_L", "wfor_L"))] + ["temp", "cloud", "rad", "wspd"]:
        changed.iloc[after:, changed.columns.get_loc(col)] += 999.0
    X1, _ = feat.lead_features(changed, lead)
    a, b = X0.iloc[i], X1.iloc[i]
    moved = [c for c in X0.columns if not (pd.isna(a[c]) and pd.isna(b[c])) and a[c] != b[c]]
    # error history for q's own row legitimately uses indo of old periods only; q's target y is separate
    assert moved == [], f"features that changed when only the future changed: {moved}"


def test_target_is_indo_minus_the_leads_own_vintage():
    base = synthetic_base()
    X, y = feat.lead_features(base, 240)
    assert np.allclose(y.dropna(), (base["indo"] - base["ndf_L240"]).loc[y.dropna().index])
    assert X["lead_min"].eq(240).all() and "y" not in X.columns


def test_calendar_flags_holidays_weekends_and_clock_change():
    idx = pd.date_range("2025-03-28", "2025-04-02", freq="30min", tz="UTC")           # spans the 30 Mar clock change
    cal = feat.calendar_features(idx, [pd.Timestamp("2025-03-31").date()])
    assert cal.loc["2025-03-31 12:00", "is_holiday"] == 1 and cal.loc["2025-03-30 12:00", "holiday_tomorrow"] == 1
    assert cal.loc["2025-03-30 12:00", "is_weekend"] == 1 and cal.loc["2025-03-29 12:00", "clock_change_day"] == 0
    assert cal.loc["2025-03-30 12:00", "clock_change_day"] == 1


def test_build_dataset_stacks_leads_and_drops_rows_without_a_target():
    base = synthetic_base()
    base.loc[base.index[:10], "indo"] = np.nan
    ds = feat.build_dataset(base)
    assert set(ds["lead_min"]) == {0, 240} and ds["y"].notna().all()
    assert "y" not in feat.feature_columns(ds)


def test_build_base_reindexes_pv_live_to_period_start():
    ts = pd.date_range("2025-06-01 10:30", periods=3, freq="30min", tz="UTC")           # end-stamped
    pv = pd.DataFrame({"ts": ts, "pv_live": [1.0, 2.0, 3.0]})
    out = pd.DataFrame({"target_start": pd.date_range("2025-06-01 10:00", periods=4, freq="30min", tz="UTC"), "indo": 1.0})
    ndf = pd.DataFrame({"target_start": out["target_start"], "lead_min": 0, "value": 1.0})
    empty = pd.DataFrame()
    base = feat.build_base(ndf=ndf, outturn=out, embedded=empty, windfor=empty, pv_live=pv, fuel5=empty, weather=empty, holidays=[])
    assert base["pv"].tolist()[:3] == [1.0, 2.0, 3.0]          # the 10:30-stamped value is the 10:00 period
