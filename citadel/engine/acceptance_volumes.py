"""Per-acceptance offer/bid volumes the way Elexon's published stack books them.

STANDALONE and not wired into the pricing engine. It exists to pin down, against Elexon's own
`balancing/settlement/stack/all/{offer,bid}` data, the rule the engine's FPN-relative model gets
wrong (see build_marginal_deltas()'s docstring: the T_FERRB-1 acceptance 14433 miss and the three
failed fixes), before anything in the engine is changed.

The rule, as found on T_LIONB-1 in SP23 of 2026-10-08 (every one of its 24 acceptances matched
Elexon's volumes to three decimals): an acceptance's volume is the area between its own declared
level path and the declared level path of the PREVIOUS acceptance in force at the same minute
(previous = accepted earlier, by acceptance time then number); where no earlier acceptance covers
the minute, the baseline is the plant's FPN. A positive difference is offer volume, a negative one
bid volume. The engine instead keeps only the latest acceptance per minute and measures it
against FPN, which nets away upward moves that Elexon books as offers.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable, Mapping

MINUTE = 60.0


def _ts(value) -> float:
    """Epoch seconds from an ISO string or a datetime/pandas Timestamp (naive means UTC)."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _interp(t: float, t0: float, t1: float, l0: float, l1: float) -> float:
    return l0 if t1 <= t0 else l0 + (l1 - l0) * ((t - t0) / (t1 - t0))


def _level_at(segments: list[tuple[float, float, float, float]], t: float) -> float | None:
    for t0, t1, l0, l1 in segments:
        if t0 <= t < t1:
            return _interp(t, t0, t1, l0, l1)
    return None


def acceptance_volumes(
    boalf_rows: Iterable[Mapping], pn_rows: Iterable[Mapping], sp_start: datetime, minutes: int = 30,
) -> dict[tuple[str, int], list[float]]:
    """{(bm_unit, acceptance_number): [offer_mwh, bid_mwh (negative)]} for one settlement period.

    `boalf_rows`: balancing/acceptances/all rows (bmUnit, acceptanceNumber, acceptanceTime,
    timeFrom, timeTo, levelFrom, levelTo). `pn_rows`: datasets/PN rows for the same period
    (bmUnit, timeFrom, timeTo, levelFrom, levelTo). Integrates at each minute's midpoint, which is
    exact for the one-minute-granularity piecewise-linear paths BOALF declares."""
    start = sp_start.astimezone(timezone.utc).timestamp()
    fpn: dict[str, list[tuple[float, float, float, float]]] = defaultdict(list)
    for r in pn_rows:
        fpn[r["bmUnit"]].append((_ts(r["timeFrom"]), _ts(r["timeTo"]), float(r["levelFrom"]), float(r["levelTo"])))

    per_unit: dict[str, dict[int, dict]] = defaultdict(dict)
    for r in boalf_rows:
        acc = per_unit[r["bmUnit"]].setdefault(
            int(r["acceptanceNumber"]), {"time": _ts(r["acceptanceTime"]), "segs": []})
        acc["segs"].append((_ts(r["timeFrom"]), _ts(r["timeTo"]), float(r["levelFrom"]), float(r["levelTo"])))

    out: dict[tuple[str, int], list[float]] = {}
    for unit, accs in per_unit.items():
        order = sorted(accs, key=lambda n: (accs[n]["time"], n))
        for m in range(minutes):
            t = start + (m + 0.5) * MINUTE
            prev = None
            for n in order:
                level = _level_at(accs[n]["segs"], t)
                if level is None:
                    continue
                base = prev if prev is not None else (_level_at(fpn.get(unit, []), t) or 0.0)
                delta = level - base
                vol = out.setdefault((unit, n), [0.0, 0.0])
                vol[0 if delta > 0 else 1] += delta / 60.0
                prev = level
    return {k: [round(v[0], 6), round(v[1], 6)] for k, v in out.items()}


def elexon_stack_volumes(offer_rows: Iterable[Mapping], bid_rows: Iterable[Mapping]) -> dict[tuple[str, int], list[float]]:
    """The same {(bm_unit, acceptance_number): [offer, bid]} shape from Elexon's published stack
    (raw `volume`, before de-minimis/arbitrage/NIV adjustments)."""
    out: dict[tuple[str, int], list[float]] = {}
    for r in offer_rows:
        out.setdefault((r["id"], int(r["acceptanceId"])), [0.0, 0.0])[0] += float(r["volume"])
    for r in bid_rows:
        out.setdefault((r["id"], int(r["acceptanceId"])), [0.0, 0.0])[1] += float(r["volume"])
    return out


def compare(mine: Mapping, theirs: Mapping, tolerance: float = 0.01) -> dict:
    """How closely `mine` reproduces `theirs`. Pairs with no volume on both sides are ignored."""
    keys = {k for k, v in mine.items() if abs(v[0]) + abs(v[1]) > 1e-9} | {k for k, v in theirs.items() if abs(v[0]) + abs(v[1]) > 1e-9}
    wrong = []
    for k in keys:
        a, b = mine.get(k, [0.0, 0.0]), theirs.get(k, [0.0, 0.0])
        err = max(abs(a[0] - b[0]), abs(a[1] - b[1]))
        if err > tolerance:
            wrong.append((err, k, a, b))
    wrong.sort(reverse=True)
    return {
        "pairs": len(keys), "matching": len(keys) - len(wrong), "wrong": wrong,
        "mine_offers": sum(v[0] for v in mine.values()), "mine_bids": sum(v[1] for v in mine.values()),
        "their_offers": sum(v[0] for v in theirs.values()), "their_bids": sum(v[1] for v in theirs.values()),
    }


# ---------------------------------------------------------------------------
# Band split: which bid/offer pair (and therefore which price) each part of an acceptance's volume
# belongs to.
# ---------------------------------------------------------------------------

def pair_bands(bod_rows: Iterable[Mapping]) -> dict[str, dict]:
    """{bm_unit: {"bands": [(pair_id, lo, hi, bid, offer)] in MW deviation from FPN}}.

    Same convention as stack.build_bod_ladder(): a pair's `levelTo` is the WIDTH of its band and
    the bands are stacked outwards from the FPN, positive pairs above it and negative pairs below
    it. Pair 1 covers 0..L1, pair 2 covers L1..L1+L2 and so on (mirrored for -1, -2, ...)."""
    by_unit: dict[str, list[Mapping]] = defaultdict(list)
    for r in bod_rows:
        by_unit[r["bmUnit"]].append(r)
    out: dict[str, dict] = {}
    for unit, rows in by_unit.items():
        bands = []
        for sign in (1, -1):
            edge = 0.0
            for r in sorted((r for r in rows if r["pairId"] * sign > 0), key=lambda r: abs(r["pairId"])):
                width = abs(float(r["levelTo"]))
                lo, hi = (edge, edge + width) if sign > 0 else (-(edge + width), -edge)
                bands.append((int(r["pairId"]), lo, hi, float(r["bid"]), float(r["offer"])))
                edge += width
        out[unit] = {"bands": bands}
    return out


def acceptance_bands(
    boalf_rows: Iterable[Mapping], pn_rows: Iterable[Mapping], bod_rows: Iterable[Mapping],
    sp_start: datetime, minutes: int = 30,
) -> list[dict]:
    """The previous-acceptance volume rule of acceptance_volumes(), split by bid/offer pair.

    Integrated second by second (a ramp can cross a band edge inside a minute, and the split of
    the area between the two paths across the bands is exact only if that is followed). Each layer
    of the area between an acceptance's path and its baseline is priced at the pair whose band it
    sits in: offer price when the acceptance is above its baseline, bid price when below.

    Returns rows {bm_unit, acceptance_number, pair_id, side ("offer"|"bid"), volume (MWh, negative
    for bids), price, so_flag, stor_flag}."""
    start = sp_start.astimezone(timezone.utc).timestamp()
    end = start + minutes * MINUTE
    bands = pair_bands(bod_rows)
    fpn: dict[str, list] = defaultdict(list)
    for r in pn_rows:
        fpn[r["bmUnit"]].append((_ts(r["timeFrom"]), _ts(r["timeTo"]), float(r["levelFrom"]), float(r["levelTo"])))

    per_unit: dict[str, dict[int, dict]] = defaultdict(dict)
    for r in boalf_rows:
        acc = per_unit[r["bmUnit"]].setdefault(int(r["acceptanceNumber"]), {
            "time": _ts(r["acceptanceTime"]), "segs": [], "so": bool(r.get("soFlag")), "stor": bool(r.get("storFlag"))})
        acc["segs"].append((_ts(r["timeFrom"]), _ts(r["timeTo"]), float(r["levelFrom"]), float(r["levelTo"])))

    acc_totals: dict[tuple, float] = defaultdict(float)
    for unit, accs in per_unit.items():
        unit_bands = bands.get(unit, {}).get("bands", [])
        order = sorted(accs, key=lambda n: (accs[n]["time"], n))
        for i, n in enumerate(order):
            for t0, t1, l0, l1 in accs[n]["segs"]:
                for s in range(int(max(t0, start)), int(min(t1, end))):
                    t = s + 0.5
                    a = _interp(t, t0, t1, l0, l1)
                    base = None
                    for j in range(i - 1, -1, -1):
                        base = _level_at(accs[order[j]]["segs"], t)
                        if base is not None:
                            break
                    f = _level_at(fpn.get(unit, []), t) or 0.0
                    a_dev, b_dev = a - f, (base - f if base is not None else 0.0)
                    lo, hi = min(a_dev, b_dev), max(a_dev, b_dev)
                    if hi - lo < 1e-9:
                        continue
                    sign = 1 if a_dev > b_dev else -1
                    for pair_id, blo, bhi, bid, offer in unit_bands:
                        overlap = min(hi, bhi) - max(lo, blo)
                        if overlap > 0:
                            acc_totals[(unit, n, pair_id, sign, bid, offer)] += overlap / 3600.0
    rows = []
    for (unit, n, pair_id, sign, bid, offer), vol in acc_totals.items():
        rows.append({
            "bm_unit": unit, "acceptance_number": n, "pair_id": pair_id, "side": "offer" if sign > 0 else "bid",
            "volume": round(sign * vol, 6), "price": offer if sign > 0 else bid,
            "so_flag": per_unit[unit][n]["so"], "stor_flag": per_unit[unit][n]["stor"],
        })
    return rows


def elexon_stack_rows(offer_rows: Iterable[Mapping], bid_rows: Iterable[Mapping]) -> dict[tuple, dict]:
    """Elexon's published rows keyed the same way as acceptance_bands(): (unit, acceptance, pair, side)."""
    out: dict[tuple, dict] = {}
    for side, rows in (("offer", offer_rows), ("bid", bid_rows)):
        for r in rows:
            if r.get("acceptanceId") is None:      # a non-BM action (e.g. DISBSAD): not an acceptance
                continue
            k = (r["id"], int(r["acceptanceId"]), int(r["bidOfferPairId"]), side)
            d = out.setdefault(k, {"volume": 0.0, "price": r["originalPrice"], "so_flag": bool(r["soFlag"]), "cadl": r.get("cadlFlag")})
            d["volume"] += float(r["volume"])
    return out


def elexon_non_bm_rows(offer_rows: Iterable[Mapping], bid_rows: Iterable[Mapping]) -> list[dict]:
    """Elexon's published stack rows that are not BM acceptances (DISBSAD and other non-BM
    actions), shaped like acceptance_bands() rows so they can join the price calculation.
    Taken from the published stack for now: the live engine blends DISBSAD itself."""
    out = []
    for side, rows in (("offer", offer_rows), ("bid", bid_rows)):
        for i, r in enumerate(x for x in rows if x.get("acceptanceId") is None):
            out.append({"bm_unit": f"{r['id']}", "acceptance_number": -(i + 1) if side == "offer" else -(10_000 + i + 1),
                        "pair_id": 0, "side": side, "volume": float(r["volume"]), "price": float(r["originalPrice"]),
                        "so_flag": bool(r["soFlag"]), "stor_flag": bool(r.get("storProviderFlag"))})
    return out


def compare_bands(mine_rows: Iterable[Mapping], theirs: Mapping[tuple, Mapping], tolerance: float = 0.01) -> dict:
    mine = {(r["bm_unit"], r["acceptance_number"], r["pair_id"], r["side"]): r for r in mine_rows}
    keys = {k for k, r in mine.items() if abs(r["volume"]) > 1e-9} | {k for k, r in theirs.items() if abs(r["volume"]) > 1e-9}
    wrong = []
    for k in keys:
        a, b = mine.get(k), theirs.get(k)
        av, bv = (a["volume"] if a else 0.0), (b["volume"] if b else 0.0)
        ap, bp = (a["price"] if a else None), (b["price"] if b else None)
        price_bad = a is not None and b is not None and abs(ap - bp) > 1e-6
        if abs(av - bv) > tolerance or price_bad:
            wrong.append((max(abs(av - bv), 1.0 if price_bad else 0.0), k, av, bv, ap, bp))
    wrong.sort(reverse=True)
    return {"rows": len(keys), "matching": len(keys) - len(wrong), "wrong": wrong}


def price_from_bands(band_rows: Iterable[Mapping], market_index_price: float | None = None,
                     cadl_acceptances: set[tuple[str, int]] | None = None, dmat_mwh: float = 0.1) -> float:
    """The energy imbalance price for one period from acceptance_bands() rows, through the same
    Classification / NIV / Replacement Price / PAR code the engine uses (engine/imbalance_price.py).

    De-minimis tagging is applied here per WHOLE acceptance and side (as Elexon does: the guide's
    own definition, and what the published `dmatAdjustedVolume` shows), instead of per row.
    `cadl_acceptances` marks (unit, acceptance) pairs treated as CADL-flagged, which count as
    flagged alongside SO and STOR flags."""
    from .imbalance_price import compute_imbalance_price

    rows = list(band_rows)
    totals: dict[tuple, float] = defaultdict(float)
    for r in rows:
        totals[(r["bm_unit"], r["acceptance_number"], r["side"])] += abs(r["volume"])
    cadl = cadl_acceptances or set()
    feed = [
        {"delta": r["volume"], "m_orig_price": r["price"],
         "so_flag": bool(r["so_flag"] or r["stor_flag"] or (r["bm_unit"], r["acceptance_number"]) in cadl)}
        for r in rows
        if abs(r["volume"]) > 1e-9 and totals[(r["bm_unit"], r["acceptance_number"], r["side"])] >= dmat_mwh
    ]
    price, _ = compute_imbalance_price(feed, market_index_price=market_index_price, apply_de_minimis=False)
    return price


# ---------------------------------------------------------------------------
# Engine integration: the rows build_price_stack() prices when the acceptance model is on.
# ---------------------------------------------------------------------------

_STACK_ROW_COLUMNS = [
    "settlementDate", "settlementPeriod", "max_ta", "bmUnit", "storFlag", "deemedBoFlag", "soFlag", "cadlFlag",
    "firstStageFlag", "acceptanceNumber", "reversal", "ap_mult_vol", "delta", "vol_to_price", "disbsad_cost",
    "m_orig_price", "dmatKey",
]


# The per-period result only changes when that period's own inputs change, and each recompute cycle
# touches a handful of periods at most -- so remember the last result per (date, period), keyed by a
# hash of exactly the inputs it depends on. Lives in the (long-lived) pool worker process.
_PERIOD_CACHE: dict[tuple, tuple] = {}
_PERIOD_CACHE_MAX = 64
_BOALF_KEYS = ["bmUnit", "acceptanceNumber", "acceptanceTime", "timeFrom", "timeTo", "levelFrom", "levelTo", "soFlag", "storFlag"]


def _digest(df, columns) -> int:
    import pandas as pd

    cols = [c for c in columns if c in df.columns]
    if df.empty or not cols:
        return 0
    return int(pd.util.hash_pandas_object(df[cols].astype(str), index=False).sum())


def acceptance_stack_rows(boalf_df, pn_df, bod_df, flags_df, periods, max_ta):
    """Pre-pricing stack rows (one per unit, acceptance, bid/offer pair and side) for every
    (settlement date, period) in `periods`, from the engine's own raw frames.

    `flags_df`: one row per (bmUnit, acceptanceNumber) with deemedBoFlag/soFlag/storFlag/cadlFlag/
    firstStageFlag, as compute_stack() builds it. `periods`: DataFrame or iterable of
    (settlementDate, settlementPeriod). Rows come back in the column layout build_price_stack()
    builds from its own per-minute frame, with an extra `dmatKey` so the price step can apply
    de-minimis tagging per whole acceptance and side."""
    import pandas as pd

    from ..settlement import sp_start_utc

    empty = pd.DataFrame()
    boalf_df = empty if boalf_df is None else boalf_df
    pn_df = empty if pn_df is None else pn_df
    bod_df = empty if bod_df is None else bod_df
    flags_df = empty if flags_df is None else flags_df

    # parse each time column once for every period's overlap test
    b_from = pd.to_datetime(boalf_df["timeFrom"], utc=True) if not boalf_df.empty else None
    b_to = pd.to_datetime(boalf_df["timeTo"], utc=True) if not boalf_df.empty else None
    p_from = pd.to_datetime(pn_df["timeFrom"], utc=True) if not pn_df.empty else None
    p_to = pd.to_datetime(pn_df["timeTo"], utc=True) if not pn_df.empty else None
    bod_sd = pd.to_datetime(bod_df["settlementDate"]).dt.date if not bod_df.empty else None

    if hasattr(periods, "itertuples"):
        period_list = [(row[0], int(row[1])) for row in periods.itertuples(index=False)]
    else:
        period_list = [(sd, int(sp)) for sd, sp in periods]

    out = []
    for sd_raw, sp in period_list:
        sd = pd.Timestamp(sd_raw).date()
        sp_start = sp_start_utc(sd, sp)
        sp_end = sp_start + pd.Timedelta(minutes=30)

        b_slice = boalf_df[(b_from < sp_end) & (b_to > sp_start)] if b_from is not None else empty
        p_slice = pn_df[(p_from < sp_end) & (p_to > sp_start)] if p_from is not None else empty
        bod_slice = bod_df[(bod_sd == sd) & (bod_df["settlementPeriod"] == sp)] if bod_sd is not None else empty
        f_slice = flags_df.merge(b_slice[["bmUnit", "acceptanceNumber"]].drop_duplicates(), on=["bmUnit", "acceptanceNumber"]) \
            if not flags_df.empty and not b_slice.empty else empty
        signature = (
            _digest(b_slice, _BOALF_KEYS), _digest(p_slice, ["bmUnit", "timeFrom", "timeTo", "levelFrom", "levelTo"]),
            _digest(bod_slice, ["bmUnit", "pairId", "levelTo", "bid", "offer"]),
            _digest(f_slice, ["bmUnit", "acceptanceNumber", "soFlag", "storFlag", "deemedBoFlag", "cadlFlag"]),
        )

        cached = _PERIOD_CACHE.get((sd, sp))
        if cached is not None and cached[0] == signature:
            rows = cached[1]
        else:
            rows = _period_rows(b_slice, p_slice, bod_slice, f_slice, sd, sp)
            if len(_PERIOD_CACHE) >= _PERIOD_CACHE_MAX:
                _PERIOD_CACHE.clear()
            _PERIOD_CACHE[(sd, sp)] = (signature, rows)
        out.extend({**r, "max_ta": max_ta} for r in rows)
    return pd.DataFrame(out, columns=_STACK_ROW_COLUMNS)


def _period_rows(b_slice, p_slice, bod_slice, f_slice, sd, sp) -> list[dict]:
    import pandas as pd

    from ..settlement import sp_start_utc

    flags = {}
    for r in f_slice.to_dict("records"):
        flags[(r["bmUnit"], int(r["acceptanceNumber"]))] = r
    bands = acceptance_bands(
        b_slice.to_dict("records"), p_slice.to_dict("records"), bod_slice.to_dict("records"), sp_start_utc(sd, sp))
    taken = {(r["bm_unit"], r["acceptance_number"], r["pair_id"]) for r in bands if r["side"] == "offer"}
    rows = []
    for r in bands:
        if abs(r["volume"]) < 1e-9:
            continue
        unit, acc, pair = r["bm_unit"], r["acceptance_number"], r["pair_id"]
        fl = flags.get((unit, acc), {})
        so, stor = bool(fl.get("soFlag", r["so_flag"])), bool(fl.get("storFlag", r["stor_flag"]))
        cadl = bool(fl.get("cadlFlag", False))
        # one offer and one bid can share a (unit, acceptance, pair): keep their stack keys distinct
        name = f"{unit}_{pair}" + ("~bid" if r["side"] == "bid" and (unit, acc, pair) in taken else "")
        rows.append({
            "settlementDate": pd.Timestamp(sd, tz="UTC"), "settlementPeriod": sp, "max_ta": None,
            "bmUnit": name, "storFlag": stor, "deemedBoFlag": bool(fl.get("deemedBoFlag", False)),
            "soFlag": so, "cadlFlag": cadl, "firstStageFlag": so or stor or cadl,
            "acceptanceNumber": acc, "reversal": 1.0,
            "ap_mult_vol": r["price"] * abs(r["volume"]), "delta": r["volume"], "vol_to_price": abs(r["volume"]),
            "disbsad_cost": 0.0, "m_orig_price": r["price"], "dmatKey": f"{unit}|{acc}|{r['side']}",
        })
    return rows
