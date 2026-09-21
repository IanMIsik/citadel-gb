"""Unit tests for engine/imbalance_price.py, pinned against Elexon's own
worked example in the Imbalance Pricing Guidance Note (v15.0, section 4)
wherever possible, plus targeted cases for De Minimis and Arbitrage Tagging
that the worked example doesn't cover in isolation.
"""

from __future__ import annotations

import pytest

from citadel.engine.imbalance_price import compute_imbalance_price, compute_niv


def _row(bm_unit: str, delta: float, price: float, so_flag: bool = False, acceptance_key: object = None) -> dict:
    row = {"bm_unit": bm_unit, "delta": delta, "m_orig_price": price, "so_flag": so_flag}
    if acceptance_key is not None:
        row["acceptance_key"] = acceptance_key
    return row


def test_niv_tagging_nets_smaller_side_off_top_of_larger_one():
    """A direct check of stage 6 against the guide's own arithmetic
    (section 4): a 285 MWh Buy stack (offers) against a 60 MWh Sell stack
    (bids) leaves a 225 MWh NIV, built by removing the 60 MWh from the
    *top* (most expensive end) of the Buy stack -- the 40 MWh @ £300 offer
    entirely, then 20 MWh of the next-most-expensive (150 @ £150). Kept
    deliberately simple (no flagged actions) to isolate NIV Tagging alone,
    since reconstructing the guide's own worked Classification diagram from
    its lossy PDF text extraction (colour-coded flags don't survive text
    extraction) wasn't reliable enough to pin a test to.
    """
    rows = [
        _row("offer_120", 30, 120.0),
        _row("offer_100", 5, 100.0),
        _row("offer_300", 40, 300.0),
        _row("offer_20", 100, 20.0),
        _row("offer_40", 10, 40.0),
        _row("vgb_30", 50, 30.0),
        _row("bsaa_buy_150", 150, 150.0),
        _row("bsaa_sell_4", -10, 4.0),
        _row("rrauss_0", -30, 0.0),
        _row("bid_neg30", -20, -30.0),
    ]
    # Buy stack total: 30+5+40+100+10+50+150 = 385. Sell stack: 10+30+20 = 60.
    # NIV Tagging removes 60 from the top: the 40@300 entirely, then 20 of
    # the 150@150 -> surviving Buy stack = 385-60 = 325 MWh, and the
    # £150 action's surviving volume is 150-20=130 MWh, still the single
    # most expensive survivor, so it alone sets the PAR-Tagged price.
    price, contributions = compute_imbalance_price(rows)
    assert price == pytest.approx(150.0, abs=0.01)


def test_de_minimis_removes_tiny_actions_entirely():
    real, tiny = _row("real", 10, 50.0), _row("tiny", 0.01, 999.0)
    price, contributions = compute_imbalance_price([real, tiny])
    assert id(tiny) not in contributions
    # The tiny action must not affect the price at all (it's fully excluded).
    price_without_tiny, _ = compute_imbalance_price([_row("real", 10, 50.0)])
    assert price == pytest.approx(price_without_tiny)


def test_arbitrage_cancels_equal_crossed_volume():
    # A £10/MWh offer and a £15/MWh bid are crossed (offer price <= bid
    # price) -- the guide's own arbitrage example. Equal volume (30 MWh)
    # should be removed from both, leaving nothing to price.
    rows = [_row("offer", 30, 10.0), _row("bid", -30, 15.0)]
    price, contributions = compute_imbalance_price(rows)
    assert price == pytest.approx(0.0)
    assert contributions == {}


def test_flagged_action_more_expensive_than_unflagged_gets_repriced():
    rows = [
        _row("unflagged_cheap", 10, 50.0),
        _row("flagged_expensive", 5, 200.0, so_flag=True),  # more expensive than unflagged -> Second Stage Flagged
    ]
    price, contributions = compute_imbalance_price(rows)
    # No bids at all -> the full 15 MWh offer stack survives NIV Tagging
    # untouched. The flagged row is repriced to the unflagged survivors'
    # RPAR price (£50, the only unflagged price available) before PAR
    # Tagging draws its final 1 MWh -- so the result must be £50, not the
    # flagged row's own £200.
    assert price == pytest.approx(50.0)


def test_no_actions_returns_zero_price():
    price, contributions = compute_imbalance_price([])
    assert price == 0.0
    assert contributions == {}


def test_long_period_with_no_unflagged_bids_cashes_out_at_market_index_price():
    # System long (bids are the relevant NIV direction) but every bid is
    # flagged -- no Second-Stage-Unflagged bid to draw an RPAR price from,
    # so the guide's Market Price fallback applies (confirmed with the user
    # this is the real case worth handling, not a rare textbook edge case).
    rows = [
        _row("offer", 5, 100.0),
        _row("flagged_bid_1", -20, -50.0, so_flag=True),
        _row("flagged_bid_2", -10, -80.0, so_flag=True),
    ]
    price, _ = compute_imbalance_price(rows, market_index_price=42.5)
    assert price == pytest.approx(42.5)


def test_short_period_with_no_unflagged_offers_cashes_out_at_market_index_price():
    rows = [
        _row("bid", -5, -30.0),
        _row("flagged_offer_1", 20, 150.0, so_flag=True),
        _row("flagged_offer_2", 10, 200.0, so_flag=True),
    ]
    price, _ = compute_imbalance_price(rows, market_index_price=37.0)
    assert price == pytest.approx(37.0)


def test_without_market_index_price_falls_back_to_original_price():
    # No market_index_price supplied at all (e.g. MID ingest hasn't run
    # yet) -- must not raise, and must fall back to leaving flagged rows at
    # their own price rather than inventing a number.
    rows = [
        _row("offer", 5, 100.0),
        _row("flagged_bid", -20, -50.0, so_flag=True),
    ]
    price, _ = compute_imbalance_price(rows)
    assert price == pytest.approx(-50.0)


def test_compute_niv_is_the_raw_net_of_offers_minus_bids():
    rows = [_row("offer", 10, 50.0), _row("offer", 5, 40.0), _row("bid", -8, -30.0)]
    assert compute_niv(rows) == pytest.approx(10 + 5 - 8)


def test_compute_niv_de_minimis_checks_a_whole_acceptance_not_its_band_fragments():
    # Same acceptance (acceptance_key=1) split across two BOD-band
    # fragments by engine/stack.py's own six-case allocation -- each
    # fragment is individually under DMAT (0.1 MWh), but the acceptance's
    # own total (0.15) is not, so neither fragment should be dropped.
    # A second, genuinely tiny acceptance (key=2, total 0.05) should be.
    rows = [
        _row("unit_1", -0.06, -20.0, acceptance_key=1),
        _row("unit_1", -0.09, -25.0, acceptance_key=1),
        _row("unit_2", -0.05, -10.0, acceptance_key=2),
        _row("offer", 10, 50.0),
    ]
    assert compute_niv(rows) == pytest.approx(10 - 0.15)


def test_compute_niv_never_affects_the_priced_result():
    # compute_niv() must be read-only with respect to pricing -- calling
    # it (in either order, any number of times) must never change what
    # compute_imbalance_price() returns for the same rows.
    rows = [
        _row("unit_1", -0.06, -20.0, acceptance_key=1),
        _row("unit_1", -0.09, -25.0, acceptance_key=1),
        _row("offer", 10, 50.0),
    ]
    price_before, contributions_before = compute_imbalance_price(rows)
    compute_niv(rows)
    price_after, contributions_after = compute_imbalance_price(rows)
    assert price_before == pytest.approx(price_after)
    assert contributions_before.keys() == contributions_after.keys()
