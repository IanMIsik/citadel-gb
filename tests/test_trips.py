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


def test_trip_chart_loss_follows_gridtrip_definition_and_flags_mel_restriction():
    from datetime import datetime, timedelta, timezone

    from citadel.engine.trip_chart import build_trip_chart

    sp = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    # Trip SP: plan 200 MW all period, plant only delivers 120 for half of it (MEL cut to 120).
    pts = [{"spot_time": sp + timedelta(minutes=5 * i), "fpn": 200.0, "mel": 200.0 if i < 3 else 120.0,
            "adjusted_fpn": 200.0 if i < 3 else 120.0} for i in range(6)]
    pts.append({"spot_time": sp - timedelta(minutes=30), "fpn": 200.0, "mel": 200.0, "adjusted_fpn": 200.0})  # before the trip
    out = build_trip_chart(pts, sp, now=sp + timedelta(minutes=20))
    s = out["stats"]
    assert s["mean_fpn"] == 200.0 and s["mean_adj"] == 160.0
    assert s["loss_mw"] == 40.0 and s["impact_pct"] == 20.0 and s["peak_shortfall_mw"] == 80.0
    assert s["mel_limited_share"] == 0.5
    assert out["points"][-1]["future"] is False and out["points"][5]["future"] is True  # after 'now' = published plan


def test_trip_chart_survives_missing_values():
    from datetime import datetime, timezone

    from citadel.engine.trip_chart import build_trip_chart

    sp = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    out = build_trip_chart([{"spot_time": sp, "fpn": 100.0, "mel": None, "adjusted_fpn": float("nan")}], sp, now=sp)
    assert out["stats"]["loss_mw"] is None and out["points"][0]["shortfall"] is None
    assert build_trip_chart([], sp)["stats"]["loss_mw"] is None


def test_stack_runner_notifies_the_fpn_runner_so_the_decision_table_follows_the_stack():
    import asyncio

    from citadel.engine.fpn_runner import FpnRunner
    from citadel.engine.runner import Runner

    fpn = FpnRunner.__new__(FpnRunner)
    fpn._dirty = asyncio.Event()
    stack = Runner.__new__(Runner)
    stack._stack_persisted_listeners = []
    stack.add_stack_persisted_listener(fpn.on_stack_persisted)

    assert not fpn._dirty.is_set()
    for callback in stack._stack_persisted_listeners:
        callback()
    assert fpn._dirty.is_set()


def test_finite_helper_turns_nan_and_infinity_into_gaps():
    from citadel.api.app import _finite

    assert _finite(float("nan")) is None and _finite(float("inf")) is None and _finite(float("-inf")) is None
    assert _finite(12.5) == 12.5 and _finite(None) is None and _finite(0.0) == 0.0


def test_mel_evidence_flags_an_unpublished_outage_and_reads_the_return_from_the_published_mel():
    from datetime import datetime, timedelta, timezone

    from citadel.engine.trip_chart import mel_evidence

    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)

    def pts(cut_from, back_at):
        out = []
        for i in range(-24, 13):                       # two hours back, one hour of published plan forward
            t = now + timedelta(minutes=5 * i)
            cut = t >= cut_from and t < back_at
            out.append({"spot_time": t, "fpn": 300.0, "mel": 0.0 if cut else 300.0})
        return out

    ev = mel_evidence(pts(now - timedelta(minutes=50), now + timedelta(minutes=30)), now)
    assert ev["verdict"] == "mel_cut" and ev["unavailable_mw"] == 300.0
    assert ev["since"] == "2026-10-05T11:10:00Z" and ev["back_at"] == "2026-10-05T12:30:00Z"

    never = mel_evidence(pts(now - timedelta(minutes=50), now + timedelta(days=1)), now)
    assert never["verdict"] == "mel_cut" and never["back_at"] is None      # MEL never returns inside the window

    assert mel_evidence(pts(now + timedelta(days=1), now + timedelta(days=2)), now)["verdict"] == "no_cut"
    assert mel_evidence([], now)["verdict"] == "no_data"
    # a small dip below plan is noise, not an outage
    small = [{"spot_time": now - timedelta(minutes=5 * i), "fpn": 300.0, "mel": 295.0} for i in range(6)]
    assert mel_evidence(small, now)["verdict"] == "no_cut"


# The real shapes seen for T_HEYM11 on 2026-10-05: a full 610 MW outage that began on 25 Aug (revision 116, published
# today) running alongside several long-lived 112 MW partial derates.
_FULL_OUTAGE = {"mrid": "48X000000000022A-ELXP-RMT-00148107", "revisionNumber": 116, "eventStatus": "Active",
                "eventStartTime": "2026-08-25T23:00:00Z", "eventEndTime": "2026-10-21T07:00:00Z",
                "normalCapacity": 610.0, "availableCapacity": 0.0, "unavailableCapacity": 610.0, "publishTime": "2026-10-05T10:43:09Z"}
_PARTIAL_DERATE = {"mrid": "48X000000000022A-ELXP-RMT-00148227", "revisionNumber": 3, "eventStatus": "Active",
                   "eventStartTime": "2026-10-02T22:00:00Z", "eventEndTime": "2027-01-07T08:30:00Z",
                   "normalCapacity": 610.0, "availableCapacity": 498.0, "unavailableCapacity": 112.0, "publishTime": "2026-09-21T13:49:58Z"}


def test_remit_match_finds_a_long_running_notice_that_started_long_before_detection():
    detected_at = datetime(2026, 10, 5, 9, 39, tzinfo=timezone.utc)          # we only saw the plant today
    # With no information about the plant it still refuses (a stale notice is worse than none)...
    assert best_remit_match([_FULL_OUTAGE, _PARTIAL_DERATE], detected_at) is None
    # ...but with the plant's own MEL (0 MW: nothing available) the full outage is the one that fits, not the 112 MW derate.
    assert best_remit_match([_PARTIAL_DERATE, _FULL_OUTAGE], detected_at, mel_now=0.0) == _FULL_OUTAGE["mrid"]
    # a plant still exporting most of its capacity is explained by the partial derate, not the full outage
    assert best_remit_match([_FULL_OUTAGE, _PARTIAL_DERATE], detected_at, mel_now=498.0) == _PARTIAL_DERATE["mrid"]


def test_remit_match_falls_back_to_the_trip_size_and_rejects_notices_that_do_not_cover_it():
    detected_at = datetime(2026, 10, 5, 9, 39, tzinfo=timezone.utc)
    assert best_remit_match([_FULL_OUTAGE, _PARTIAL_DERATE], detected_at, drop_mw=600.0) == _FULL_OUTAGE["mrid"]
    ended = {**_FULL_OUTAGE, "eventEndTime": "2026-10-01T00:00:00Z"}            # over before we detected anything
    not_started = {**_FULL_OUTAGE, "eventStartTime": "2026-10-06T00:00:00Z"}    # starts tomorrow
    inactive = {**_FULL_OUTAGE, "eventStatus": "Dismissed"}
    assert best_remit_match([ended, not_started, inactive], detected_at, mel_now=0.0) is None
    assert best_remit_match([_PARTIAL_DERATE], detected_at, mel_now=0.0) is None     # 498 MW still available: does not explain MEL 0


def test_remit_match_still_prefers_a_notice_that_starts_at_detection():
    detected_at = datetime(2026, 10, 5, 9, 39, tzinfo=timezone.utc)
    fresh = {**_PARTIAL_DERATE, "mrid": "FRESH", "eventStartTime": "2026-10-05T09:00:00Z"}
    assert best_remit_match([_FULL_OUTAGE, fresh], detected_at, mel_now=0.0) == "FRESH"


def test_trip_chart_handles_a_pumping_plant_held_back_by_its_mil():
    from datetime import datetime, timedelta, timezone

    from citadel.engine.trip_chart import build_trip_chart

    sp = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
    # Pumped storage planning to import 300 MW; its MIL (max import) is cut to -100, so it can only take 100.
    pts = [{"spot_time": sp + timedelta(minutes=5 * i), "fpn": -300.0, "mel": 300.0, "mil": -100.0,
            "adjusted_fpn": -100.0} for i in range(6)]
    out = build_trip_chart(pts, sp, now=sp + timedelta(minutes=30))
    s = out["stats"]
    assert s["mean_fpn"] == -300.0 and s["mean_adj"] == -100.0
    assert s["loss_mw"] == 200.0 and s["impact_pct"] == 66.7 and s["peak_shortfall_mw"] == 200.0
    assert s["side"] == "import" and s["limit"] == "mil" and s["mel_limited_share"] == 1.0
    assert out["points"][0]["mil"] == -100.0 and out["points"][0]["shortfall"] == 200.0


def test_mil_evidence_for_an_importing_plant_and_mel_for_an_exporting_one():
    from datetime import datetime, timedelta, timezone

    from citadel.engine.trip_chart import mel_evidence

    now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
    pts = []
    for i in range(-24, 13):
        t = now + timedelta(minutes=5 * i)
        cut = t >= now - timedelta(minutes=50) and t < now + timedelta(minutes=30)
        pts.append({"spot_time": t, "fpn": -250.0, "mel": 250.0, "mil": 0.0 if cut else -250.0})
    ev = mel_evidence(pts, now)
    assert ev["verdict"] == "mel_cut" and ev["limit"] == "mil" and ev["unavailable_mw"] == 250.0
    assert ev["since"] == "2026-10-05T11:10:00Z" and ev["back_at"] == "2026-10-05T12:30:00Z"
    # an importing plant whose MEL happens to be zero is NOT cut: MEL is not its limit
    only_mel_zero = [{"spot_time": p["spot_time"], "fpn": -250.0, "mel": 0.0, "mil": -250.0} for p in pts]
    assert mel_evidence(only_mel_zero, now)["verdict"] == "no_cut"
    # rows stored before MIL was recorded carry no import limit, so there is nothing to judge
    assert mel_evidence([{**p, "mil": None} for p in pts], now)["verdict"] == "no_data"
    exporting = [{"spot_time": p["spot_time"], "fpn": 300.0, "mel": 0.0, "mil": None} for p in pts]
    assert mel_evidence(exporting, now)["limit"] == "mel"


def test_worst_behaviour_series_carries_mil_and_drops_unpublished_values():
    import numpy as np

    rows = [_row("T_P", "2026-09-30T10:00:00Z", -300, -100), _row("T_P", "2026-09-30T10:05:00Z", -300, -100)]
    for r, mil in zip(rows, (-100.0, -np.inf)):
        r["mil_spot_vol"] = mil
    state = {"T_P": {"drop": 200.0, "peak": 200.0, "partial": False, "recovered_at": None}}
    out = worst_behaviour_series(pd.DataFrame(rows), state, [(date(2026, 9, 30), 20)], NOW)
    assert out["mil"].iloc[0] == -100.0 and pd.isna(out["mil"].iloc[1])
