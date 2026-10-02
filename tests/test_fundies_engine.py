"""Unit tests for the ported Fundies engine (citadel/engine/fundies.py).

Small, hand-derived synthetic frames -- confirms the SP-broadcast helper,
the wind-curtailment estimate (with and without a working bid stack), the
SEMO IDA2-over-IDA1 priority merge, and the Zapdos-style derived-row
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


def test_natgrid_ng_vol_prefers_neso_and_falls_back_to_disbsad():
    neso_trades = [{"Date": "2026-09-28", "SP": 10, "Volume": 5}]
    disbsad = [
        {"settlementDate": "2026-09-28", "settlementPeriod": 10, "volume": 999},  # covered by NESO -> ignored
        {"settlementDate": "2026-09-28", "settlementPeriod": 11, "volume": 3},  # not covered -> used
    ]
    out = natgrid_ng_vol(neso_trades, disbsad).sort_values("settlementPeriod").reset_index(drop=True)
    assert out.iloc[0]["settlementPeriod"] == 10
    assert out.iloc[0]["ng_vol"] == 10  # 5 * 2, NESO wins over DISBSAD's 999
    assert out.iloc[1]["settlementPeriod"] == 11
    assert out.iloc[1]["ng_vol"] == 6  # 3 * 2, DISBSAD fallback


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


def test_build_derived_rows_matches_zapdos_formulas():
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
