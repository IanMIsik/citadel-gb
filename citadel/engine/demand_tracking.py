"""Tracking the live demand-error predictions (dev only): what to log, how to score it, and the band it implies.

Every prediction the FPN page's predictor makes is written once to `demand_prediction_log` and scored when INDO is
published. Only predictions made no later than the end of the period they are for count as "live" (a prediction
computed after INDO is out is hindsight, not a forecast).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from citadel.engine import demand_model

# A prediction counts as live if it was made at most this long after the period began (so before INDO could exist).
LIVE_UP_TO_MINUTES_AFTER_START = 30
MIN_ROWS_FOR_LIVE_BAND = 300


def log_rows(records: list[dict], predicted_at, trained_through: str | None) -> list[tuple]:
    """Predictor records -> demand_prediction_log tuples."""
    predicted_at = pd.Timestamp(predicted_at)
    if predicted_at.tzinfo is None:
        predicted_at = predicted_at.tz_localize("UTC")
    rows = []
    for r in records:
        if r.get("pred_error") is None or r.get("ndf") is None:
            continue
        start = pd.Timestamp(r["start"])
        lead_at = (start - predicted_at).total_seconds() / 60.0
        rows.append((start.to_pydatetime(), int(r["lead_used_min"]), predicted_at.to_pydatetime(), lead_at, float(r["ndf"]),
                     r["pred_error"], r.get("raw_low"), r.get("raw_high"), r.get("pred_error_low"), r.get("pred_error_high"), trained_through))
    return rows


def scores_frame(records) -> pd.DataFrame:
    df = pd.DataFrame([dict(r) for r in records])
    if df.empty:
        return df
    df["target_start"] = pd.to_datetime(df["target_start"], utc=True)
    return df


def live_only(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["lead_at_prediction_min"] >= -LIVE_UP_TO_MINUTES_AFTER_START] if not df.empty else df


def tracking_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Per lead: how many live predictions were scored, the model's mean absolute error against the published NDF's
    (= |actual error|), the improvement, and how often the shown band held the outcome."""
    df = live_only(df)
    if df.empty:
        return pd.DataFrame(columns=["lead_min", "n", "ndf_mae", "model_mae", "mae_vs_ndf_pct", "model_bias", "coverage", "band_width_mw"])
    rows = []
    for lead, g in df.groupby("lead_min"):
        resid = g["actual_error"] - g["pred_error"]
        ndf_mae = g["actual_error"].abs().mean()
        rows.append({
            "lead_min": int(lead), "n": len(g), "ndf_mae": ndf_mae, "model_mae": resid.abs().mean(),
            "mae_vs_ndf_pct": 100.0 * (resid.abs().mean() / ndf_mae - 1.0) if ndf_mae else np.nan,
            "model_bias": resid.mean(),
            "coverage": float(((g["actual_error"] >= g["pred_low"]) & (g["actual_error"] <= g["pred_high"])).mean()),
            "band_width_mw": float((g["pred_high"] - g["pred_low"]).mean()),
        })
    return pd.DataFrame(rows).sort_values("lead_min").reset_index(drop=True)


def logged_offsets(df: pd.DataFrame, alpha: float = demand_model.DEFAULT_ALPHA, min_rows: int = MIN_ROWS_FOR_LIVE_BAND) -> dict[int, float]:
    """Band widening per lead learned from the live log itself: how far outcomes fell outside the *raw* model band.
    Only leads with at least `min_rows` live, scored predictions get one; the others keep the offset saved with the model."""
    df = live_only(df)
    if df.empty:
        return {}
    cal = df.rename(columns={"actual_error": "y", "raw_low": "q10", "raw_high": "q90"}).dropna(subset=["y", "q10", "q90"])
    counts = cal.groupby("lead_min").size()
    keep = counts[counts >= min_rows].index
    if not len(keep):
        return {}
    return {int(k): float(v) for k, v in demand_model.static_offsets(cal[cal["lead_min"].isin(keep)], alpha).items()}
