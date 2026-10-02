"""Presentation-layer shaping of a computed stack into the view the user
actually wants -- confirmed directly against Elexon's own Imbalance Pricing
Guidance Note (v15.0, section on Ranked Sets / Classification / NIV
Tagging), not just inferred:

- Two ranked sets, one per direction (Buy Ranked Set = offers, Sell Ranked
  Set = bids), each ordered price descending -- for offers that's most
  expensive first (high price = expensive to buy); for bids it's cheapest
  first (a HIGHER bid price is CHEAPER for the system -- confirmed
  directly by the user and by the guide's own wording: "the lower the
  price the more expensive" a sell/bid action is). One descending sort by
  raw price on a combined list naturally reproduces both directions'
  correct order at once. Rows tied on the exact same price (different
  bm_units) are then ordered by cumulative MWh as a second sort key, so
  ties don't fall back to arbitrary/insertion order -- descending for
  offers, ascending for bids, matching each direction's own top-to-bottom
  cumulative trend (see `_display_rows`; getting this backwards for
  either direction produces a visible sawtooth dip in the cumulative
  column at every tied price).

- Classification (guide, "Ranked Sets" section): within each direction
  independently, an SO-Flagged ("First Stage Flagged") action cheaper than
  the most expensive *unflagged* action in that same direction is
  reclassified "Second Stage Unflagged" -- it is priced at its own
  original price from then on, same as a genuine unflagged action, rather
  than being repriced by the Replacement Price. Only flagged actions still
  *more* expensive than that threshold stay "Second Stage Flagged" (get
  repriced). If a direction has no unflagged actions at all, every flagged
  action in it stays flagged (guide's own explicit rule).

  Re-reading the guide's own "Ranked Sets" section (confirmed with the
  user): there is only ONE ranked set per direction (Buy or Sell), ordered
  purely by price -- Classification changes a flagged action's *pricing*
  treatment, never its rank position or which visual group it belongs to.
  So a cheap flagged action is NOT moved into the unflagged display group
  here (an earlier version of this module did that, which was a display
  bug, not something the guide actually calls for) -- it stays displayed
  under the flagged group (see `_classify_effective` / `split_and_sort`),
  but its volume is still counted at its own true (cheap) rank when
  computing cumulative MWh, exactly as if it sat in one combined list.

- Cumulative MWh is therefore computed once per direction, over ALL of
  that direction's rows (flagged and unflagged together) in a single
  price-ranked walk -- see `_cumulative_by_id`. The running total always
  accumulates starting from the cheapest action toward the most expensive
  one (confirmed directly by the user), independent of *display* order or
  which group (flagged/unflagged) a row is shown in. For offers that's
  ascending price (cheapest/lowest first); for bids it's descending price
  (cheapest/highest first) -- which happens to already match the bids' own
  display order, but not the offers' (offers display most-expensive-first,
  so their cumulative column decreases top-to-bottom while bids' increases
  top-to-bottom -- both mean the same thing: "total volume cheaper than or
  equal to this action, across the whole direction").

- Multiple stored rows can legitimately share the same (bm_unit, price) --
  e.g. the same acceptance split across adjacent BOD price bands, or
  across a reversal boundary in engine/stack.py's six-case allocation.
  Correct as computed, but visually just clutter/duplicates, so they're
  summed into one display row per (bm_unit, price) before cumulative
  volume is computed (confirmed with the user: this is a display
  optimization, not a computation change).

This is a display-layer approximation of the guide's process (real
Classification also involves Emergency flags and STOR priced via
Utilisation/Reserve Scarcity Price before classification, and De Minimis/
Arbitrage/NIV Tagging happen around it -- none of that is reproduced here,
only the SO-Flag reclassification the user specifically asked about; CADL
Flagging IS reproduced, but upstream in engine/stack.py's
compute_cadl_flags(), which folds a computed CADL flag straight into
`so_flag` before rows ever reach this module -- see that function's
docstring). The underlying price-setting math in engine/stack.py is
unchanged by this -- this module only decides how rows are grouped and
ordered for display.
"""

from __future__ import annotations

from typing import Any

from .classification import classify_prices


def _to_view_row(record: dict[str, Any], cumulative_mwh: float, effective_flagged: bool) -> dict[str, Any]:
    delta = float(record["delta"])
    return {
        "bm_unit": record["bm_unit"],
        "direction": "Offer" if delta > 0 else "Bid",
        "so_flag": bool(record["so_flag"]),
        "cadl_flag": bool(record["cadl_flag"]),
        "effective_flagged": effective_flagged,
        "stor_flag": bool(record["stor_flag"]),
        "deemed_bo_flag": bool(record["deemed_bo_flag"]),
        "acceptance_number": int(record["acceptance_number"]),
        "delta_mwh": round(delta, 4),
        "price_gbp_mwh": round(float(record["m_orig_price"]), 2),
        "cumulative_mwh": round(cumulative_mwh, 4),
        "price_setting": abs(float(record.get("misik_imb_price") or 0)) > 1e-9,
        "reversal": float(record["reversal"]),
    }


def _merge_duplicates(rows: list[dict]) -> list[dict]:
    """Sums rows sharing (bm_unit, price) into one -- see module docstring.
    Volume (`delta`) and its contribution to the period price
    (`misik_imb_price`) are additive, so summing is exact; everything else
    is carried over from the first row in the group (flags/direction are
    invariant within a group by construction -- rows only merge if they
    already share a price, and a genuinely different action landing on the
    exact same price by coincidence is rare enough not to special-case).
    """
    merged: dict[tuple[str, float], dict] = {}
    for r in rows:
        key = (r["bm_unit"], round(float(r["m_orig_price"]), 2))
        if key not in merged:
            merged[key] = {**r, "delta": float(r["delta"]), "misik_imb_price": float(r.get("misik_imb_price") or 0)}
        else:
            merged[key]["delta"] += float(r["delta"])
            merged[key]["misik_imb_price"] += float(r.get("misik_imb_price") or 0)
    return list(merged.values())


def _classify_effective(rows: list[dict]) -> dict[int, bool]:
    """One direction's rows (all offers, or all bids) -> {id(row): effective
    flagged status}, per classification.classify_prices() -- shared with
    engine/imbalance_price.py so this always agrees with what was actually
    used to compute the price. Used only to tag rows for display (whether
    a flagged action is being priced at its own price or repriced), NOT to
    decide which group a row is shown under -- see module docstring.
    `rows` must all share the same sign of `delta`.
    """
    if not rows:
        return {}
    is_offer = float(rows[0]["delta"]) > 0
    entries = [(float(r["m_orig_price"]), bool(r["so_flag"])) for r in rows]
    effective = classify_prices(entries, is_offer)
    return {id(r): eff for r, eff in zip(rows, effective)}


def _cumulative_by_id(rows: list[dict]) -> dict[int, float]:
    """One direction's rows (flagged and unflagged together) -> {id(row):
    cumulative MWh}, from a single combined price-ranked walk -- see
    module docstring on why this must span both groups rather than being
    computed separately per group.
    """
    if not rows:
        return {}
    is_offer = float(rows[0]["delta"]) > 0

    # Accumulate cheapest-first regardless of display order: ascending
    # price for offers, descending (i.e. highest/most-positive first) for
    # bids -- see module docstring.
    accumulation_order = sorted(rows, key=lambda r: float(r["m_orig_price"]), reverse=not is_offer)
    cumulative: dict[int, float] = {}
    running = 0.0
    for r in accumulation_order:
        running += abs(float(r["delta"]))
        cumulative[id(r)] = running
    return cumulative


def _display_rows(rows: list[dict], cumulative_by_id: dict[int, float], effective_by_id: dict[int, bool]) -> list[dict]:
    if not rows:
        return []
    is_offer = float(rows[0]["delta"]) > 0
    # Price descending always (see module docstring). Ties -- rows sharing
    # the exact same price, different bm_units -- are broken by cumulative
    # MWh in whichever direction continues that direction's own natural
    # top-to-bottom trend, so the column never dips within a tied price
    # group: offers' cumulative decreases top-to-bottom (most expensive
    # first, so ties go highest-cumulative-first); bids' display order
    # already matches accumulation order (cheapest first), so their
    # cumulative INCREASES top-to-bottom, meaning ties must go lowest-
    # cumulative-first -- the opposite of offers. Encoding that as a sign
    # flip on the second sort key lets one reverse=True handle both.
    tie_sign = 1 if is_offer else -1
    display_order = sorted(
        rows, key=lambda r: (float(r["m_orig_price"]), tie_sign * cumulative_by_id[id(r)]), reverse=True
    )
    return [_to_view_row(r, cumulative_by_id[id(r)], effective_by_id[id(r)]) for r in display_order]


def split_and_sort(rows: list[dict]) -> dict:
    """`rows`: snake_case dicts for one settlement period (see module
    docstring for the expected keys and the Classification behaviour).
    Returns the unflagged/flagged stacks (grouped by each row's own raw
    SO flag -- see module docstring) plus the period's NIV and synthetic
    imbalance price.
    """
    offers = [r for r in rows if float(r["delta"]) > 0]
    bids = [r for r in rows if float(r["delta"]) < 0]

    total_price = float(rows[0]["total_misik_price"]) if rows else None
    niv = float(rows[0]["total_delta"]) if rows else None

    unflagged: list[dict] = []
    so_flagged: list[dict] = []
    for direction_rows in (offers, bids):
        if not direction_rows:
            continue
        raw_unflagged = _merge_duplicates([r for r in direction_rows if not bool(r["so_flag"])])
        raw_flagged = _merge_duplicates([r for r in direction_rows if bool(r["so_flag"])])
        combined = raw_unflagged + raw_flagged

        effective_by_id = _classify_effective(combined)
        cumulative_by_id = _cumulative_by_id(combined)

        unflagged += _display_rows(raw_unflagged, cumulative_by_id, effective_by_id)
        so_flagged += _display_rows(raw_flagged, cumulative_by_id, effective_by_id)

    return {
        "total_misik_price_gbp_mwh": round(total_price, 2) if total_price is not None else None,
        "niv_mwh": round(niv, 4) if niv is not None else None,
        "unflagged": unflagged,
        "so_flagged": so_flagged,
    }
