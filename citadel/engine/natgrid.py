"""National Grid's balancing trades, resolved from the two sources that describe them.

NESO's trade list is forward-looking and evolving (trades appear, are revised and
extended as they are agreed); Elexon's DISBSAD is the settled record and also carries
small actions NESO's list omits. Neither alone is right, so every consumer (the Natgrid
page, the Fundies natgrid row, the FPN NATGRID row) goes through `resolve_trades`:

  * a PAST period (it has ended) that DISBSAD has rows for: DISBSAD is the record;
  * a current or future period, or a past one DISBSAD has nothing for yet: NESO's trades,
    plus any DISBSAD actions that NESO does not already have. DISBSAD sometimes lists
    actions for periods that are not over; those are matched to NESO's trades (same price
    and size) so an action both sources know about is counted once, and only the extras
    are added.

Volumes keep their sign: positive is National Grid buying (an increase), negative is
selling (a decrease) -- and prices can be negative too.

`build_ladder` shapes the resolved trades for the Natgrid page, shaped like the reference
app's natgrid response grouped by date and settlement period: {date: {sp: [rows]}}. Each
period's buys and sells are laddered separately, in ascending price order, and `disc_cumm`
is the running volume on that side (positive for buys, negative for sells), so a chart
can stack buys up from zero and sells down from it.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, timezone

from ..settlement import sp_end_utc

# Two entries describe the same trade if their prices agree to this and their sizes to
# MATCH_MW (DISBSAD volumes are rounded MWh per period, hence the size slack).
MATCH_PRICE = 0.011
MATCH_MW = 0.6


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _trade_rows(neso_trades: list[dict], disbsad_records: list[dict]) -> list[dict]:
    """Normalised per-trade rows from the raw source records, with NO preference between the
    sources: date, sp, price (None if the source gave none), volume_mw (signed), so_flag,
    reason, source. Rows without a usable size are dropped."""
    rows: list[dict] = []
    for t in neso_trades:
        sp, date_str = t.get("SP"), str(t.get("Date", ""))[:10]
        # GTMA rows carry the block's MW; the older upcoming-trades rows only
        # carry Volume, which is MWh per SP (so * 2 for MW).
        mw = _num(t.get("MW"))
        if mw is None:
            vol = _num(t.get("Volume"))
            mw = vol * 2 if vol is not None else None
        if sp is None or not date_str or mw is None:
            continue
        rows.append({"date": date_str, "sp": int(sp), "price": _num(t.get("Price")), "volume_mw": mw,
                     "so_flag": t.get("SO_Flag"), "reason": t.get("Reason"), "source": t.get("Source", "NESO")})

    for d in disbsad_records:
        sp, date_str = d.get("settlementPeriod"), str(d.get("settlementDate", ""))[:10]
        volume, cost = _num(d.get("volume")), _num(d.get("cost"))
        if sp is None or not date_str or not volume:
            continue
        flag = d.get("soFlag")
        rows.append({"date": date_str, "sp": int(sp), "price": (cost / volume) if cost is not None else None,
                     "volume_mw": volume * 2,
                     "so_flag": "T" if flag is True else "F" if flag is False else flag,
                     "reason": d.get("service"), "source": "DISBSAD"})
    return rows


def _same_trade(a: dict, b: dict) -> bool:
    if abs(a["volume_mw"] - b["volume_mw"]) > MATCH_MW:
        return False
    if a["price"] is None or b["price"] is None:
        return True  # no price on one side: size is all there is to go on
    return abs(a["price"] - b["price"]) <= MATCH_PRICE


def _extras(disbsad: list[dict], neso: list[dict]) -> list[dict]:
    """The DISBSAD actions NESO does not already have: each NESO trade claims at most one
    matching DISBSAD action, and whatever is left over is an extra."""
    free = list(disbsad)
    for n in neso:
        match = next((d for d in free if _same_trade(n, d)), None)
        if match is not None:
            free.remove(match)
    return free


def resolve_trades(neso_trades: list[dict], disbsad_records: list[dict], now: datetime | None = None) -> list[dict]:
    """One list of per-trade rows (see `_trade_rows`) with the source rule in the module
    docstring applied period by period."""
    now = now or datetime.now(timezone.utc)
    neso_by: dict[tuple[str, int], list[dict]] = defaultdict(list)
    disb_by: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for r in _trade_rows(neso_trades, []):
        neso_by[(r["date"], r["sp"])].append(r)
    for r in _trade_rows([], disbsad_records):
        disb_by[(r["date"], r["sp"])].append(r)

    out: list[dict] = []
    for key in set(neso_by) | set(disb_by):
        neso, disb = neso_by.get(key, []), disb_by.get(key, [])
        try:
            ended = sp_end_utc(date.fromisoformat(key[0]), key[1]) <= now
        except ValueError:
            ended = False
        if ended and disb:
            out.extend(disb)                                   # settled: DISBSAD is the record
        else:
            out.extend(neso)                                   # current / future / not yet settled
            out.extend(_extras(disb, neso))                    # plus what only DISBSAD knows about
    return out


def total_mw_by_period(neso_trades: list[dict], disbsad_records: list[dict], now: datetime | None = None) -> dict[tuple[str, int], float]:
    """Net MW per (date string, settlement period) after resolving the sources -- what the
    Fundies and FPN natgrid rows are built from."""
    totals: dict[tuple[str, int], float] = defaultdict(float)
    for r in resolve_trades(neso_trades, disbsad_records, now):
        totals[(r["date"], r["sp"])] += r["volume_mw"]
    return dict(totals)


def build_ladder(neso_trades: list[dict], disbsad_records: list[dict], now: datetime | None = None) -> dict[str, dict[str, list[dict]]]:
    """{date: {sp: [{side, disc_cumm, price, volume_mw, so_flag, reason, source}, ...]}}: buys first
    (ascending price, running volume rising from 0), then sells (ascending price, running volume
    falling from 0). Trades with no price cannot be placed on a price ladder and are left out of it."""
    grouped: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for r in resolve_trades(neso_trades, disbsad_records, now):
        if r["price"] is not None:
            grouped[(r["date"], r["sp"])].append(r)

    ladder: dict[str, dict[str, list[dict]]] = {}
    for (date_str, sp), trades in sorted(grouped.items()):
        out = []
        for side, sign in (("buy", 1), ("sell", -1)):
            running = 0.0
            side_trades = [t for t in trades if (t["volume_mw"] > 0) == (sign > 0) and t["volume_mw"] != 0]
            for t in sorted(side_trades, key=lambda x: (x["price"], -abs(x["volume_mw"]))):
                running += t["volume_mw"]
                out.append({"side": side, "disc_cumm": round(running, 1), "price": round(t["price"], 2),
                            "volume_mw": round(t["volume_mw"], 1), "so_flag": t["so_flag"],
                            "reason": t["reason"], "source": t["source"]})
        ladder.setdefault(date_str, {})[str(sp)] = out
    return ladder
