"""Chart data and headline figures for one trip, ported from the GridTrip
Alert app's per-unit chart: the plan (FPN), what the plant can actually
deliver (adjusted FPN, i.e. FPN limited by MEL/MIL) and its capability (MEL),
with the *shortfall* between plan and delivery.

Headline loss follows GridTrip's own definition -- the mean FPN minus the mean
adjusted FPN over the trip's settlement period -- so the numbers on this page match
what that tool showed for the same data. It is taken as a size (an absolute gap), because
a plant that is importing (pumped storage pumping, FPN below zero) is held back by its
MIL (Maximum Import Level) rather than its MEL: its adjusted FPN then sits *above* the
plan (less negative), and "FPN minus adjusted" would be negative.
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


MEL_TOLERANCE_MW = 0.5


def limit_gap(fpn, mel, mil) -> tuple[float | None, str | None]:
    """How far the plant's own limit sits inside its plan, in MW, and which limit that is.

    Exporting (FPN above zero): the limit is MEL, so the gap is FPN - MEL. Importing (FPN below
    zero, e.g. pumped storage pumping): the limit is MIL, so the gap is MIL - FPN (MIL is zero
    or negative; a MIL above the plan means it cannot import as much as it planned to). A
    plant planning zero has nothing to be held back from. (None, None) when the relevant limit
    is not known."""
    if fpn is None:
        return None, None
    if fpn > 0:
        return (None, None) if mel is None else (max(0.0, fpn - mel), "mel")
    if fpn < 0:
        return (None, None) if mil is None else (max(0.0, mil - fpn), "mil")
    return 0.0, None


def build_trip_chart(points: Sequence[Mapping], sp_start: datetime, now: datetime | None = None) -> dict:
    """`points`: rows with spot_time (datetime), fpn, mel, mil, adjusted_fpn (any may be None).
    `sp_start`: UTC start of the settlement period the trip was detected in."""
    now = now or datetime.now(timezone.utc)
    series = []
    for p in points:
        fpn, adj, mel, mil = _num(p.get("fpn")), _num(p.get("adjusted_fpn")), _num(p.get("mel")), _num(p.get("mil"))
        t = p["spot_time"]
        series.append({
            "t": t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "fpn": fpn, "adj": adj, "mel": mel, "mil": mil,
            "shortfall": round(abs(fpn - adj), 1) if fpn is not None and adj is not None else None,
            "future": t > now,
        })

    in_sp = [p for p in points if sp_start <= p["spot_time"] < sp_start + PERIOD]
    fpns = [v for v in (_num(p.get("fpn")) for p in in_sp) if v is not None]
    adjs = [v for v in (_num(p.get("adjusted_fpn")) for p in in_sp) if v is not None]
    mean_fpn = sum(fpns) / len(fpns) if fpns else None
    mean_adj = sum(adjs) / len(adjs) if adjs else None
    loss = abs(mean_fpn - mean_adj) if mean_fpn is not None and mean_adj is not None else None

    after = [s for s in series if s["t"] >= sp_start.strftime("%Y-%m-%dT%H:%M:%SZ")]
    shortfalls = [s["shortfall"] for s in after if s["shortfall"] is not None]
    gaps = [limit_gap(s["fpn"], s["mel"], s["mil"]) for s in after]
    mel_checked = [g for g in gaps if g[0] is not None]
    mel_limited = [g for g in mel_checked if g[0] > MEL_TOLERANCE_MW]
    limited_by_mil = sum(1 for g in mel_limited if g[1] == "mil")
    return {
        "points": series,
        "stats": {
            "mean_fpn": None if mean_fpn is None else round(mean_fpn, 1),
            "mean_adj": None if mean_adj is None else round(mean_adj, 1),
            "loss_mw": None if loss is None else round(loss, 1),
            "impact_pct": round(loss / abs(mean_fpn) * 100, 1) if loss is not None and mean_fpn else None,
            # "import" when the plant is mostly importing over the period (pumped storage pumping): the
            # loss is then import it could not take, not generation it could not deliver.
            "side": None if mean_fpn is None else ("import" if mean_fpn < 0 else "export"),
            "peak_shortfall_mw": max(shortfalls) if shortfalls else None,
            # Share of the points since the trip where the plant's own limit sat inside its plan (MEL for
            # an exporting plant, MIL for an importing one): high means the plant itself restricted its
            # capability (a genuine outage); low means something else (a Balancing Mechanism action,
            # say) pulled output down. `limit` says which limit did most of the limiting.
            "mel_limited_share": round(len(mel_limited) / len(mel_checked), 2) if mel_checked else None,
            "limit": "mil" if mel_limited and limited_by_mil * 2 > len(mel_limited) else "mel",
        },
    }


# A plant that is unavailable but publishes no REMIT notice still shows it in its own MEL
# (Maximum Export Level): it cuts MEL below its plan. These are the thresholds for calling that
# a cut rather than noise.
MEL_CUT_MIN_MW = 10.0        # average gap between plan and its limit (MEL, or MIL when importing) to call it a cut
MEL_CUT_MIN_SHARE = 0.5      # share of the recent minutes the gap must have been there
RECENT = timedelta(hours=1)


def mel_evidence(points: Sequence[Mapping], now: datetime | None = None) -> dict:
    """What the plant's own MEL says about a trip, independent of any REMIT notice.

    `points`: rows with spot_time (datetime), fpn, mel, mil. Looks at the last hour of points up to
    `now` for how far the plant's limit sits inside its plan (MEL below an export plan, MIL above an
    import plan -- see limit_gap), walks back to find when that run of cut minutes began, and reads
    the published (future) limit for when it returns to plan.

    Returns {verdict: "mel_cut" | "no_cut" | "no_data", unavailable_mw, since, back_at, limit}
    (`limit` is "mil" when the cut is on the import side, otherwise "mel").
    `back_at` is None if MEL never returns to plan inside the published window.
    """
    now = now or datetime.now(timezone.utc)
    rows = []   # (time, gap MW, which limit)
    for p in sorted(points, key=lambda r: r["spot_time"]):
        gap, side = limit_gap(_num(p.get("fpn")), _num(p.get("mel")), _num(p.get("mil")))
        if gap is not None:
            rows.append((p["spot_time"], gap, side))
    past = [r for r in rows if r[0] <= now]
    recent = [r for r in past if r[0] > now - RECENT]
    if not recent:
        return {"verdict": "no_data", "unavailable_mw": None, "since": None, "back_at": None, "limit": "mel"}

    cut = [r for r in recent if r[1] > MEL_TOLERANCE_MW]
    gap = sum(r[1] for r in cut) / len(cut) if cut else 0.0
    share = len(cut) / len(recent)
    verdict = "mel_cut" if share >= MEL_CUT_MIN_SHARE and gap >= MEL_CUT_MIN_MW else "no_cut"
    limit = "mil" if cut and sum(1 for r in cut if r[2] == "mil") * 2 > len(cut) else "mel"

    since = None
    if verdict == "mel_cut":
        for t, g, _ in reversed(past):              # the unbroken run of cut minutes ending now
            if g > MEL_TOLERANCE_MW:
                since = t
            else:
                break
    back_at = None
    if verdict == "mel_cut":
        back_at = next((t for t, g, _ in rows if t > now and g <= MEL_TOLERANCE_MW), None)
    iso = lambda t: None if t is None else t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    return {"verdict": verdict, "unavailable_mw": round(gap, 1) if cut else 0.0, "since": iso(since), "back_at": iso(back_at), "limit": limit}
