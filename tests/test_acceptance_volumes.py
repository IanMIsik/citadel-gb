from datetime import datetime, timezone

from citadel.engine.acceptance_volumes import acceptance_volumes, compare, elexon_stack_volumes

SP = datetime(2026, 10, 8, 10, 0, tzinfo=timezone.utc)
PN = [{"bmUnit": "T_X-1", "timeFrom": "2026-10-08T10:00:00Z", "timeTo": "2026-10-08T10:30:00Z", "levelFrom": 10, "levelTo": 10}]


def _acc(n, accepted, t0, t1, l0, l1, unit="T_X-1"):
    return {"bmUnit": unit, "acceptanceNumber": n, "acceptanceTime": f"2026-10-08T{accepted}:00Z",
            "timeFrom": f"2026-10-08T{t0}:00Z", "timeTo": f"2026-10-08T{t1}:00Z", "levelFrom": l0, "levelTo": l1}


def test_each_acceptance_is_measured_against_the_previous_one_not_the_plan():
    boalf = [
        _acc(1, "09:59", "10:00", "10:10", -40, -40),   # first: baseline is the FPN (10) -> 10 min x -50 MW
        _acc(2, "10:02", "10:03", "10:06", -50, -50),   # baseline is #1 (-40) -> 3 min x -10 MW
        _acc(3, "10:04", "10:05", "10:06", -40, 0),     # ramp averaging -20 vs #2's -50 -> +30 MW for 1 min
    ]
    out = acceptance_volumes(boalf, PN, SP)
    assert out[("T_X-1", 1)] == [0.0, -8.333333]
    assert out[("T_X-1", 2)] == [0.0, -0.5]
    assert out[("T_X-1", 3)] == [0.5, 0.0]          # an offer, although the unit is still far below plan


def test_a_later_acceptance_ending_hands_the_baseline_back():
    boalf = [_acc(1, "09:59", "10:00", "10:10", -40, -40), _acc(2, "10:02", "10:03", "10:05", -50, -50)]
    out = acceptance_volumes(boalf, PN, SP)
    assert out[("T_X-1", 2)] == [0.0, -0.333333]    # 2 minutes at -10 against #1
    assert out[("T_X-1", 1)][1] == -8.333333        # #1 is always priced against the plan, whatever follows it


def test_compare_reports_matches_and_the_worst_mismatch():
    mine = {("A", 1): [0.5, 0.0], ("A", 2): [0.0, -1.0]}
    theirs = elexon_stack_volumes(
        [{"id": "A", "acceptanceId": 1, "volume": 0.5}], [{"id": "A", "acceptanceId": 2, "volume": -1.2}])
    res = compare(mine, theirs)
    assert res["pairs"] == 2 and res["matching"] == 1 and res["wrong"][0][1] == ("A", 2)


def _bod(pair, width, bid, offer, unit="T_X-1"):
    return {"bmUnit": unit, "pairId": pair, "levelTo": width, "bid": bid, "offer": offer}


BOD = [_bod(-1, -10, 92, 170), _bod(-2, -20, 92, 170), _bod(-3, -20, 80, 171), _bod(-4, -100, 60, 172),
       _bod(1, 10, 92, 170), _bod(2, 20, 92, 200)]


def test_pair_bands_stack_outwards_from_the_plan():
    from citadel.engine.acceptance_volumes import pair_bands

    bands = {b[0]: b[1:3] for b in pair_bands(BOD)["T_X-1"]["bands"]}
    assert bands[-1] == (-10, 0) and bands[-2] == (-30, -10) and bands[-3] == (-50, -30) and bands[-4] == (-150, -50)
    assert bands[1] == (0, 10) and bands[2] == (10, 30)


def test_band_split_prices_each_layer_of_a_ramp_at_its_own_pair():
    from citadel.engine.acceptance_volumes import acceptance_bands

    # Baseline -40 (50 below the +10 plan); then a 1-minute ramp to 0 (10 below plan). The area above the
    # baseline is 0.333 MWh: the first half of the ramp sits in band -3, the second half reaches band -2.
    boalf = [_acc(1, "09:59", "10:00", "10:20", -40, -40), _acc(2, "10:05", "10:10", "10:11", -40, 0)]
    rows = {(r["acceptance_number"], r["pair_id"], r["side"]): r for r in acceptance_bands(boalf, PN, BOD, SP)}
    ramp = {k: v for k, v in rows.items() if k[0] == 2}
    assert set(ramp) == {(2, -3, "offer"), (2, -2, "offer")}
    assert ramp[(2, -3, "offer")]["volume"] == 0.25 and ramp[(2, -3, "offer")]["price"] == 171
    assert ramp[(2, -2, "offer")]["volume"] == 0.083333 and ramp[(2, -2, "offer")]["price"] == 170
    # the first acceptance is measured against the plan: 20 minutes x 50 MW below it, across bands -1..-3 and -4
    first = sum(v["volume"] for k, v in rows.items() if k[0] == 1)
    assert round(first, 4) == round(-50 * 20 / 60, 4) and all(k[2] == "bid" for k in rows if k[0] == 1)


def test_price_from_bands_applies_de_minimis_per_whole_acceptance():
    from citadel.engine.acceptance_volumes import price_from_bands

    def row(unit, acc, pair, side, vol, price, so=False):
        return {"bm_unit": unit, "acceptance_number": acc, "pair_id": pair, "side": side, "volume": vol,
                "price": price, "so_flag": so, "stor_flag": False}

    # An acceptance split over two pairs is 0.12 MWh in total: kept as a whole, though each row alone is < 0.1.
    kept = [row("A", 1, -1, "offer", 5.0, 100.0), row("B", 2, -1, "offer", 0.06, 300.0), row("B", 2, -2, "offer", 0.06, 300.0)]
    dropped = [row("A", 1, -1, "offer", 5.0, 100.0), row("B", 2, -1, "offer", 0.05, 300.0), row("B", 2, -2, "offer", 0.04, 300.0)]
    assert price_from_bands(kept, 50.0) > price_from_bands(dropped, 50.0)


def test_stack_rows_are_cached_per_period_and_recomputed_when_an_input_changes():
    import pandas as pd

    from citadel.engine import acceptance_volumes as av

    boalf = pd.DataFrame([{**_acc(1, "09:59", "10:00", "10:10", -40, -40), "soFlag": False, "storFlag": False}])
    pn = pd.DataFrame(PN)
    bod = pd.DataFrame([{**b, "settlementDate": "2026-10-08", "settlementPeriod": 23} for b in BOD])
    flags = pd.DataFrame([{"bmUnit": "T_X-1", "acceptanceNumber": 1, "soFlag": False, "storFlag": False,
                           "deemedBoFlag": False, "cadlFlag": False}])
    periods = [(datetime(2026, 10, 8).date(), 23)]
    av._PERIOD_CACHE.clear()
    first = av.acceptance_stack_rows(boalf, pn, bod, flags, periods, pd.Timestamp("2026-10-08T10:00:00Z"))
    assert len(av._PERIOD_CACHE) == 1 and not first.empty
    cached = av._PERIOD_CACHE[(datetime(2026, 10, 8).date(), 23)][1]
    again = av.acceptance_stack_rows(boalf, pn, bod, flags, periods, pd.Timestamp("2026-10-08T10:05:00Z"))
    assert av._PERIOD_CACHE[(datetime(2026, 10, 8).date(), 23)][1] is cached        # served from the cache
    assert list(again["max_ta"].unique()) == [pd.Timestamp("2026-10-08T10:05:00Z")]  # but stamped afresh
    changed = boalf.assign(levelTo=-30.0, levelFrom=-30.0)
    av.acceptance_stack_rows(changed, pn, bod, flags, periods, pd.Timestamp("2026-10-08T10:05:00Z"))
    assert av._PERIOD_CACHE[(datetime(2026, 10, 8).date(), 23)][1] is not cached    # an input moved: recomputed
