"""Pure helpers for storing National Grid trades locally: normalising NESO
trade blocks and Elexon DISBSAD actions into table rows, spotting when a NESO
trade has been revised, and reconciling the two sources period by period.

NESO's trade list and Elexon's DISBSAD describe the same actions, so their
per-period volumes should agree -- storing both lets a discrepancy be seen
and survives either source being unreachable or revised.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Mapping

from ..engine.natgrid import _trade_rows

MATCH_TOLERANCE_MW = 1.0
_TRACKED = ("start_time", "end_time", "volume_mw", "price", "so_flag", "reason")


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _utc(v) -> datetime | None:
    if v is None or v == "":
        return None
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)


def normalise_block(raw: Mapping) -> dict | None:
    """One NESO GTMA record -> natgrid_trades columns (None if unusable)."""
    try:
        start, end, mw = _utc(raw["StartTime"]), _utc(raw["EndTime"]), _num(raw["Volume"])
    except (KeyError, ValueError):
        return None
    trade_id = raw.get("ID")
    if not trade_id or start is None or end is None or mw is None:
        return None
    return {
        "id": str(trade_id), "start_time": start, "end_time": end, "volume_mw": mw,
        "price": _num(raw.get("Price")), "cost": _num(raw.get("Cost")),
        "so_flag": raw.get("SO_Flag"), "reason": raw.get("Reason"), "source_updated": _utc(raw.get("Last_Updated")),
    }


def block_changed(old: Mapping, new: Mapping) -> bool:
    """True when a stored NESO trade differs from the freshly fetched one in a
    way that matters (timing, volume, price, flag, reason) -- a mere
    Last_Updated bump or re-fetch is not a change."""
    for key in _TRACKED:
        a, b = old.get(key), new.get(key)
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if abs(a - b) > 1e-6:
                return True
        elif a != b:
            return True
    return False


def normalise_disbsad(raw: Mapping) -> dict | None:
    """One Elexon DISBSAD action -> disbsad_actions columns (None if unusable)."""
    try:
        sd, sp, action_id = date.fromisoformat(str(raw["settlementDate"])[:10]), int(raw["settlementPeriod"]), int(raw["id"])
    except (KeyError, TypeError, ValueError):
        return None
    volume, cost = _num(raw.get("volume")), _num(raw.get("cost"))
    return {
        "settlement_date": sd, "settlement_period": sp, "action_id": action_id,
        "volume": volume, "cost": cost, "price": (cost / volume) if volume and cost is not None else None,
        "so_flag": raw.get("soFlag"), "stor_flag": raw.get("storFlag"), "party_id": raw.get("partyId"),
        "asset_id": raw.get("assetId"), "service": raw.get("service"), "is_tendered": raw.get("isTendered"),
    }


def reconcile(neso_trades: list[dict], disbsad_records: list[dict], tolerance_mw: float = MATCH_TOLERANCE_MW) -> list[dict]:
    """Per (date, SP): net MW from NESO vs net MW from DISBSAD (volume is MWh
    per SP, so * 2), with a status of match / mismatch / neso_only / disbsad_only."""
    def totals(rows: list[dict]) -> dict[tuple[str, int], float]:
        out: dict[tuple[str, int], float] = defaultdict(float)
        for r in rows:
            out[(r["date"], r["sp"])] += r["volume_mw"]
        return out

    neso = totals(_trade_rows(neso_trades, []))
    disb = totals(_trade_rows([], disbsad_records))
    out = []
    for key in sorted(set(neso) | set(disb)):
        n, d = neso.get(key), disb.get(key)
        if n is None:
            status = "disbsad_only"
        elif d is None:
            status = "neso_only"
        else:
            status = "match" if abs(n - d) <= tolerance_mw else "mismatch"
        out.append({"date": key[0], "sp": key[1], "neso_mw": None if n is None else round(n, 1),
                    "disbsad_mw": None if d is None else round(d, 1),
                    "diff_mw": None if n is None or d is None else round(n - d, 1), "status": status})
    return out
