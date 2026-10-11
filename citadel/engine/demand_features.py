"""Features for the demand-error model (dev only; reads the `demand_*` history tables).

Everything is laid out on one gap-free half-hourly UTC grid (a row = a period start), so "the period p
rows back" is always `series.shift(p)`. For a forecast made `lead` minutes before period q (decision time
T = start(q) - lead) a feature may use:

  * anything published about period q itself *before T*: its NDF / embedded / WINDFOR vintage at that lead
    (the vintage tables are already "latest published at least `lead` minutes earlier"), the weather for q;
  * INDO / PV_Live of periods that ended at least `publish_lag_min` before T (`known_steps`);
  * FUELINST (real time, no publish lag) up to T;
  * the calendar.

Nothing else. tests/test_demand_features.py perturbs every value that would be in the future at T and
checks the features do not move.

PV_Live's timestamp is the *end* of its half hour (checked against NESO's embedded solar estimate: shifted
back 30 minutes the two agree to correlation 1.000), so it is re-indexed to period start here.
Published NDF and INDO already include embedded wind and solar; the embedded series are inputs that help
explain the error, never an adjustment to either figure.
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import pandas as pd

from citadel.engine.demand_eval import DEFAULT_PUBLISH_LAG_MIN, PERIOD_MIN, known_steps

LONDON = "Europe/London"
PERIODS_PER_DAY = 48


def _wide(df: pd.DataFrame, value_col: str, prefix: str) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    d = df.copy()
    d["target_start"] = pd.to_datetime(d["target_start"], utc=True)
    w = d.pivot_table(index="target_start", columns="lead_min", values=value_col, aggfunc="last")
    w.columns = [f"{prefix}{int(c)}" for c in w.columns]
    return w


def build_base(*, ndf: pd.DataFrame, outturn: pd.DataFrame, embedded: pd.DataFrame, windfor: pd.DataFrame,
               pv_live: pd.DataFrame, fuel5: pd.DataFrame, weather: pd.DataFrame, holidays: Iterable) -> pd.DataFrame:
    """Join every source onto the half-hourly grid.

    ndf / windfor: target_start, lead_min, value.   embedded: target_start, lead_min, wind_forecast,
    wind_capacity, solar_forecast, solar_capacity.   outturn: target_start, indo.   pv_live: ts, pv_live.
    fuel5: start_time, total_mw, wind_mw.   weather: point, ts, temperature_2m, cloud_cover,
    shortwave_radiation, wind_speed_100m.   holidays: England & Wales bank-holiday dates."""
    out = outturn.copy()
    out["target_start"] = pd.to_datetime(out["target_start"], utc=True)
    parts = [out.set_index("target_start")[["indo"]], _wide(ndf, "value", "ndf_L")]
    for col, prefix in (("wind_forecast", "ewind_L"), ("solar_forecast", "esolar_L")):
        parts.append(_wide(embedded, col, prefix))
    base = pd.concat([p for p in parts if not p.empty], axis=1).sort_index()
    if base.empty:
        return base
    idx = pd.date_range(base.index.min(), base.index.max(), freq=f"{PERIOD_MIN}min", tz="UTC")
    base = base.reindex(idx)
    base.index.name = "target_start"

    if not embedded.empty:
        lead0 = embedded[embedded["lead_min"] == embedded["lead_min"].min()].copy()
        lead0["target_start"] = pd.to_datetime(lead0["target_start"], utc=True)
        lead0 = lead0.set_index("target_start")
        base["ewind_cap"] = lead0["wind_capacity"].reindex(idx)
        base["esolar_cap"] = lead0["solar_capacity"].reindex(idx)

    wf = _wide(windfor, "value", "wfor_L")
    for c in wf.columns:                                  # hourly forecasts -> each half hour takes its hour's value
        base[c] = wf[c].reindex(idx.floor("h")).to_numpy()

    if not pv_live.empty:
        pv = pv_live.copy()
        pv["ts"] = pd.to_datetime(pv["ts"], utc=True) - pd.Timedelta(minutes=PERIOD_MIN)   # end-stamped -> period start
        base["pv"] = pv.set_index("ts")["pv_live"].reindex(idx)

    if not fuel5.empty:
        f = fuel5.copy()
        f["start_time"] = pd.to_datetime(f["start_time"], utc=True)
        f = f.set_index("start_time").sort_index()
        grouped = f.groupby(f.index.floor(f"{PERIOD_MIN}min"))
        base["fuel_mean"] = grouped["total_mw"].mean().reindex(idx)
        if "wind_mw" in f.columns:
            base["fuel_wind_mean"] = grouped["wind_mw"].mean().reindex(idx)

    if not weather.empty:
        w = weather.copy()
        w["ts"] = pd.to_datetime(w["ts"], utc=True)
        names = {"temperature_2m": "temp", "cloud_cover": "cloud", "shortwave_radiation": "rad", "wind_speed_100m": "wspd"}
        for col, name in names.items():
            piv = w.pivot_table(index="ts", columns="point", values=col, aggfunc="last")
            hourly_mean = piv.mean(axis=1)
            base[name] = hourly_mean.reindex(hourly_mean.index.union(idx)).interpolate(limit=3).reindex(idx)
            if name == "cloud":
                spread = piv.std(axis=1)
                base["cloud_spread"] = spread.reindex(spread.index.union(idx)).interpolate(limit=3).reindex(idx)

    base.attrs["holidays"] = sorted({pd.Timestamp(h).date() for h in holidays})
    return base


def _lead_cols(base: pd.DataFrame, prefix: str) -> list[int]:
    return sorted(int(c.removeprefix(prefix)) for c in base.columns if c.startswith(prefix) and c[len(prefix):].isdigit())


def available_leads(base: pd.DataFrame) -> list[int]:
    return _lead_cols(base, "ndf_L")


def calendar_features(index: pd.DatetimeIndex, holidays: Iterable) -> pd.DataFrame:
    local = index.tz_convert(LONDON)
    day = pd.DatetimeIndex(local.date)
    hol = pd.DatetimeIndex(sorted({pd.Timestamp(h) for h in holidays}))
    is_hol = day.isin(hol)
    minutes = local.hour * 60 + local.minute
    cal = pd.DataFrame(index=index)
    cal["local_minutes"] = minutes
    cal["dow"] = local.dayofweek
    cal["month"] = local.month
    cal["doy_sin"] = np.sin(2 * np.pi * local.dayofyear / 365.25)
    cal["doy_cos"] = np.cos(2 * np.pi * local.dayofyear / 365.25)
    cal["is_weekend"] = (local.dayofweek >= 5).astype(int)
    cal["is_holiday"] = is_hol.astype(int)
    cal["holiday_tomorrow"] = (day + pd.Timedelta(days=1)).isin(hol).astype(int)
    cal["holiday_yesterday"] = (day - pd.Timedelta(days=1)).isin(hol).astype(int)
    # Clock-change days have 46 or 50 periods: flag them from the UTC offset difference across the day.
    offset = pd.Series(local.tz_localize(None) - index.tz_localize(None), index=index)
    cal["clock_change_day"] = (offset.groupby(day.date).transform("nunique") > 1).astype(int).to_numpy()
    return cal


def lead_features(base: pd.DataFrame, lead: int, publish_lag_min: int = DEFAULT_PUBLISH_LAG_MIN) -> tuple[pd.DataFrame, pd.Series]:
    """Feature matrix X and target y (= INDO - NDF vintage) for one lead. Rows are period starts."""
    ndf = base[f"ndf_L{lead}"]
    steps = known_steps(lead, publish_lag_min)
    h = lead // PERIOD_MIN                    # periods between decision time and q's start
    now_shift = h + 1                         # the period that ended exactly at decision time
    y = (base["indo"] - ndf).where(base["indo"] > 0)
    err = y

    X = calendar_features(base.index, base.attrs.get("holidays", []))
    X["lead_min"] = lead
    X["ndf"] = ndf
    longer = [x for x in _lead_cols(base, "ndf_L") if x > lead]
    X["ndf_revision"] = ndf - base[f"ndf_L{longer[0]}"] if longer else np.nan
    X["ndf_vs_yesterday"] = ndf - base["indo"].shift(PERIODS_PER_DAY)
    X["ndf_vs_last_week"] = ndf - base["indo"].shift(7 * PERIODS_PER_DAY)

    # Errors of earlier periods, made at the same lead, that were already published.
    for k in range(4):
        X[f"err_known_{k}"] = err.shift(steps + k)
    known = err.shift(steps)
    X["err_mean6"] = known.rolling(6, min_periods=3).mean()
    X["err_ewm"] = known.ewm(halflife=4.0, ignore_na=True).mean()
    days = pd.concat([err.shift(PERIODS_PER_DAY * d) for d in range(1, 8)], axis=1)
    X["err_same_sp_7d"] = days.mean(axis=1).where(days.notna().sum(axis=1) >= 3)

    # Embedded wind/solar: the forecast for q at this lead, how it was just revised, and how recent solar misses went.
    for src, name in (("ewind_L", "ewind_fc"), ("esolar_L", "esolar_fc")):
        if f"{src}{lead}" in base.columns:
            X[name] = base[f"{src}{lead}"]
            longer_e = [x for x in _lead_cols(base, src) if x > lead]
            X[f"{name}_revision"] = base[f"{src}{lead}"] - base[f"{src}{longer_e[0]}"] if longer_e else np.nan
    if "esolar_cap" in base.columns:
        X["esolar_load_factor"] = X.get("esolar_fc", np.nan) / base["esolar_cap"].replace(0, np.nan)
    if "ewind_cap" in base.columns:
        X["ewind_load_factor"] = X.get("ewind_fc", np.nan) / base["ewind_cap"].replace(0, np.nan)
    if "pv" in base.columns:
        X["pv_known"] = base["pv"].shift(steps)
        if f"esolar_L{lead}" in base.columns:
            miss = base["pv"] - base[f"esolar_L{lead}"]
            X["solar_miss_known"] = miss.shift(steps)
            X["solar_miss_mean3"] = miss.shift(steps).rolling(3, min_periods=2).mean()

    # Transmission wind: forecast for q, and how the wind actually running at decision time compares with its forecast.
    if f"wfor_L{lead}" in base.columns:
        X["wind_fc"] = base[f"wfor_L{lead}"]
        if "fuel_wind_mean" in base.columns:
            X["wind_miss_now"] = (base["fuel_wind_mean"] - base[f"wfor_L{lead}"]).shift(now_shift)

    # Real-time generation up to decision time versus the demand forecast for that same period: the live page's idea.
    if "fuel_mean" in base.columns:
        ndf0 = base["ndf_L0"] if "ndf_L0" in base.columns else ndf
        gap_now = base["fuel_mean"] - ndf0
        X["gen_vs_ndf_now"] = gap_now.shift(now_shift)
        X["gen_minus_indo_known"] = (base["fuel_mean"] - base["indo"]).shift(steps)
        X["gen_nowcast_err"] = X["gen_vs_ndf_now"] - X["gen_minus_indo_known"]
        X["gen_momentum"] = base["fuel_mean"].shift(now_shift) - base["fuel_mean"].shift(now_shift + 1)

    # Weather for q (Open-Meteo historical forecast), and how it has changed over the last three hours.
    for c in ("temp", "cloud", "rad", "wspd", "cloud_spread"):
        if c in base.columns:
            X[c] = base[c]
    if "temp" in base.columns:
        X["temp_change_3h"] = base["temp"] - base["temp"].shift(6)
        X["heating_degrees"] = (15.5 - base["temp"]).clip(lower=0)
    if "rad" in base.columns:
        X["rad_change_3h"] = base["rad"] - base["rad"].shift(6)
    return X, y


def build_dataset(base: pd.DataFrame, leads: Iterable[int] | None = None, publish_lag_min: int = DEFAULT_PUBLISH_LAG_MIN) -> pd.DataFrame:
    """All leads stacked: feature columns, `y`, and `ndf` (the published forecast the error is relative to)."""
    frames = []
    for lead in (leads if leads is not None else available_leads(base)):
        X, y = lead_features(base, lead, publish_lag_min)
        X["y"] = y
        X = X[X["y"].notna() & X["ndf"].notna()]
        frames.append(X)
    ds = pd.concat(frames) if frames else pd.DataFrame()
    ds.index.name = "target_start"
    return ds


FEATURE_EXCLUDE = ("y",)


def feature_columns(ds: pd.DataFrame) -> list[str]:
    return [c for c in ds.columns if c not in FEATURE_EXCLUDE]
