"""Elexon's actual Imbalance Pricing methodology (Imbalance Pricing
Guidance Note v15.0), replacing the original notebook's NIV/PAR-band
heuristic in engine/stack.py with the real, documented multi-stage
process (section numbers match the guide's own 10-stage worked example):

  3. Rank the Buy Ranked Set (offers) and Sell Ranked Set (bids), cheapest
     first in each -- see classification.expense()'s docstring for the
     direction-dependent "cheapest" convention.
  4. De Minimis Tagging -- actions under DMAT (0.1 MWh) are excluded
     entirely (both price and volume) -- checked per row here, a known
     simplification against the guide's own per-whole-action definition;
     see _de_minimis()'s own docstring on why an acceptance-level version
     was tried and reverted. Arbitrage Tagging -- equal volumes removed
     from both sides wherever an offer's price is <= a bid's price (a
     crossed, arbitrage-able pair), matched from each side's own cheapest
     end.
  5. Classification -- see classification.py (shared with engine/view.py's
     display grouping, so the two never disagree).
  6. NIV Tagging -- the smaller ranked set's total volume is netted off
     the top (most expensive end) of the larger one; what survives on the
     larger side is this period's NIV.
  7. Replacement Price -- any Second-Stage-Flagged action still surviving
     NIV Tagging is repriced to the volume-weighted average of the most
     expensive 1 MWh (RPAR) of Second-Stage-Unflagged survivors in the
     same direction. If there are none at all -- e.g. a long period with
     no unflagged bids, or a short one with no unflagged offers -- the
     guide's own fallback applies: reprice to the settlement period's
     Market Index Price instead (see `market_index_price` below; confirmed
     directly with the user this is the real-world case worth handling,
     not just a textbook edge case). The same Market Price fallback also
     covers the "no actions left in NIV at all" case in NIV Tagging.
  8. PAR Tagging -- the final energy imbalance price is the volume-
     weighted average of the most expensive PAR (1 MWh) of the (now fully
     effectively-unflagged) surviving stack.

KNOWN GAPS (not implemented -- no data source ingested for these yet):
  - Stage 2, STOR action pricing via Utilisation Price / Reserve Scarcity
    Price -- STOR-flagged actions are treated as ordinary SO-Flagged ones.
  - Stage 9, Buy/Sell Price Adjustment (BPA/SPA) -- treated as zero.
  - Stage 10, Transmission Loss Multiplier -- treated as 1.0.
  - TERRE/RRAUSS/VGB (non-BM balancing actions beyond DISBSAD) -- not
    ingested; DISBSAD's synthetic rows (see engine/stack.py's
    blend_disbsad) are this engine's only BSAA-like input.
These are genuine, bounded sources of remaining error against Elexon's
real published price, on top of (but separate from) the core Classification
/ NIV Tagging / PAR Tagging mechanism this module implements faithfully.
Use scripts/backtest_sp.py to measure how much they matter in practice.
"""

from __future__ import annotations

from dataclasses import dataclass

from .classification import classify_prices, expense

DMAT = 0.1  # De Minimis Acceptance Threshold, MWh (current BSC value)
PAR = 1.0   # Price Averaging Reference volume, MWh (current BSC value)


@dataclass
class _Tag:
    """One row's mutable working state through the tagging pipeline -- the
    caller's own row dict (`ref`) is never mutated, so it can always be
    mapped back to by `id(ref)` once this module is done with it.
    """

    ref: dict
    is_offer: bool
    price: float
    vol: float  # remaining volume still in play; shrinks through the pipeline
    so_flag: bool
    par_vol: float = 0.0
    priced_at: float = 0.0


def _de_minimis(tags: list[_Tag]) -> list[_Tag]:
    """Per-row check against DMAT (the guide's own De Minimis Tagging is
    defined over whole balancing actions -- since engine/stack.py's
    six-case BOD-band allocation routinely splits one accepted action's
    volume across several price bands, each its own row/tag here, this is
    a known, deliberate simplification, not a literal reading).

    An acceptance-level version (grouping every band fragment of the same
    originating action together before testing DMAT) was tried IN THIS
    function and reverted: it measurably closed the gap between our
    displayed NIV and Elexon's own reported netImbalanceVolume on several
    real periods, but on another real period (SP40, 2026-09-18) it
    rescued two genuinely real, larger acceptances that our own BOD-band
    split had spread across several price levels -- and PAR Tagging is
    sensitive enough to exactly which price levels hold volume that
    adding that (correctly totalled) volume back in at OUR band-split's
    price points displaced the action that was actually setting the real
    settlement price, turning an exact match into a real, confirmed price
    regression.

    compute_niv() below fixes the DISPLAY gap the acceptance-level version
    was chasing without that risk, precisely by NOT touching this function
    (or _arbitrage()/_niv_tag(), which compute_imbalance_price() also
    still calls unchanged) -- it's a separate, standalone calculation used
    only for the `total_delta` shown to the user, deliberately never fed
    back into pricing.
    """
    return [t for t in tags if t.vol >= DMAT]


def _arbitrage(offers: list[_Tag], bids: list[_Tag]) -> None:
    """Removes equal volumes from both sides wherever an offer's price is
    <= a bid's price, matched from each side's own cheapest end -- mutates
    `.vol` in place.
    """
    o = sorted(offers, key=lambda t: t.price)  # cheapest offer first (lowest price)
    b = sorted(bids, key=lambda t: -t.price)  # cheapest bid first (highest price)
    i = j = 0
    while i < len(o) and j < len(b) and o[i].vol > 0 and b[j].vol > 0 and o[i].price <= b[j].price:
        cut = min(o[i].vol, b[j].vol)
        o[i].vol -= cut
        b[j].vol -= cut
        if o[i].vol <= 0:
            i += 1
        if b[j].vol <= 0:
            j += 1


def _reclassify(tags: list[_Tag], is_offer: bool) -> None:
    entries = [(t.price, t.so_flag) for t in tags]
    effective = classify_prices(entries, is_offer)
    for t, so_flag in zip(tags, effective):
        t.so_flag = so_flag


def _niv_tag(offer_tags: list[_Tag], bid_tags: list[_Tag]) -> tuple[list[_Tag], bool]:
    """Returns (surviving tags of the larger side, is_offer of that side)
    -- see module docstring, stage 6. The survivors' total volume is this
    period's NIV. An empty result (no actions left in NIV -- vanishingly
    rare with real, continuously-valued volumes) is handled by the caller
    via the Market Price fallback, same as an empty Replacement Price.
    """
    offer_total = sum(t.vol for t in offer_tags)
    bid_total = sum(t.vol for t in bid_tags)
    if offer_total == bid_total:
        return [], True

    larger, is_offer, to_remove = (
        (offer_tags, True, bid_total) if offer_total > bid_total else (bid_tags, False, offer_total)
    )
    ordered = sorted(larger, key=lambda t: expense(t.price, is_offer), reverse=True)  # most expensive first
    survivors = []
    for t in ordered:
        if to_remove <= 0:
            survivors.append(t)
        elif t.vol <= to_remove:
            to_remove -= t.vol  # this action is fully NIV Tagged out
        else:
            t.vol -= to_remove
            to_remove = 0
            survivors.append(t)
    return survivors, is_offer


def _replacement_price(survivors: list[_Tag], is_offer: bool, market_index_price: float | None) -> None:
    for t in survivors:
        t.priced_at = t.price

    unflagged = [t for t in survivors if not t.so_flag]
    if not unflagged:
        # No Second-Stage-Unflagged survivor to draw an RPAR price from at
        # all -- e.g. a long period with no unflagged bids, or a short one
        # with no unflagged offers. The guide's own fallback is the Market
        # Price; if we don't have one for this period, leave the flagged
        # rows at their own original price rather than inventing a number.
        if market_index_price is not None:
            for t in survivors:
                t.priced_at = market_index_price
        return

    ordered = sorted(unflagged, key=lambda t: expense(t.price, is_offer), reverse=True)
    remaining, weighted_sum, vol_used = PAR, 0.0, 0.0
    for t in ordered:
        if remaining <= 0:
            break
        cut = min(t.vol, remaining)
        weighted_sum += cut * t.price
        vol_used += cut
        remaining -= cut
    if vol_used == 0:
        return
    replacement = weighted_sum / vol_used
    for t in survivors:
        if t.so_flag:
            t.priced_at = replacement


def _par_tag(survivors: list[_Tag], is_offer: bool) -> float:
    """Volume-weighted average of the most expensive PAR (1 MWh) of the
    surviving stack -- stage 8, the final energy imbalance price (before
    the BPA/SPA and TLM adjustments this module doesn't implement -- see
    module docstring). Also records each tag's own `par_vol` so callers
    can attribute a per-row contribution to the price.
    """
    ordered = sorted(survivors, key=lambda t: expense(t.priced_at, is_offer), reverse=True)
    remaining, weighted_sum, vol_used = PAR, 0.0, 0.0
    for t in ordered:
        if remaining <= 0:
            break
        cut = min(t.vol, remaining)
        t.par_vol = cut
        weighted_sum += cut * t.priced_at
        vol_used += cut
        remaining -= cut
    return weighted_sum / vol_used if vol_used > 0 else 0.0


def compute_imbalance_price(
    rows: list[dict], market_index_price: float | None = None
) -> tuple[float, dict[int, float]]:
    """`rows`: one settlement period's priced actions -- dicts with at
    least 'delta' (signed MWh, positive=offer/negative=bid), 'm_orig_price',
    and 'so_flag'. `market_index_price`: that period's Market Index Price
    (volume-weighted across MID providers -- see ingest/elexon_rest.py's
    blend_market_index), used as the guide's own Replacement Price fallback
    when a direction has no Second-Stage-Unflagged action to price from at
    all. Returns (energy_imbalance_price, {id(row):
    misik_imb_price_contribution}); rows are matched back by `id()`, so
    callers must keep the same dict objects alive between calling this and
    reading the returned mapping.
    """
    if not rows:
        return 0.0, {}

    offer_tags = [
        _Tag(ref=r, is_offer=True, price=float(r["m_orig_price"]), vol=float(r["delta"]), so_flag=bool(r["so_flag"]))
        for r in rows if float(r["delta"]) > 0
    ]
    bid_tags = [
        _Tag(ref=r, is_offer=False, price=float(r["m_orig_price"]), vol=abs(float(r["delta"])), so_flag=bool(r["so_flag"]))
        for r in rows if float(r["delta"]) < 0
    ]

    offer_tags = _de_minimis(offer_tags)
    bid_tags = _de_minimis(bid_tags)
    _arbitrage(offer_tags, bid_tags)
    offer_tags = [t for t in offer_tags if t.vol > 0]
    bid_tags = [t for t in bid_tags if t.vol > 0]

    _reclassify(offer_tags, is_offer=True)
    _reclassify(bid_tags, is_offer=False)

    survivors, is_offer = _niv_tag(offer_tags, bid_tags)
    if not survivors:
        # No actions left in NIV at all -- guide's own Market Price fallback.
        return (market_index_price if market_index_price is not None else 0.0), {}

    _replacement_price(survivors, is_offer, market_index_price)
    price = _par_tag(survivors, is_offer)

    contributions = {id(t.ref): t.par_vol * t.priced_at / PAR for t in survivors if t.par_vol > 0}
    return price, contributions


def compute_niv(rows: list[dict]) -> float:
    """This period's Net Imbalance Volume, for DISPLAY purposes only --
    deliberately independent from compute_imbalance_price()'s own pricing
    pipeline (does not call _de_minimis()/_arbitrage()/_niv_tag(), and
    mutates nothing those functions touch), so it's safe to compute this
    more accurately than the raw per-row sum without any risk of the kind
    of price regression _de_minimis()'s own docstring describes.

    An optional 'acceptance_key' per row groups BOD-band fragments of the
    SAME originating acceptance (see engine/stack.py's own caller) so De
    Minimis Tagging is checked against a whole acceptance's total volume,
    matching the guide's actual definition -- a row without one defaults
    to its own standalone group. Arbitrage Tagging is deliberately NOT
    replicated here: it always removes EQUAL volume from both sides, so
    it can only ever change which individual actions survive, never the
    net NIV (offer_total - bid_total) itself -- confirmed on real data
    that it doesn't fire at all for the periods checked, but the identity
    holds regardless.
    """
    if not rows:
        return 0.0
    offer_totals: dict[object, float] = {}
    bid_totals: dict[object, float] = {}
    for i, r in enumerate(rows):
        delta = float(r["delta"])
        key = r.get("acceptance_key", i)
        if delta > 0:
            offer_totals[key] = offer_totals.get(key, 0.0) + delta
        elif delta < 0:
            bid_totals[key] = bid_totals.get(key, 0.0) + abs(delta)
    offer_total = sum(v for v in offer_totals.values() if v >= DMAT)
    bid_total = sum(v for v in bid_totals.values() if v >= DMAT)
    return offer_total - bid_total
