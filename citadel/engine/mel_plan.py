"""A tripped plant's "return plan": how its published MEL (Maximum Export
Limit) profile has changed with each MEL notification it has submitted.

Elexon's MELS dataset is a log of notifications -- each row is a segment
(timeFrom..timeTo, levelFrom..levelTo) published at `notificationTime`.
At any minute the *effective* MEL is the segment from the most recent
notification covering it (the same latest-notification-wins rule
engine/fpn.py applies). A "vintage" is the whole MEL curve as it stood just
after a given notification: successive vintages show the plant pushing its
planned return later (or pulling it forward), exactly what the user wants
from the worst-behaviour graphs.

Limited to what the live MEL buffer holds (MELS windowed around now, ~+/-2h).
"""

from __future__ import annotations

import pandas as pd

from .stack import vectorized_exploder

MAX_VINTAGES = 8


def mel_vintages(
    mel_df: pd.DataFrame, bm_unit: str, since: pd.Timestamp, step_minutes: int = 5, max_vintages: int = MAX_VINTAGES,
) -> list[dict]:
    """Up to `max_vintages` MEL curves for `bm_unit`, oldest first: the one
    in force just before `since` (the baseline before the trip) plus the
    most recent notifications after it. Each: {"notification_time": iso,
    "points": [{"t": iso, "mel": MW}]} sampled every `step_minutes`.
    """
    if mel_df is None or mel_df.empty:
        return []
    df = mel_df[mel_df["bmUnit"] == bm_unit].copy()
    if df.empty:
        return []
    df["notificationTime"] = pd.to_datetime(df["notificationTime"], utc=True)
    exploded = vectorized_exploder(df)
    if exploded.empty:
        return []
    exploded = exploded[exploded["spot_time"].dt.minute % step_minutes == 0]
    since = pd.Timestamp(since)
    if since.tzinfo is None:
        since = since.tz_localize("UTC")

    notified = sorted(exploded["notificationTime"].unique())
    before = [n for n in notified if n < since]
    after = [n for n in notified if n >= since]
    chosen = (before[-1:] + after)[-max_vintages:]

    horizon_from = since - pd.Timedelta(minutes=30)
    vintages = []
    for n in chosen:
        sub = exploded[exploded["notificationTime"] <= n]
        latest = sub.loc[sub.groupby("spot_time")["notificationTime"].idxmax()]
        latest = latest[latest["spot_time"] >= horizon_from].sort_values("spot_time")
        vintages.append({
            "notification_time": pd.Timestamp(n).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "points": [
                {"t": t.strftime("%Y-%m-%dT%H:%M:%SZ"), "mel": round(float(v), 1)}
                for t, v in zip(latest["spot_time"], latest["spot_level"])
            ],
        })
    return vintages
