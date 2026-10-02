"""Unit tests for the plant trip detector: engine/fpn.py's detect_trips()
edge-triggering/hysteresis, and engine/fpn_runner.py's best_remit_match()
REMIT-matching filter -- the latter pinned against real REMIT data captured
live during this feature's own development session (message family
mrid=48X000000000063X-NGET-RMT-00031271, a genuine 6-revision outage whose
eventEndTime slipped from same-day 21:30 to next-day 21:30 before resolving
back in at 09:00 -- see the implementation plan for the full trace).
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd

from citadel.engine.fpn import TRIP_THRESHOLD_MW, detect_trips, worst_behaviour_series
from citadel.engine.fpn_runner import best_remit_match


NOW = datetime(2026, 9, 30, 10, 5, tzinfo=timezone.utc)


def _row(bm_unit: str, spot_time: str, fpn: float, adjusted: float, ft: str = "CCGT", sp: int = 20) -> dict:
    return {
        "bmUnit": bm_unit, "settlementDate": pd.Timestamp("2026-09-30"), "settlementPeriod": sp,
        "spot_time": pd.Timestamp(spot_time), "fpn_spot_vol": fpn, "adjusted_fpn": adjusted,
        "mel_spot_vol": adjusted, "FT": ft,
    }


def _step(rows, state, **kw):
    return detect_trips(pd.DataFrame(rows), state, now=NOW, **kw)


def test_trip_fires_once_and_stays_silent_for_its_whole_duration():
    state = {}
    trips, recs, state = _step([_row("T_X", "2026-09-30T10:00:00Z", 100, 100)], state)
    assert trips == [] and recs == []
    trips, recs, state = _step([_row("T_X", "2026-09-30T10:01:00Z", 100, 20)], state)
    assert len(trips) == 1 and trips[0].drop_mw == 80 and trips[0].fuel_type == "CCGT"
    # Still tripped on later cycles, even as the unit's data moves into later periods -- no new alert.
    for minute, sp in [("10:02", 20), ("10:03", 21), ("10:04", 22)]:
        trips, recs, state = _step([_row("T_X", f"2026-09-30T{minute}:00Z", 100, 20, sp=sp)], state)
        assert trips == [] and recs == []


def test_reading_is_taken_at_now_not_at_the_far_end_of_the_forecast_window():
    # The trip is announced for the *future* (MEL dips in SP22, 30 min ahead) while right now the unit is fine.
    rows = [_row("T_X", "2026-09-30T10:00:00Z", 100, 100), _row("T_X", "2026-09-30T10:35:00Z", 100, 0, sp=22)]
    trips, recs, state = _step(rows, {})
    assert trips == []


def test_partial_then_full_recovery_and_rearm():
    state = {}
    _, _, state = _step([_row("T_X", "2026-09-30T10:00:00Z", 100, 100)], state)
    trips, _, state = _step([_row("T_X", "2026-09-30T10:01:00Z", 100, 0)], state)    # drop 100
    assert len(trips) == 1
    _, recs, state = _step([_row("T_X", "2026-09-30T10:02:00Z", 100, 60)], state)    # drop 40 (<=50% of peak)
    assert [r.kind for r in recs] == ["partial"] and recs[0].peak_mw == 100 and recs[0].drop_mw == 40
    _, recs, state = _step([_row("T_X", "2026-09-30T10:03:00Z", 100, 70)], state)    # drop 30: no repeat partial
    assert recs == []
    _, recs, state = _step([_row("T_X", "2026-09-30T10:04:00Z", 100, 100)], state)   # drop 0 -> full
    assert [r.kind for r in recs] == ["full"] and state["T_X"]["peak"] is None
    trips, _, state = _step([_row("T_X", "2026-09-30T10:05:00Z", 100, 0)], state)    # a genuinely new trip fires again
    assert len(trips) == 1


def test_ignores_drops_below_threshold():
    trips, recs, _ = _step([_row("T_X", "2026-09-30T10:00:00Z", 100, 100 - (TRIP_THRESHOLD_MW - 1))], {})
    assert trips == [] and recs == []


def test_seed_only_records_already_tripped_units_without_alerting():
    trips, recs, state = _step([_row("T_X", "2026-09-30T10:00:00Z", 100, 0)], {}, seed_only=True)
    assert trips == [] and recs == [] and state["T_X"]["peak"] == 100
    trips, recs, state = _step([_row("T_X", "2026-09-30T10:01:00Z", 100, 0)], state)   # still tripped: stays quiet
    assert trips == [] and recs == []


def test_empty_frame_returns_state_unchanged():
    state = {"T_X": {"drop": 30.0, "peak": None, "partial": False, "recovered_at": None}}
    trips, recs, out = detect_trips(pd.DataFrame(), state)
    assert trips == [] and recs == [] and out == state


def test_worst_behaviour_series_covers_tripped_units_only():
    rows = [_row("T_X", "2026-09-30T10:00:00Z", 100, 0), _row("T_X", "2026-09-30T10:05:00Z", 100, 40),
            _row("T_OK", "2026-09-30T10:00:00Z", 100, 100)]
    state = {"T_X": {"drop": 100.0, "peak": 100.0, "partial": False, "recovered_at": None},
             "T_OK": {"drop": 0.0, "peak": None, "partial": False, "recovered_at": None}}
    out = worst_behaviour_series(pd.DataFrame(rows), state, [(date(2026, 9, 30), 20)], NOW)
    assert set(out["bm_unit"]) == {"T_X"} and list(out["vol"]) == [0.0, 40.0] and set(out["status"]) == {"tripped"}


# --- REMIT matching, pinned against real data from this session --------

# Real revisions of mrid=48X000000000063X-NGET-RMT-00031271 (T_LYNE-adjacent
# NGET outage), captured live: rev1 eventStartTime 2026-09-29T21:00:00Z.
_REAL_CANDIDATE = {
    "mrid": "48X000000000063X-NGET-RMT-00031271",
    "eventStartTime": "2026-09-29T21:00:00Z",
    "eventEndTime": "2026-09-29T21:30:00Z",
}
# A genuinely different, unrelated outage for the same asset a few days
# earlier -- must never be picked over a same-day trip.
_UNRELATED_CANDIDATE = {
    "mrid": "48X000000000063X-NGET-RMT-OLDONE",
    "eventStartTime": "2026-09-24T09:00:00Z",
    "eventEndTime": "2026-09-24T10:00:00Z",
}


def test_best_remit_match_picks_the_event_within_window():
    detected_at = datetime(2026, 9, 29, 20, 55, tzinfo=timezone.utc)  # 5 min before real eventStartTime
    mrid = best_remit_match([_REAL_CANDIDATE, _UNRELATED_CANDIDATE], detected_at, window_hours=6)
    assert mrid == "48X000000000063X-NGET-RMT-00031271"


def test_best_remit_match_returns_none_outside_window():
    detected_at = datetime(2026, 9, 20, 0, 0, tzinfo=timezone.utc)  # far from both candidates
    mrid = best_remit_match([_REAL_CANDIDATE, _UNRELATED_CANDIDATE], detected_at, window_hours=6)
    assert mrid is None


def test_best_remit_match_ignores_candidates_with_no_event_start_time():
    detected_at = datetime(2026, 9, 29, 20, 55, tzinfo=timezone.utc)
    malformed = {"mrid": "no-start-time"}
    mrid = best_remit_match([malformed, _REAL_CANDIDATE], detected_at, window_hours=6)
    assert mrid == "48X000000000063X-NGET-RMT-00031271"


def test_fpn_runner_keeps_every_method_the_recompute_loop_calls():
    # A botched edit once deleted _persist, silently breaking every FPN
    # recompute -- cheap to guard against.
    from citadel.engine.fpn_runner import FpnRunner
    for name in ("_recompute_and_persist", "_persist", "_handle_trips", "_remit_poll_loop", "_poll_trip_remit"):
        assert callable(getattr(FpnRunner, name, None)), name
