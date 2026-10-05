"""Chart data and headline figures for one trip, ported from the GridTrip
Alert app's per-unit chart: the plan (FPN), what the plant can actually
deliver (adjusted FPN, i.e. FPN limited by MEL/MIL) and its capability (MEL),
with the *shortfall* between plan and delivery.

Headline loss follows GridTrip's own definition -- the mean FPN minus the mean
adjusted FPN over the trip's settlement period, never below zero -- so the
numbers on this page match what that tool showed for the same data.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Mapping, Sequence

PERIOD = timedelta(minutes=30)


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def build_trip_chart(points: Sequence[Mapping], sp_start: datetime, now: datetime | None = None) -> dict:
    """`points`: rows with spot_time (datetime), fpn, mel, adjusted_fpn (any may be None).
    `sp_start`: UTC start of the settlement period the trip was detected in."""
    now = now or datetime.now(timezone.utc)
    series = []
    for p in points:
        fpn, adj, mel = _num(p.get("fpn")), _num(p.get("adjusted_fpn")), _num(p.get("mel"))
        t = p["spot_time"]
        series.append({
            "t": t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "fpn": fpn, "adj": adj, "mel": mel,
            "shortfall": round(max(0.0, fpn - adj), 1) if fpn is not None and adj is not None else None,
            "future": t > now,
        })

    in_sp = [p for p in points if sp_start <= p["spot_time"] < sp_start + PERIOD]
    fpns = [v for v in (_num(p.get("fpn")) for p in in_sp) if v is not None]
    adjs = [v for v in (_num(p.get("adjusted_fpn")) for p in in_sp) if v is not None]
    mean_fpn = sum(fpns) / len(fpns) if fpns else None
    mean_adj = sum(adjs) / len(adjs) if adjs else None
    loss = max(0.0, mean_fpn - mean_adj) if mean_fpn is not None and mean_adj is not None else None

    after = [s for s in series if s["t"] >= sp_start.strftime("%Y-%m-%dT%H:%M:%SZ")]
    shortfalls = [s["shortfall"] for s in after if s["shortfall"] is not None]
    mel_checked = [s for s in after if s["mel"] is not None and s["fpn"] is not None]
    mel_limited = [s for s in mel_checked if s["mel"] < s["fpn"] - 0.5]
    return {
        "points": series,
        "stats": {
            "mean_fpn": None if mean_fpn is None else round(mean_fpn, 1),
            "mean_adj": None if mean_adj is None else round(mean_adj, 1),
            "loss_mw": None if loss is None else round(loss, 1),
            "impact_pct": round(loss / mean_fpn * 100, 1) if loss is not None and mean_fpn else None,
            "peak_shortfall_mw": max(shortfalls) if shortfalls else None,
            # Share of the points since the trip where MEL sat below the plan: high means the plant
            # itself restricted its capability (a genuine outage); low means something else
            # (a Balancing Mechanism action, say) pulled output down.
            "mel_limited_share": round(len(mel_limited) / len(mel_checked), 2) if mel_checked else None,
        },
    }


# A plant that is unavailable but publishes no REMIT notice still shows it in its own MEL
# (Maximum Export Level): it cuts MEL below its plan. These are the thresholds for calling that
# a cut rather than noise.
MEL_CUT_MIN_MW = 10.0        # average gap between plan and MEL needed to call it a cut
MEL_CUT_MIN_SHARE = 0.5      # share of the recent minutes the gap must have been there
MEL_TOLERANCE_MW = 0.5
RECENT = timedelta(hours=1)


def mel_evidence(points: Sequence[Mapping], now: datetime | None = None) -> dict:
    """What the plant's own MEL says about a trip, independent of any REMIT notice.

    `points`: rows with spot_time (datetime), fpn, mel. Looks at the last hour of points up to
    `now` for how far MEL sits below the plan, walks back to find when that run of cut minutes
    began, and reads the published (future) MEL for when it returns to plan.

    Returns {verdict: "mel_cut" | "no_cut" | "no_data", unavailable_mw, since, back_at}.
    `back_at` is None if MEL never returns to plan inside the published window.
    """
    now = now or datetime.now(timezone.utc)
    rows = []
    for p in sorted(points, key=lambda r: r["spot_time"]):
        fpn, mel = _num(p.get("fpn")), _num(p.get("mel"))
        if fpn is not None and mel is not None:
            rows.append((p["spot_time"], fpn, mel))
    past = [r for r in rows if r[0] <= now]
    recent = [r for r in past if r[0] > now - RECENT]
    if not recent:
        return {"verdict": "no_data", "unavailable_mw": None, "since": None, "back_at": None}

    cut = [r for r in recent if r[1] - r[2] > MEL_TOLERANCE_MW]
    gap = sum(r[1] - r[2] for r in cut) / len(cut) if cut else 0.0
    share = len(cut) / len(recent)
    verdict = "mel_cut" if share >= MEL_CUT_MIN_SHARE and gap >= MEL_CUT_MIN_MW else "no_cut"

    since = None
    if verdict == "mel_cut":
        for t, fpn, mel in reversed(past):          # the unbroken run of cut minutes ending now
            if fpn - mel > MEL_TOLERANCE_MW:
                since = t
            else:
                break
    back_at = None
    if verdict == "mel_cut":
        back_at = next((t for t, fpn, mel in rows if t > now and fpn - mel <= MEL_TOLERANCE_MW), None)
    iso = lambda t: None if t is None else t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    return {"verdict": verdict, "unavailable_mw": round(gap, 1) if cut else 0.0, "since": iso(since), "back_at": iso(back_at)}
