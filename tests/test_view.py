"""Unit tests for engine/view.py's Classification-based grouping -- pinned
directly against Elexon's own Imbalance Pricing Guidance Note (v15.0): the
guide's "Ranked Sets" section describes a single price-ordered ranked set
per direction, with Classification only changing a flagged action's
*pricing* treatment (own price vs Replacement Price), never its rank
position. So a First-Stage-Flagged action cheaper than the most expensive
unflagged action in its own direction is reclassified "Second Stage
Unflagged" for pricing purposes and effective_flagged=False, but it stays
displayed under the flagged (so_flagged) group -- its raw SO flag decides
which group it's shown in. Cumulative MWh, however, is computed from one
combined price-ranked walk across both groups, so that cheap flagged
action's volume is still counted at its own true (early) rank. Also covers
the duplicate-row merge.
"""

from __future__ import annotations

from citadel.engine.view import split_and_sort


def _row(bm_unit: str, delta: float, price: float, so_flag: bool, total_delta: float = 50.0) -> dict:
    return {
        "bm_unit": bm_unit, "delta": delta, "m_orig_price": price, "so_flag": so_flag,
        "stor_flag": False, "deemed_bo_flag": False, "acceptance_number": 1,
        "misik_imb_price": 0.0, "reversal": 1.0, "total_misik_price": 100.0, "total_delta": total_delta,
    }


def _units(rows: list[dict]) -> set[str]:
    return {r["bm_unit"] for r in rows}


def _by_unit(rows: list[dict]) -> dict[str, dict]:
    return {r["bm_unit"]: r for r in rows}


def test_cheaper_flagged_offer_stays_in_flagged_group_but_counts_early():
    rows = [
        _row("cheap_flagged", 10, 20.0, so_flag=True),   # cheaper than the priciest unflagged (30) -> reclassified for pricing
        _row("expensive_flagged", 10, 40.0, so_flag=True),  # more expensive -> stays flagged
        _row("unflagged", 10, 30.0, so_flag=False),
    ]
    view = split_and_sort(rows)
    # Raw SO flag decides the display group -- reclassification never
    # moves a row out of the flagged section (see module docstring).
    assert _units(view["unflagged"]) == {"unflagged"}
    assert _units(view["so_flagged"]) == {"cheap_flagged", "expensive_flagged"}

    so_flagged = _by_unit(view["so_flagged"])
    assert so_flagged["cheap_flagged"]["effective_flagged"] is False
    assert so_flagged["expensive_flagged"]["effective_flagged"] is True

    # Combined ranked set, cheapest first: cheap_flagged (£20, 10MWh) ->
    # unflagged (£30, 10MWh) -> expensive_flagged (£40, 10MWh).
    assert so_flagged["cheap_flagged"]["cumulative_mwh"] == 10
    assert _by_unit(view["unflagged"])["unflagged"]["cumulative_mwh"] == 20
    assert so_flagged["expensive_flagged"]["cumulative_mwh"] == 30


def test_cheaper_flagged_bid_stays_in_flagged_group_but_counts_early():
    # For bids, a HIGHER (more positive) price is cheaper -- confirmed by
    # the user and by the guide's own wording.
    rows = [
        _row("cheap_flagged_bid", -10, -20.0, so_flag=True),   # -20 is cheaper than -50 (higher price) -> reclassified for pricing
        _row("expensive_flagged_bid", -10, -80.0, so_flag=True),  # more expensive (lower price) -> stays flagged
        _row("unflagged_bid", -10, -50.0, so_flag=False),
    ]
    view = split_and_sort(rows)
    assert _units(view["unflagged"]) == {"unflagged_bid"}
    assert _units(view["so_flagged"]) == {"cheap_flagged_bid", "expensive_flagged_bid"}

    so_flagged = _by_unit(view["so_flagged"])
    assert so_flagged["cheap_flagged_bid"]["effective_flagged"] is False
    assert so_flagged["expensive_flagged_bid"]["effective_flagged"] is True

    # Combined ranked set, cheapest first: cheap_flagged_bid (£-20, 10MWh)
    # -> unflagged_bid (£-50, 10MWh) -> expensive_flagged_bid (£-80, 10MWh).
    assert so_flagged["cheap_flagged_bid"]["cumulative_mwh"] == 10
    assert _by_unit(view["unflagged"])["unflagged_bid"]["cumulative_mwh"] == 20
    assert so_flagged["expensive_flagged_bid"]["cumulative_mwh"] == 30


def test_no_unflagged_actions_keeps_everything_flagged():
    rows = [_row("a", 10, 20.0, so_flag=True), _row("b", 10, 40.0, so_flag=True)]
    view = split_and_sort(rows)
    assert view["unflagged"] == []
    assert _units(view["so_flagged"]) == {"a", "b"}


def test_cumulative_mwh_accumulates_cheapest_first():
    rows = [
        _row("offer_a", 10, 50.0, so_flag=False, total_delta=1000.0),  # more expensive offer
        _row("offer_b", 5, 40.0, so_flag=False, total_delta=1000.0),   # cheaper -> accumulates FIRST
        _row("bid_a", -3, 10.0, so_flag=False, total_delta=1000.0),    # cheaper bid (higher price) -> FIRST
        _row("bid_b", -7, 5.0, so_flag=False, total_delta=1000.0),     # more expensive bid
    ]
    view = split_and_sort(rows)
    by_unit = {r["bm_unit"]: r["cumulative_mwh"] for r in view["unflagged"]}
    # Offers: cheapest (offer_b, £40) accumulates first, most expensive (offer_a, £50) last.
    assert by_unit["offer_b"] == 5
    assert by_unit["offer_a"] == 15
    # Bids: cheapest (bid_a, higher price £10) accumulates first, most expensive (bid_b, £5) last.
    assert by_unit["bid_a"] == 3
    assert by_unit["bid_b"] == 10


def test_same_price_rows_break_ties_by_cumulative_mwh_descending():
    # offer_a and offer_b share the exact same price (£50, different
    # bm_units) so sorting by price alone leaves their relative order
    # arbitrary. offer_c is cheaper and accumulates first, making
    # offer_a's cumulative (7) and offer_b's (10) diverge based on which
    # one is summed second in the combined ranked walk -- offer_b should
    # then display FIRST within the £50 tie, since it has the larger
    # cumulative MWh.
    rows = [
        _row("offer_a", 5, 50.0, so_flag=False, total_delta=1000.0),
        _row("offer_b", 3, 50.0, so_flag=False, total_delta=1000.0),
        _row("offer_c", 2, 40.0, so_flag=False, total_delta=1000.0),
    ]
    view = split_and_sort(rows)
    order = [r["bm_unit"] for r in view["unflagged"]]
    assert order == ["offer_b", "offer_a", "offer_c"]
    by_unit = _by_unit(view["unflagged"])
    assert by_unit["offer_c"]["cumulative_mwh"] == 2
    assert by_unit["offer_a"]["cumulative_mwh"] == 7
    assert by_unit["offer_b"]["cumulative_mwh"] == 10


def test_same_price_bids_break_ties_by_cumulative_mwh_ascending():
    # Bids' display order already matches accumulation order (cheapest --
    # i.e. highest raw price -- first), so cumulative MWh increases
    # top-to-bottom, the opposite trend from offers. bid_a and bid_b tie
    # at the same price (£-50); bid_c is cheaper (£-40) and accumulates
    # first. Within the tie, bid_a (cumulative 7) must display BEFORE
    # bid_b (cumulative 10) to continue that increasing trend -- sorting
    # ties descending (like offers) would dip the column backwards here.
    rows = [
        _row("bid_a", -5, -50.0, so_flag=False, total_delta=1000.0),
        _row("bid_b", -3, -50.0, so_flag=False, total_delta=1000.0),
        _row("bid_c", -2, -40.0, so_flag=False, total_delta=1000.0),
    ]
    view = split_and_sort(rows)
    order = [r["bm_unit"] for r in view["unflagged"]]
    assert order == ["bid_c", "bid_a", "bid_b"]
    by_unit = _by_unit(view["unflagged"])
    assert by_unit["bid_c"]["cumulative_mwh"] == 2
    assert by_unit["bid_a"]["cumulative_mwh"] == 7
    assert by_unit["bid_b"]["cumulative_mwh"] == 10


def test_duplicate_bm_unit_and_price_rows_are_summed():
    rows = [
        _row("T_TEST-1_1", 10, 50.0, so_flag=False),
        _row("T_TEST-1_1", 8, 50.0, so_flag=False),  # same bm_unit + price -> merged with the row above
        _row("T_OTHER-1_1", 3, 50.0, so_flag=False),  # same price, different unit -> stays separate
    ]
    view = split_and_sort(rows)
    action_rows = view["unflagged"]
    assert len(action_rows) == 2
    by_unit = {r["bm_unit"]: r["delta_mwh"] for r in action_rows}
    assert by_unit["T_TEST-1_1"] == 18
    assert by_unit["T_OTHER-1_1"] == 3
