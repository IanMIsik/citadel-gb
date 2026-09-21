"""Shared primitive for Elexon's Classification stage (Imbalance Pricing
Guidance Note v15.0): within one direction (all offers, or all bids), an
SO-Flagged action cheaper than the most expensive *unflagged* action in
that same direction is reclassified "Second Stage Unflagged" and behaves
identically to a genuine unflagged action from then on; one still more
expensive than that threshold stays "Second Stage Flagged". If a direction
has no unflagged actions at all, every action in it stays flagged.

Used by both engine/imbalance_price.py (the actual price calculation) and
engine/view.py (the live page's display grouping) so the two can never
disagree about which actions count as flagged.
"""

from __future__ import annotations


def expense(price: float, is_offer: bool) -> float:
    """A single comparable "how expensive is this" key that works the same
    way for both directions -- higher is always more expensive. For
    offers, a higher price is more expensive (ordinary). For bids, a
    *lower* price is more expensive (confirmed directly by the user and by
    the guide's own wording: "the lower the price the more expensive" a
    sell/bid action is) -- negating the price here makes "higher expense
    value = more expensive" hold in both cases, so the rest of the pricing
    pipeline never has to branch on direction again.
    """
    return price if is_offer else -price


def classify_prices(entries: list[tuple[float, bool]], is_offer: bool) -> list[bool]:
    """`entries`: [(price, so_flag), ...] for one direction, same order as
    the caller's own rows. Returns the *effective* flagged state per entry
    (same order) after Classification.
    """
    unflagged_prices = [price for price, so_flag in entries if not so_flag]
    if not unflagged_prices:
        return [so_flag for _price, so_flag in entries]

    threshold = max(expense(price, is_offer) for price in unflagged_prices)
    return [
        False if not so_flag else expense(price, is_offer) >= threshold
        for price, so_flag in entries
    ]
