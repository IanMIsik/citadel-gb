"""engine/mel_plan.py: a tripped plant's MEL "vintages" -- the MEL curve as
it stood after each notification (latest notification wins per minute)."""

from __future__ import annotations

import pandas as pd

from citadel.engine.mel_plan import mel_vintages


def _mel(bm_unit, time_from, time_to, level, notified):
    return {
        "bmUnit": bm_unit, "timeFrom": time_from, "timeTo": time_to,
        "levelFrom": level, "levelTo": level, "notificationTime": notified,
    }


def test_each_notification_after_the_trip_is_a_vintage_and_later_ones_supersede_earlier_minutes():
    df = pd.DataFrame([
        _mel("T_X", "2026-10-02T10:00:00Z", "2026-10-02T13:00:00Z", 500, "2026-10-02T09:00:00Z"),   # baseline: healthy
        _mel("T_X", "2026-10-02T10:30:00Z", "2026-10-02T12:00:00Z", 0, "2026-10-02T10:35:00Z"),     # trip: back by 12:00
        _mel("T_X", "2026-10-02T10:30:00Z", "2026-10-02T12:30:00Z", 0, "2026-10-02T11:10:00Z"),     # plan slips to 12:30
        _mel("T_OTHER", "2026-10-02T10:00:00Z", "2026-10-02T13:00:00Z", 100, "2026-10-02T09:00:00Z"),
    ])
    v = mel_vintages(df, "T_X", pd.Timestamp("2026-10-02T10:30:00Z"))
    assert [x["notification_time"] for x in v] == ["2026-10-02T09:00:00Z", "2026-10-02T10:35:00Z", "2026-10-02T11:10:00Z"]

    def mel_at(vintage, t):
        return next(p["mel"] for p in vintage["points"] if p["t"] == t)

    assert mel_at(v[0], "2026-10-02T11:00:00Z") == 500            # before the trip: healthy
    assert mel_at(v[1], "2026-10-02T11:00:00Z") == 0              # first plan: out...
    assert mel_at(v[1], "2026-10-02T12:15:00Z") == 500            # ...back at 12:00 (falls back to the older MEL)
    assert mel_at(v[2], "2026-10-02T12:15:00Z") == 0              # revised: still out at 12:15


def test_unknown_unit_and_empty_input_give_no_vintages():
    assert mel_vintages(pd.DataFrame(), "T_X", pd.Timestamp("2026-10-02T10:00:00Z")) == []
    df = pd.DataFrame([_mel("T_Y", "2026-10-02T10:00:00Z", "2026-10-02T11:00:00Z", 1, "2026-10-02T09:00:00Z")])
    assert mel_vintages(df, "T_X", pd.Timestamp("2026-10-02T10:00:00Z")) == []
