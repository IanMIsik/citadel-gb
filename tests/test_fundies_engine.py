"""Unit tests for the ported Fundies engine (citadel/engine/fundies.py).

Small, hand-derived synthetic frames -- confirms the SP-broadcast helper,
the wind-curtailment estimate (with and without a working bid stack), the
SEMO IDA2-over-IDA1 priority merge, and the reference app-style derived-row
formulas (ported from the real Electron app's DeltaRowData/
MultipleSubtractionRowData -- see engine/fundies.py's own docstring)
against known arithmetic.
"""

from __future__ import annotations

import pandas as pd
import pytest

from citadel.engine.fundies import (
    build_derived_rows,
    build_windfor_frame,
    broadcast_hourly_to_half_hourly,
    interconnector_real_flows,
    natgrid_ng_vol,
    semo_net_flows,
    wind_outturn_with_curtailment,
)


def test_broadcast_hourly_to_half_hourly_duplicates_onto_the_pair():
    df = pd.DataFrame({"settlementPeriod": [1, 3, 5], "val": [10, 20, 30]})
    out = broadcast_hourly_to_half_hourly(df).sort_values("settlementPeriod").reset_index(drop=True)
    assert list(out["settlementPeriod"]) == [1, 2, 3, 4, 5, 6]
    assert list(out["val"]) == [10, 10, 20, 20, 30, 30]


def test_broadcast_hourly_to_half_hourly_empty_frame_passthrough():
    df = pd.DataFrame(columns=["settlementPeriod", "val"])
    assert broadcast_hourly_to_half_hourly(df).empty


def test_build_windfor_frame_derives_sp_from_start_time_and_broadcasts():
    records = [{"startTime": "2026-09-28T03:00:00Z", "publishTime": "2026-09-28T02:00:00Z", "generation": 500}]
    out = build_windfor_frame(records, "latestwindfor").sort_values("settlementPeriod").reset_index(drop=True)
    # 03:00 UTC = 04:00 BST -> SP9 (odd, hour start); broadcasts onto SP9+SP10.
    assert list(out["settlementPeriod"]) == [9, 10]
    assert list(out["latestwindfor"]) == [500, 500]


def test_build_windfor_frame_keeps_only_latest_publish_per_hour():
    records = [
        {"startTime": "2026-09-28T03:00:00Z", "publishTime": "2026-09-28T01:00:00Z", "generation": 400},
        {"startTime": "2026-09-28T03:00:00Z", "publishTime": "2026-09-28T02:00:00Z", "generation": 500},
    ]
    out = build_windfor_frame(records, "latestwindfor")
    assert (out["latestwindfor"] == 500).all()


def test_wind_curtailment_falls_back_to_metered_outturn_without_bid_stack():
    fuelhh = [{"settlementDate": "2026-09-28", "settlementPeriod": 10, "fuelType": "WIND", "generation": 300}]
    out = wind_outturn_with_curtailment(fuelhh, [], pd.DataFrame(columns=["bmUnit", "FT"]))
    assert out.iloc[0]["total_wind_outturn"] == 300


def test_wind_curtailment_adds_shut_bid_volume_for_wind_units():
    fuelhh = [{"settlementDate": "2026-09-28", "settlementPeriod": 10, "fuelType": "WIND", "generation": 300}]
    # ISPSTACK's own BM unit identifier column is `id`, not `bmUnit` --
    # confirmed against the real API response and the notebook's own code.
    bids = [
        {"settlementDate": "2026-09-28", "settlementPeriod": 10, "id": "T_WIND-1", "volume": -5},
        {"settlementDate": "2026-09-28", "settlementPeriod": 10, "id": "T_GAS-1", "volume": -100},
    ]
    fuel_ref = pd.DataFrame([
        {"bmUnit": "T_WIND-1", "nationalGridBmUnit": "WIND1", "FT": "WIND"},
        {"bmUnit": "T_GAS-1", "nationalGridBmUnit": "GAS1", "FT": "CCGT"},
    ])
    out = wind_outturn_with_curtailment(fuelhh, bids, fuel_ref)
    # Only the wind unit's bid counts: 300 + abs(-5)*2 = 310, not the gas unit's.
    assert out.iloc[0]["wind_ot"] == 300
    assert out.iloc[0]["shut_volume"] == 10
    assert out.iloc[0]["total_wind_outturn"] == 310


def test_interconnector_real_flows_maps_fuelhh_codes_onto_our_column_names():
    records = [
        {"settlementDate": "2026-09-28", "settlementPeriod": 10, "fuelType": "INTFR", "generation": 100},
        {"settlementDate": "2026-09-28", "settlementPeriod": 10, "fuelType": "INTIFA2", "generation": 50},
        {"settlementDate": "2026-09-28", "settlementPeriod": 10, "fuelType": "CCGT", "generation": 9999},
    ]
    out = interconnector_real_flows(records)
    row = out.iloc[0]
    assert row["real_ifa_net"] == 100
    assert row["real_ifa2_net"] == 50
    # Unmapped fuel types (CCGT) don't leak into any interconnector column.
    assert row["real_eleclink_net"] != 9999


def test_semo_net_flows_ida2_overrides_ida1():
    def _row(ni_gb, gb_ni):
        return {
            "StartTime": "2026-09-28T03:00:00Z",
            "TotalScheduled-NI-GB": ni_gb, "TotalScheduled-GB-NI": gb_ni,
            "TotalScheduled-IE-GB": 1, "TotalScheduled-GB-IE": 1,
            "TotalScheduled-IE2-GB2": 0, "TotalScheduled-GB2-IE2": 0,
        }
    ida1 = [_row(10, 2)]  # net = 8
    ida2 = [_row(20, 2)]  # net = 18
    out = semo_net_flows(ida1, ida2)
    assert out.iloc[0]["intmoyle_net"] == 18
    assert out.iloc[0]["intew_net"] == 0


def test_semo_net_flows_ida1_fills_gaps_ida2_doesnt_cover():
    ida1 = [{
        "StartTime": "2026-09-28T03:00:00Z",
        "TotalScheduled-NI-GB": 10, "TotalScheduled-GB-NI": 2,
        "TotalScheduled-IE-GB": 1, "TotalScheduled-GB-IE": 1,
        "TotalScheduled-IE2-GB2": 0, "TotalScheduled-GB2-IE2": 0,
    }]
    out = semo_net_flows(ida1, [])
    assert out.iloc[0]["intmoyle_net"] == 8


def test_build_derived_rows_matches_reference_formulas():
    sd = pd.Timestamp("2026-09-28").date()
    real_time = pd.DataFrame([{
        "settlementDate": sd, "settlementPeriod": 10,
        "indo": 1000, "latest_ndf": 1010, "total_wind_outturn": 200, "latestwindfor": 210,
    }])
    day_ahead = pd.DataFrame([{
        "settlementDate": sd, "settlementPeriod": 10,
        "da_ndf": 1005, "da_windfor": 205, "nuke_214": 400, "eleclink_net": 50,
    }])
    out = build_derived_rows(real_time, day_ahead).iloc[0]

    assert out["indo_da_ndf_delta"] == pytest.approx(1000 - 1005)
    assert out["fake_wind_ot_da_winfor_delta"] == pytest.approx(200 - 205)
    assert out["domestic_tight_delta"] == pytest.approx((1000 - 1005) - (200 - 205))
    assert out["interconnector_ng"] == pytest.approx(50)
    # demand - wind - interconnector_ng - nuclear
    assert out["latest_resid"] == pytest.approx(1000 - 200 - 50 - 400)


def test_build_derived_rows_falls_back_to_latest_ndf_when_indo_missing():
    sd = pd.Timestamp("2026-09-28").date()
    real_time = pd.DataFrame([{
        "settlementDate": sd, "settlementPeriod": 10,
        "indo": None, "latest_ndf": 1010, "total_wind_outturn": None, "latestwindfor": 210,
    }])
    day_ahead = pd.DataFrame([{"settlementDate": sd, "settlementPeriod": 10, "da_ndf": 1005, "da_windfor": 205, "nuke_214": 0}])
    out = build_derived_rows(real_time, day_ahead).iloc[0]
    # variable falls back to latest_ndf (1010) and latestwindfor (210) when the primary is missing.
    assert out["indo_da_ndf_delta"] == pytest.approx(1010 - 1005)
    assert out["fake_wind_ot_da_winfor_delta"] == pytest.approx(210 - 205)


def test_gtma_blocks_expand_to_per_sp_rows_in_the_old_feeds_unit():
    """A 300 MW 09:00-10:00 UTC block (2026-10-02, BST) covers SP21 and SP22
    and must come out as 150 MWh per SP -- Elexon's own SP21 buy
    adjustment -- so natgrid_ng_vol's `* 2` restores 300 MW.
    """
    from citadel.ingest.neso import gtma_blocks_to_sp_rows

    rows = gtma_blocks_to_sp_rows([{"StartTime": "2026-10-02T09:00:00", "EndTime": "2026-10-02T10:00:00", "Volume": 300.0}])
    assert [(r["Date"], r["SP"], r["Volume"]) for r in rows] == [("2026-10-02", 21, 150.0), ("2026-10-02", 22, 150.0)]
    out = natgrid_ng_vol(rows, []).sort_values("settlementPeriod")
    assert out["ng_vol"].tolist() == [300.0, 300.0]


def test_gtma_expansion_keeps_per_trade_detail_for_the_natgrid_ladder():
    from citadel.ingest.neso import gtma_blocks_to_sp_rows

    rows = gtma_blocks_to_sp_rows([{"ID": "ES1", "StartTime": "2026-10-02T09:00:00", "EndTime": "2026-10-02T09:30:00",
                                    "Volume": 300.0, "Price": 142.36, "SO_Flag": "T", "Reason": None}])
    assert len(rows) == 1
    assert rows[0]["MW"] == 300.0 and rows[0]["Price"] == 142.36 and rows[0]["Volume"] == 150.0  # MWh per SP unchanged


def test_natgrid_ladder_keeps_sells_negative_and_ladders_each_side_separately():
    from citadel.engine.natgrid import build_ladder

    neso = [
        {"Date": "2026-09-30", "SP": 30, "MW": 100.0, "Price": 150.0},
        {"Date": "2026-09-30", "SP": 30, "MW": -22.9, "Price": -92.69, "Reason": "B6_localised"},
        {"Date": "2026-09-30", "SP": 30, "MW": -10.0, "Price": -50.0},
        {"Date": "2026-09-30", "SP": 30, "MW": 50.0, "Price": 120.0},
    ]
    rows = build_ladder(neso, [])["2026-09-30"]["30"]
    buys = [r for r in rows if r["side"] == "buy"]
    sells = [r for r in rows if r["side"] == "sell"]
    assert [(r["price"], r["disc_cumm"]) for r in buys] == [(120.0, 50.0), (150.0, 150.0)]
    assert [(r["price"], r["disc_cumm"], r["volume_mw"]) for r in sells] == [(-92.69, -22.9, -22.9), (-50.0, -32.9, -10.0)]
    assert sells[0]["reason"] == "B6_localised"


def test_natgrid_store_normalises_detects_revisions_and_reconciles():
    from datetime import timezone

    from citadel.ingest.natgrid_store import block_changed, normalise_block, normalise_disbsad, reconcile

    raw = {"ID": "ES9", "StartTime": "2026-10-02T09:00:00", "EndTime": "2026-10-02T10:00:00", "Volume": 300.0,
           "Price": 142.36, "Cost": 42708.0, "SO_Flag": "T", "Reason": None, "Last_Updated": "2026-10-02T00:20:30.393000"}
    row = normalise_block(raw)
    assert row["id"] == "ES9" and row["start_time"].tzinfo == timezone.utc and row["volume_mw"] == 300.0
    assert normalise_block({"StartTime": "2026-10-02T09:00:00"}) is None  # no ID

    assert not block_changed(row, normalise_block({**raw, "Last_Updated": "2026-10-02T05:00:00"}))  # a re-fetch is not a revision
    assert block_changed(row, normalise_block({**raw, "Volume": 250.0}))
    assert block_changed(row, normalise_block({**raw, "Price": 150.0}))

    d = normalise_disbsad({"settlementDate": "2026-10-02", "settlementPeriod": 21, "id": 1, "cost": 21354.0, "volume": 150.0,
                           "soFlag": True, "storFlag": False, "partyId": "p", "assetId": "a", "service": "System"})
    assert d["price"] == 142.36 and d["action_id"] == 1

    neso = [{"Date": "2026-10-02", "SP": 21, "MW": 300.0, "Price": 142.36},
            {"Date": "2026-10-02", "SP": 22, "MW": 300.0, "Price": 142.0},
            {"Date": "2026-10-02", "SP": 23, "MW": 100.0, "Price": 150.0}]
    disbsad = [{"settlementDate": "2026-10-02", "settlementPeriod": 21, "volume": 150.0, "cost": 21354.0},  # agrees (300 MW)
               {"settlementDate": "2026-10-02", "settlementPeriod": 22, "volume": 100.0, "cost": 14200.0},  # 200 MW: differs
               {"settlementDate": "2026-10-02", "settlementPeriod": 24, "volume": 10.0, "cost": 1000.0}]    # NESO has nothing
    status = {p["sp"]: p["status"] for p in reconcile(neso, disbsad)}
    assert status == {21: "match", 22: "mismatch", 23: "neso_only", 24: "disbsad_only"}


# 2 Oct 2026 is BST: SP21 is 10:00-10:30 London (ended by 12:00Z), SP30 is 14:30-15:00 London (still to come).
def _now_noon_utc():
    from datetime import datetime, timezone

    return datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def _disb(sp, volume_mwh, price, date="2026-10-02"):
    return {"settlementDate": date, "settlementPeriod": sp, "volume": volume_mwh, "cost": volume_mwh * price, "soFlag": True, "service": "Energy"}


def test_resolve_past_periods_use_disbsad_and_keep_its_extra_actions():
    from citadel.engine.natgrid import resolve_trades, total_mw_by_period

    neso = [{"Date": "2026-10-02", "SP": 21, "MW": 300.0, "Price": 142.36, "Source": "GTMA"}]
    disbsad = [_disb(21, 150.0, 142.36), _disb(21, 0.25, 210.0)]          # the NESO trade, plus a small extra NESO lacks
    rows = resolve_trades(neso, disbsad, _now_noon_utc())
    assert {r["source"] for r in rows} == {"DISBSAD"} and len(rows) == 2
    assert total_mw_by_period(neso, disbsad, _now_noon_utc()) == {("2026-10-02", 21): 300.5}


def test_resolve_past_period_with_no_disbsad_yet_falls_back_to_neso():
    from citadel.engine.natgrid import total_mw_by_period

    neso = [{"Date": "2026-10-02", "SP": 21, "MW": 300.0, "Price": 142.36}]
    assert total_mw_by_period(neso, [], _now_noon_utc()) == {("2026-10-02", 21): 300.0}


def test_resolve_future_periods_use_neso_and_add_only_the_disbsad_extras():
    from citadel.engine.natgrid import resolve_trades, total_mw_by_period

    neso = [{"Date": "2026-10-02", "SP": 30, "MW": 300.0, "Price": 142.36, "Source": "GTMA"},
            {"Date": "2026-10-02", "SP": 30, "MW": 100.0, "Price": 150.0, "Source": "GTMA"}]
    # DISBSAD already lists the 300 MW trade (counted once, not twice) and one action NESO has not got.
    disbsad = [_disb(30, 150.0, 142.36), _disb(30, 4.0, 200.0)]
    rows = resolve_trades(neso, disbsad, _now_noon_utc())
    assert sorted((r["source"], round(r["volume_mw"], 1)) for r in rows) == [("DISBSAD", 8.0), ("GTMA", 100.0), ("GTMA", 300.0)]
    assert total_mw_by_period(neso, disbsad, _now_noon_utc()) == {("2026-10-02", 30): 408.0}

    # a future period DISBSAD alone knows about is kept as it stands
    assert total_mw_by_period([], [_disb(31, 50.0, 180.0)], _now_noon_utc()) == {("2026-10-02", 31): 100.0}


def test_natgrid_rows_for_fundies_and_the_ladder_follow_the_same_rule():
    from citadel.engine.natgrid import build_ladder

    neso = [{"Date": "2026-10-02", "SP": 21, "MW": 100.0, "Price": 200.0, "SO_Flag": "T", "Source": "GTMA"},
            {"Date": "2026-10-02", "SP": 21, "MW": 300.0, "Price": 142.36, "SO_Flag": "T", "Source": "GTMA"},
            {"Date": "2026-10-02", "SP": 30, "MW": 50.0, "Price": 150.0, "Source": "GTMA"}]
    disbsad = [_disb(21, 150.0, 142.36), _disb(21, 50.0, 200.0), _disb(21, 1.0, 300.0)]   # SP21: past, DISBSAD is the record
    now = _now_noon_utc()

    ladder = build_ladder(neso, disbsad, now)["2026-10-02"]
    assert [(r["price"], r["disc_cumm"], r["source"]) for r in ladder["21"]] == [(142.36, 300.0, "DISBSAD"), (200.0, 400.0, "DISBSAD"), (300.0, 402.0, "DISBSAD")]
    assert [(r["price"], r["source"]) for r in ladder["30"]] == [(150.0, "GTMA")]            # SP30: future, NESO

    out = natgrid_ng_vol(neso, disbsad, now).sort_values("settlementPeriod").reset_index(drop=True)
    assert out["ng_vol"].tolist() == [402.0, 50.0]


def test_upcoming_trades_become_blocks_that_keep_their_id_and_convert_mwh_to_mw():
    from citadel.ingest.neso import gtma_blocks_to_sp_rows, upcoming_rows_to_blocks
    from citadel.ingest.natgrid_store import normalise_block

    # one trade over SP31-32 (the shape of NESO's real upcoming list): per-period rows, same ID, Volume in MWh
    rows = [{"ID": "ES1", "Date": "2026-10-05", "SP": 31, "Volume": 75.0, "Price": 107.23, "Cost": 8042.25, "SO_Flag": "F", "Reason": "MARGIN"},
            {"ID": "ES1", "Date": "2026-10-05", "SP": 32, "Volume": 75.0, "Price": 107.23, "Cost": 8042.25, "SO_Flag": "F", "Reason": "MARGIN"}]
    blocks = upcoming_rows_to_blocks(rows)
    assert len(blocks) == 1 and blocks[0]["ID"] == "ES1" and blocks[0]["Volume"] == 150.0          # 75 MWh per period = 150 MW
    assert blocks[0]["StartTime"] == "2026-10-05T14:00:00" and blocks[0]["EndTime"] == "2026-10-05T15:00:00"   # SP31-32 on a BST day
    assert blocks[0]["Cost"] == 16084.5 and normalise_block(blocks[0]) is not None

    back = gtma_blocks_to_sp_rows(blocks)                      # expands to the periods it covers, MW intact
    assert [(r["SP"], r["MW"], r["Price"], r["Source"]) for r in back] == [(31, 150.0, 107.23, "NESO"), (32, 150.0, 107.23, "NESO")]

    # periods of different sizes, or with a gap, cannot be one block: kept one block per period
    odd = [{"ID": "ES2", "Date": "2026-10-05", "SP": 33, "Volume": 10.0, "Price": 100.0}, {"ID": "ES2", "Date": "2026-10-05", "SP": 34, "Volume": 12.0, "Price": 100.0}]
    assert sorted(b["ID"] for b in upcoming_rows_to_blocks(odd)) == ["ES2@2026-10-05-SP33", "ES2@2026-10-05-SP34"]
    gap = [{"ID": "ES3", "Date": "2026-10-05", "SP": 33, "Volume": 10.0, "Price": 100.0}, {"ID": "ES3", "Date": "2026-10-05", "SP": 35, "Volume": 10.0, "Price": 100.0}]
    assert len(upcoming_rows_to_blocks(gap)) == 2
    assert upcoming_rows_to_blocks([{"ID": "ES4", "Date": "2026-10-05", "SP": 36}]) == []               # no volume or price: dropped
