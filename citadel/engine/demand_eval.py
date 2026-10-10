"""Baseline evaluation for the demand forecaster (dev only; reads the `demand_*` history tables).

The question: how wrong is the published National Demand Forecast (NDF) compared with the first-published
outturn (INDO), and how much of that can simple corrections remove? Anything a model does later has to
beat these numbers.

Terms
-----
error(q, L)   = INDO(q) - NDF(q, L), where NDF(q, L) is the forecast for period q that was the latest one
                published at least L minutes before q started (`demand_forecast_vintages`). Positive means
                demand came in above the forecast.
correction    = a number of MW added to NDF(q, L) using only what was known L minutes before q started. A
                period p counts as known once it has ended and its INDO has had `publish_lag_min` to arrive.
residual      = error - correction. A method is good if its residuals are smaller than the raw errors.

Published NDF/INDO already include embedded wind and solar, so nothing here adjusts for embedded generation.

`live_replica` separately reproduces the per-minute `misco_indo` / `misco_ndf` / `misco_indo_vs_ndf` the FPN
page computes, so the same maths can be run over history and checked against the stored live values.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import datetime

import numpy as np
import pandas as pd

PERIOD_MIN = 30
DEFAULT_LEADS = (0, 30, 60, 120, 240, 480, 720, 1080)
DEFAULT_PUBLISH_LAG_MIN = 15
METHODS = ("ndf_as_published", "persist_last", "mean_last_6", "ewm_hl4", "same_sp_28d")


# ---------------------------------------------------------------------------------------------
# Building the error frame
# ---------------------------------------------------------------------------------------------

def build_error_frame(vintages: pd.DataFrame, outturn: pd.DataFrame) -> pd.DataFrame:
    """Wide, gap-free half-hourly frame indexed by UTC period start.

    vintages: columns target_start, lead_min, value (the NDF vintages).
    outturn:  columns target_start, indo.
    Result columns: `indo`, and `ndf_L<lead>` per lead present. Missing periods are NaN rows, so that
    shifting by N rows is always shifting by N half-hours."""
    v = vintages.copy()
    v["target_start"] = pd.to_datetime(v["target_start"], utc=True)
    wide = v.pivot_table(index="target_start", columns="lead_min", values="value", aggfunc="last")
    wide.columns = [f"ndf_L{int(c)}" for c in wide.columns]
    o = outturn.copy()
    o["target_start"] = pd.to_datetime(o["target_start"], utc=True)
    frame = wide.join(o.set_index("target_start")[["indo"]], how="outer").sort_index()
    if frame.empty:
        return frame
    full = pd.date_range(frame.index.min(), frame.index.max(), freq=f"{PERIOD_MIN}min", tz="UTC")
    frame = frame.reindex(full)
    frame.index.name = "target_start"
    return frame


def leads_in(frame: pd.DataFrame) -> list[int]:
    return sorted(int(c.removeprefix("ndf_L")) for c in frame.columns if c.startswith("ndf_L"))


def known_steps(lead_min: int, publish_lag_min: int = DEFAULT_PUBLISH_LAG_MIN) -> int:
    """How many half-hour rows back the newest *known* period is, for a forecast made `lead_min` ahead.

    Period p (start s_p) is known at time T = s_q - L when s_p + 30 + lag <= T, so q - p >= (L + 30 + lag) / 30."""
    return math.ceil((lead_min + PERIOD_MIN + publish_lag_min) / PERIOD_MIN)


# ---------------------------------------------------------------------------------------------
# Baseline corrections (each returns MW to add to the published NDF)
# ---------------------------------------------------------------------------------------------

def corrections(error: pd.Series, steps: int, k: int = 6, halflife: float = 4.0, weeks: int = 28) -> dict[str, pd.Series]:
    """`error` is this lead's own error series on the full half-hourly grid; `steps` is known_steps().

    Everything is built from `error.shift(steps)` or older, so no correction ever sees a period that had
    not been published at decision time."""
    known = error.shift(steps)
    out = {
        "ndf_as_published": pd.Series(0.0, index=error.index),
        "persist_last": known,
        "mean_last_6": known.rolling(k, min_periods=max(2, k // 2)).mean(),
        "ewm_hl4": known.ewm(halflife=halflife, ignore_na=True).mean(),
    }
    # The same half-hour on previous days (UTC grid: 48 rows = one day). `steps` < 48 for every lead we use,
    # so even yesterday's same period was long known.
    per_day = [error.shift(48 * d) for d in range(1, weeks + 1)]
    stacked = pd.concat(per_day, axis=1)
    out["same_sp_28d"] = stacked.mean(axis=1).where(stacked.notna().sum(axis=1) >= 7)
    return out


def evaluation_table(frame: pd.DataFrame, leads: Iterable[int] | None = None,
                     publish_lag_min: int = DEFAULT_PUBLISH_LAG_MIN) -> pd.DataFrame:
    """Long frame: one row per (period, lead) with indo, ndf, error and every method's correction/residual."""
    parts = []
    for lead in (leads if leads is not None else leads_in(frame)):
        col = f"ndf_L{lead}"
        if col not in frame.columns:
            continue
        err = (frame["indo"] - frame[col]).where(frame["indo"] > 0)
        steps = known_steps(lead, publish_lag_min)
        part = pd.DataFrame({"lead_min": lead, "indo": frame["indo"], "ndf": frame[col], "error": err})
        for name, corr in corrections(err, steps).items():
            part[f"corr_{name}"] = corr
            part[f"resid_{name}"] = err - corr
        parts.append(part)
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts).reset_index()


def score(table: pd.DataFrame, group: list[str] | None = None) -> pd.DataFrame:
    """MAE / RMSE / bias of each method's residual, on the rows where every method can be scored (so the
    methods are compared on the same periods)."""
    if table.empty:
        return pd.DataFrame()
    resid_cols = [f"resid_{m}" for m in METHODS]
    base = table.dropna(subset=["error", *resid_cols])
    keys = group or ["lead_min"]
    rows = []
    for key, g in base.groupby(keys):
        key = key if isinstance(key, tuple) else (key,)
        for m in METHODS:
            r = g[f"resid_{m}"]
            rows.append({**dict(zip(keys, key)), "method": m, "n": len(r), "mae": r.abs().mean(),
                         "rmse": float(np.sqrt((r ** 2).mean())), "bias": r.mean()})
    out = pd.DataFrame(rows)
    ref = out[out["method"] == "ndf_as_published"].set_index(keys)["mae"]
    out["mae_vs_ndf_pct"] = _pct_vs_ref(out, ref, keys)
    return out


def _pct_vs_ref(out: pd.DataFrame, ref: pd.Series, keys: list[str]) -> list[float]:
    result = []
    for row in out.to_dict("records"):
        idx = tuple(row[k] for k in keys) if len(keys) > 1 else row[keys[0]]
        base = ref.loc[idx]
        result.append(100.0 * (row["mae"] / base - 1.0) if base else float("nan"))
    return result


# ---------------------------------------------------------------------------------------------
# Replica of the live per-minute demand-error maths (engine/fpn.py build_aggregated)
# ---------------------------------------------------------------------------------------------

def live_replica(fuelinst: pd.DataFrame, ndf: pd.DataFrame, indo: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    """Per-minute `misco_indo`, `misco_ndf`, `misco_indo_vs_ndf`, reusing FpnRunner's own helpers.

    fuelinst: start_time, total_mw (5-minute blocks).
    ndf:      settlementDate, settlementPeriod, latest_ndf (one value per period).
    indo:     settlementDate, settlementPeriod, indo.
    Mirrors build_aggregated: misco_indo is a 5-row rolling mean of the FUELINST total; the correction added
    to NDF is max(30-row rolling mean of misco_indo - published INDO, 300 MW), forward-filled."""
    from citadel.engine import fpn

    spots, mids = fpn.build_spot_time_grids(start, end)
    spot_ndf = fpn.sp_time_interpolator(ndf, "latest_ndf", spots, mids)
    spot_indo = fpn.sp_time_interpolator(indo, "indo", spots, mids)
    grid = spots[["settlementDate", "settlementPeriod", "spot_time"]].merge(spot_ndf, on="spot_time", how="left") \
        .merge(spot_indo, on="spot_time", how="left").sort_values("spot_time").reset_index(drop=True)

    block = (grid["spot_time"] - pd.Timedelta(seconds=1.51 * 60)).dt.round("5min")
    fuel = fuelinst.copy()
    fuel["start_time"] = pd.to_datetime(fuel["start_time"], utc=True)
    block_tz = block if block.dt.tz is not None else block.dt.tz_localize("UTC")
    grid["fuelinst_generation"] = block_tz.map(fuel.set_index("start_time")["total_mw"])
    grid["misco_indo"] = grid["fuelinst_generation"].rolling(5).mean()
    grid["unexplained_dmd"] = grid["misco_indo"] - grid["spot_indo"]
    grid["unexplained_dmd_MA"] = grid["unexplained_dmd"].rolling(window=30, min_periods=5).mean().ffill()
    grid["misco_ndf"] = grid["spot_latest_ndf"] + np.maximum(grid["unexplained_dmd_MA"], 300)
    grid["misco_indo_vs_ndf"] = grid["misco_indo"] - grid["misco_ndf"]
    return grid


def compare_with_live(replica: pd.DataFrame, live: pd.DataFrame, columns: tuple[str, ...] = ("misco_indo", "misco_ndf", "misco_indo_vs_ndf")) -> pd.DataFrame:
    """Per column: rows compared, mean absolute difference, correlation, and the share of rows within 50 MW.
    `live` has spot_time plus the same column names (from fpn_aggregated)."""
    a = replica.copy()
    a["spot_time"] = pd.to_datetime(a["spot_time"], utc=True)
    b = live.copy()
    b["spot_time"] = pd.to_datetime(b["spot_time"], utc=True)
    m = a.merge(b, on="spot_time", suffixes=("_rep", "_live"))
    rows = []
    for c in columns:
        pair = m[[f"{c}_rep", f"{c}_live"]].dropna()
        if pair.empty:
            rows.append({"column": c, "n": 0})
            continue
        diff = (pair[f"{c}_rep"] - pair[f"{c}_live"]).abs()
        rows.append({"column": c, "n": len(pair), "mean_abs_diff_mw": diff.mean(),
                     "corr": pair[f"{c}_rep"].corr(pair[f"{c}_live"]), "within_50mw_pct": 100.0 * (diff <= 50).mean()})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------------------------
# Loading from the database
# ---------------------------------------------------------------------------------------------

async def load_frame(pool, start: datetime | None = None, end: datetime | None = None) -> pd.DataFrame:
    vint = await pool.fetch(
        "SELECT target_start, lead_min, value FROM demand_forecast_vintages WHERE dataset = 'NDF' "
        "AND ($1::timestamptz IS NULL OR target_start >= $1) AND ($2::timestamptz IS NULL OR target_start < $2)", start, end)
    out = await pool.fetch(
        "SELECT target_start, indo FROM demand_outturn "
        "WHERE ($1::timestamptz IS NULL OR target_start >= $1) AND ($2::timestamptz IS NULL OR target_start < $2)", start, end)
    return build_error_frame(pd.DataFrame([dict(r) for r in vint], columns=["target_start", "lead_min", "value"]),
                             pd.DataFrame([dict(r) for r in out], columns=["target_start", "indo"]))
