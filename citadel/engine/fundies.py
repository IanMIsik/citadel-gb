"""Fundies -- fundamentals monitor (demand forecast/outturn, wind
forecast/outturn, solar, interconnector flows, nuclear day-ahead output),
ported from Fundies.ipynb (the user's own working data pipeline, which
already replaced the old the reference app Electron app's dead paid feeds -- RNP,
SEMO-via-old-host, EuroWind -- with public/personal-API-key sources) onto
this project's own engine/storage. The reference app's `fundies.html` is the
*visual* reference (see web/fundies.html/fundies.js for the transposed
48-settlement-period table + percentile-heatmap CSS); this module is the
*data* reference, following the notebook's own column provenance exactly
where the two agree, and dropping only what the notebook itself doesn't
produce (the `rnp_vs_fpn_*` rows, which needed the paid RNP feed and have
no substitute here, per explicit user decision).

Two output frames, one row per settlement period, matching the notebook's
own two Google Sheet tabs:
  - `build_real_time(...)` -> `fundies_rt_df`'s column set.
  - `build_day_ahead(...)` -> `da_df`'s column set, PLUS the reference app-style
    derived rows (`indo_da_ndf_delta`, `latest_resid`, etc.) appended on --
    those genuinely need both real-time and day-ahead inputs together (the
    real the reference app table renders them as rows in the SAME table as everything
    else), so rather than a third small table, they ride along on the
    day-ahead frame (see build_derived_rows()'s own docstring).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

from ..settlement import utc_to_settlement
from .fpn import fuel_type_reference  # noqa: F401  (re-exported for callers that only import this module)

# ---------------------------------------------------------------------------
# Settlement-period helpers
# ---------------------------------------------------------------------------

# Ported from the notebook's own `sp_df`: hourly-published datasets
# (WINDFOR, day-ahead WINDFOR, EPEX) land only on the odd settlement period
# where their hour begins -- this broadcasts that single value onto both
# half-hour periods of the hour. NOT a DST handler (that's `utc_to_settlement`
# below, via real zoneinfo) -- purely an hourly -> half-hourly fan-out.
_HOUR_TO_SP_PAIRS = pd.DataFrame(
    [(odd, new) for odd in range(1, 49, 2) for new in (odd, odd + 1)],
    columns=["settlementPeriod", "_broadcastSp"],
)


def broadcast_hourly_to_half_hourly(df: pd.DataFrame, sp_col: str = "settlementPeriod") -> pd.DataFrame:
    """`df` has one row per hour (settlementPeriod = the odd SP the hour
    starts on) -> one row per half-hour SP, duplicating every other column.
    """
    if df.empty or sp_col not in df.columns:
        return df
    merged = pd.merge(df, _HOUR_TO_SP_PAIRS.rename(columns={"settlementPeriod": sp_col}), on=sp_col, how="inner")
    return merged.drop(columns=[sp_col]).rename(columns={"_broadcastSp": sp_col})


def _sp_from_timestamps(series: pd.Series) -> tuple[pd.Series, pd.Series]:
    """Per-timestamp (settlementDate, settlementPeriod) via
    settlement.utc_to_settlement -- real zoneinfo Europe/London DST
    handling, replacing the notebook's own manual `detect_dst_offset()`
    hour-shifting for every timestamp this module derives a period from.
    """
    ts = pd.to_datetime(series, utc=True)
    pairs = [utc_to_settlement(t.to_pydatetime()) for t in ts]
    idx = series.index
    return (
        pd.Series([p[0] for p in pairs], index=idx),
        pd.Series([p[1] for p in pairs], index=idx),
    )


def _latest_publish_per_period(records: list[dict], date_field: str, period_field: str, value_field: str, out_name: str) -> pd.DataFrame:
    """NDF/WINDFOR/INDO/ITSDO share this shape: one row per (date, period,
    publishTime) -- keep only the most recently published value per period.
    Same dedup engine/fpn.py's own `_latest_publish_per_period` does for
    the FPN dashboard's demand frame; duplicated here (not imported) since
    that one is hardcoded to `settlementDate`/`settlementPeriod` field
    names and this module also needs it for WINDFOR's own derived
    `settlementPeriod` column (see build_windfor_frame()).
    """
    df = pd.DataFrame(records)
    if df.empty:
        return pd.DataFrame(columns=["settlementDate", "settlementPeriod", out_name])
    df["publishTime"] = pd.to_datetime(df["publishTime"], utc=True)
    latest = df.groupby([date_field, period_field])["publishTime"].max().reset_index()
    df = pd.merge(df, latest, on=[date_field, period_field, "publishTime"])
    df = df[[date_field, period_field, value_field]].rename(
        columns={date_field: "settlementDate", period_field: "settlementPeriod", value_field: out_name}
    )
    df["settlementDate"] = pd.to_datetime(df["settlementDate"]).dt.date
    return df.drop_duplicates(subset=["settlementDate", "settlementPeriod"], keep="last").reset_index(drop=True)


def build_windfor_frame(records: list[dict], out_name: str) -> pd.DataFrame:
    """WINDFOR is published hourly with its own `startTime`, not a
    `settlementPeriod` field -- derive the period from `startTime` (lands
    on the hour's odd SP), keep only the latest-published block per hour,
    then broadcast onto both half-hour periods of that hour.
    """
    df = pd.DataFrame(records)
    if df.empty:
        return pd.DataFrame(columns=["settlementDate", "settlementPeriod", out_name])
    df["startTime"] = pd.to_datetime(df["startTime"], utc=True)
    df["settlementDate"], df["settlementPeriod"] = _sp_from_timestamps(df["startTime"])
    latest = _latest_publish_per_period(
        df.assign(publishTime=pd.to_datetime(df["publishTime"], utc=True)).to_dict("records"),
        "settlementDate", "settlementPeriod", "generation", out_name,
    )
    return broadcast_hourly_to_half_hourly(latest)


# ---------------------------------------------------------------------------
# Wind outturn with curtailment estimate
# ---------------------------------------------------------------------------

def wind_outturn_with_curtailment(fuelhh_records: list[dict], bid_stack_records: list[dict], fuel_ref: pd.DataFrame) -> pd.DataFrame:
    """FUELHH's metered WIND outturn, plus an estimate of curtailed
    (bid-off) wind volume from the accepted-bid stack -- the notebook's own
    `total_wind_outturn = wind_ot + shut_volume`. `fuel_ref` is
    engine/fpn.py's `fuel_type_reference()` output, replacing the
    notebook's manually-uploaded BMU/fuel-type CSV for identifying wind BM
    units. Falls back to plain metered outturn (no curtailment adjustment)
    if the bid-stack side fails for any reason, same as the notebook's own
    try/except.

    Returns `wind_ot` (actual metered generation) and `shut_volume`
    (estimated curtailed volume) as their own columns alongside
    `total_wind_outturn` (their sum, the Fundies table's own row) -- the
    Wind graph wants the breakdown, not just the total, so a unit's real
    output and its curtailment are both visible rather than one number
    hiding the other.
    """
    fuelhh = pd.DataFrame(fuelhh_records)
    if fuelhh.empty:
        return pd.DataFrame(columns=["settlementDate", "settlementPeriod", "wind_ot", "shut_volume", "total_wind_outturn"])
    fuelhh = fuelhh[fuelhh["fuelType"] == "WIND"].copy()
    fuelhh["settlementDate"] = pd.to_datetime(fuelhh["settlementDate"]).dt.date
    wind_ot = fuelhh[["settlementDate", "settlementPeriod", "generation"]].rename(columns={"generation": "wind_ot"})

    try:
        wind_units = set(fuel_ref.loc[fuel_ref["FT"] == "WIND", "bmUnit"])
        bids = pd.DataFrame(bid_stack_records)
        if bids.empty or not wind_units:
            raise ValueError("no bid-stack rows or no known wind units")
        bids["settlementDate"] = pd.to_datetime(bids["settlementDate"]).dt.date
        # The ISPSTACK dataset (this endpoint's own name for itself, per its
        # response `metadata.datasets` -- confirmed live) names its BM unit
        # identifier column `id`, not `bmUnit`. Confirmed against the
        # notebook's own real code for this calculation (`bid_df['id']`) --
        # this was silently wrong before (a KeyError on the old `bmUnit`
        # column swallowed by this same try/except), meaning curtailment
        # was ALWAYS falling back to zero regardless of real bid activity.
        bids = bids[bids["id"].isin(wind_units)]
        bids["shut_volume"] = bids["volume"].abs() * 2
        shut = bids.groupby(["settlementDate", "settlementPeriod"])["shut_volume"].sum().reset_index()
        merged = pd.merge(wind_ot, shut, on=["settlementDate", "settlementPeriod"], how="left")
        merged["shut_volume"] = merged["shut_volume"].fillna(0)
    except Exception:
        merged = wind_ot.copy()
        merged["shut_volume"] = 0.0

    merged["total_wind_outturn"] = merged["wind_ot"] + merged["shut_volume"]
    return merged[["settlementDate", "settlementPeriod", "wind_ot", "shut_volume", "total_wind_outturn"]]


# ---------------------------------------------------------------------------
# Interconnector REAL (metered) flows -- FUELHH's own per-interconnector
# fuel types, for the Interconnector Graphs page's "real vs scheduled"
# overlay (ENTSO-E/SEMO give the SCHEDULED commercial flow; this gives
# what actually happened). Elexon's sign convention here is already
# "positive = flow into GB", the same convention entsoe_flows.py's own
# net = flow_in - flow_out (flow_in defined as the OTHER country -> GB)
# produces -- no sign flip needed to compare them on one chart.
# ---------------------------------------------------------------------------

# Elexon FUELHH/FUELINST fuel-type code -> this project's own interconnector
# column name (matching entsoe_flows.py's/semo.py's own net-flow names).
INTERCONNECTOR_FUELHH_TYPES = {
    "INTFR": "real_ifa_net",
    "INTIFA2": "real_ifa2_net",
    "INTELEC": "real_eleclink_net",
    "INTNED": "real_nl_net",
    "INTNEM": "real_be_net",
    "INTNSL": "real_norway_net",
    "INTVKL": "real_dk_net",
    "INTEW": "real_ew_net",
    "INTIRL": "real_moyle_net",
    "INTGRNL": "real_grnl_net",
}


def interconnector_real_flows(fuelhh_records: list[dict]) -> pd.DataFrame:
    fuelhh = pd.DataFrame(fuelhh_records)
    value_cols = list(INTERCONNECTOR_FUELHH_TYPES.values())
    if fuelhh.empty:
        return pd.DataFrame(columns=["settlementDate", "settlementPeriod"] + value_cols)
    fuelhh = fuelhh[fuelhh["fuelType"].isin(INTERCONNECTOR_FUELHH_TYPES)].copy()
    if fuelhh.empty:
        return pd.DataFrame(columns=["settlementDate", "settlementPeriod"] + value_cols)
    fuelhh["settlementDate"] = pd.to_datetime(fuelhh["settlementDate"]).dt.date
    fuelhh["column"] = fuelhh["fuelType"].map(INTERCONNECTOR_FUELHH_TYPES)
    pivot = fuelhh.pivot_table(index=["settlementDate", "settlementPeriod"], columns="column", values="generation", aggfunc="last")
    pivot = pivot.reindex(columns=value_cols).reset_index()
    return pivot


# ---------------------------------------------------------------------------
# National Grid trades (NESO feed, DISBSAD fallback) -- same combination
# logic as engine/fpn.py's natgrid_trade_rows(), flat per-SP shape instead
# of that function's per-minute-exploded one.
# ---------------------------------------------------------------------------

def natgrid_ng_vol(neso_trades: list[dict], disbsad_records: list[dict]) -> pd.DataFrame:
    ng_df = pd.DataFrame(neso_trades)
    if not ng_df.empty and {"SP", "Date", "Volume"}.issubset(ng_df.columns):
        ng_df = ng_df.rename(columns={"SP": "settlementPeriod", "Date": "settlementDate"})
        ng_df["settlementDate"] = pd.to_datetime(ng_df["settlementDate"]).dt.date
        ng_df = ng_df.groupby(["settlementDate", "settlementPeriod"])["Volume"].sum().reset_index()
        ng_df = ng_df.rename(columns={"Volume": "ng_vol"})
    else:
        ng_df = pd.DataFrame(columns=["settlementDate", "settlementPeriod", "ng_vol"])

    disbsad_fallback = pd.DataFrame(columns=["settlementDate", "settlementPeriod", "ng_vol"])
    disbsad_df = pd.DataFrame(disbsad_records)
    if not disbsad_df.empty and "volume" in disbsad_df.columns:
        disbsad_df["settlementDate"] = pd.to_datetime(disbsad_df["settlementDate"]).dt.date
        disbsad_fallback = disbsad_df.groupby(["settlementDate", "settlementPeriod"])["volume"].sum().reset_index()
        disbsad_fallback = disbsad_fallback.rename(columns={"volume": "ng_vol"})

    if not ng_df.empty:
        covered = set(ng_df[["settlementDate", "settlementPeriod"]].itertuples(index=False, name=None))
        keys = disbsad_fallback[["settlementDate", "settlementPeriod"]].apply(tuple, axis=1)
        disbsad_fallback = disbsad_fallback[~keys.isin(covered)]

    combined = pd.concat([ng_df, disbsad_fallback], ignore_index=True)
    if combined.empty:
        return pd.DataFrame(columns=["settlementDate", "settlementPeriod", "ng_vol"])
    combined["ng_vol"] = combined["ng_vol"] * 2
    return combined


# ---------------------------------------------------------------------------
# SEMO (Ireland interconnector flows)
# ---------------------------------------------------------------------------

def semo_net_flows(ida1_items: list[dict], ida2_items: list[dict]) -> pd.DataFrame:
    """Moyle/East-West/Greenlink net flows from SEMO's two intraday
    auctions -- IDA2 (later, more current) overrides IDA1 on any
    settlement period both cover, same priority as the notebook's own
    `Ida2_df.combine_first(Ida1_df)`.
    """
    def _process(items: list[dict]) -> pd.DataFrame:
        df = pd.DataFrame(items)
        if df.empty:
            return pd.DataFrame(columns=["settlementDate", "settlementPeriod", "intmoyle_net", "intew_net", "intgrnl_net"])
        df["StartTime"] = pd.to_datetime(df["StartTime"], utc=True)
        df["settlementDate"], df["settlementPeriod"] = _sp_from_timestamps(df["StartTime"])
        df["intmoyle_net"] = df["TotalScheduled-NI-GB"].astype(float) - df["TotalScheduled-GB-NI"].astype(float)
        df["intew_net"] = df["TotalScheduled-IE-GB"].astype(float) - df["TotalScheduled-GB-IE"].astype(float)
        df["intgrnl_net"] = df["TotalScheduled-IE2-GB2"].astype(float) - df["TotalScheduled-GB2-IE2"].astype(float)
        return df[["settlementDate", "settlementPeriod", "intmoyle_net", "intew_net", "intgrnl_net"]]

    ida1 = _process(ida1_items).set_index(["settlementDate", "settlementPeriod"])
    ida2 = _process(ida2_items).set_index(["settlementDate", "settlementPeriod"])
    combined = ida2.combine_first(ida1) if not ida2.empty or not ida1.empty else ida2
    return combined.reset_index()


# ---------------------------------------------------------------------------
# Real-time table
# ---------------------------------------------------------------------------

def build_real_time(
    ndf: list[dict], windfor: list[dict], indo: list[dict], itsdo: list[dict],
    fuelhh: list[dict], bid_stack: list[dict], fuel_ref: pd.DataFrame,
    pv_live: list[dict], system_prices: list[dict], market_index: list[dict],
    neso_trades: list[dict], disbsad: list[dict], imbalngc: list[dict],
) -> pd.DataFrame:
    """The notebook's `fundies_rt_df`: one outer-merge chain on
    (settlementDate, settlementPeriod), same column set (minus
    `publishTime_indo`/`publishTime_itso`, dropped before use same as the
    notebook drops them before its own Sheet push).

    `imbalngc` is the ALREADY-shaped `fundies_imbalngc` cache-table rows
    (`settlement_date`/`settlement_period`/`imbalngc`, sign-flip and dedup
    already applied by the caller when it wrote that cache -- IMBALNGC is
    one of the fragile sources kept in Postgres instead of an in-memory
    buffer, see engine/fundies_runner.py's own module docstring), not the
    raw Elexon IMBALNGC record shape.
    """
    latest_ndf = _latest_publish_per_period(ndf, "settlementDate", "settlementPeriod", "demand", "latest_ndf")
    windfor_df = build_windfor_frame(windfor, "latestwindfor")
    indo_df = _latest_publish_per_period(indo, "settlementDate", "settlementPeriod", "demand", "indo")
    itsdo_df = _latest_publish_per_period(itsdo, "settlementDate", "settlementPeriod", "demand", "itso")
    wind_ot = wind_outturn_with_curtailment(fuelhh, bid_stack, fuel_ref)

    pv_df = pd.DataFrame(pv_live)
    if not pv_df.empty:
        pv_df["start_time"] = pd.to_datetime(pv_df["start_time"], utc=True) - pd.Timedelta(minutes=30)
        pv_df["settlementDate"], pv_df["settlementPeriod"] = _sp_from_timestamps(pv_df["start_time"])
        pv_df = pv_df[["settlementDate", "settlementPeriod", "pv_live"]]
    else:
        pv_df = pd.DataFrame(columns=["settlementDate", "settlementPeriod", "pv_live"])

    prices_df = pd.DataFrame(system_prices)
    if not prices_df.empty:
        prices_df["settlementDate"] = pd.to_datetime(prices_df["settlementDate"]).dt.date
        prices_df = prices_df[["settlementDate", "settlementPeriod", "netImbalanceVolume", "systemBuyPrice", "buyPriceAdjustment", "sellPriceAdjustment"]]
    else:
        prices_df = pd.DataFrame(columns=["settlementDate", "settlementPeriod", "netImbalanceVolume", "systemBuyPrice", "buyPriceAdjustment", "sellPriceAdjustment"])

    mid_df = pd.DataFrame(market_index)
    if not mid_df.empty:
        # MID reports multiple data providers (APXMIDP, N2EXMIDP) per
        # period -- confirmed live: merging the raw per-provider rows
        # straight in produced more than one row per settlement period,
        # violating fundies_real_time's own (settlement_date,
        # settlement_period) primary key. Volume-weighted blend across
        # providers with positive volume, same approach as
        # ingest/elexon_rest.py's own blend_market_index() (which only
        # returns price; this also keeps a summed total volume, since the
        # Fundies table wants both).
        mid_df["settlementDate"] = pd.to_datetime(mid_df["settlementDate"]).dt.date
        mid_df = mid_df[mid_df["volume"] > 0]
        if not mid_df.empty:
            mid_df["_weighted"] = mid_df["price"] * mid_df["volume"]
            grouped = mid_df.groupby(["settlementDate", "settlementPeriod"]).agg(
                _weighted_sum=("_weighted", "sum"), market_index_volume=("volume", "sum"),
            ).reset_index()
            grouped["market_index_price"] = grouped["_weighted_sum"] / grouped["market_index_volume"]
            mid_df = grouped[["settlementDate", "settlementPeriod", "market_index_price", "market_index_volume"]]
        else:
            mid_df = pd.DataFrame(columns=["settlementDate", "settlementPeriod", "market_index_price", "market_index_volume"])
    else:
        mid_df = pd.DataFrame(columns=["settlementDate", "settlementPeriod", "market_index_price", "market_index_volume"])

    ng_df = natgrid_ng_vol(neso_trades, disbsad)
    real_ic_df = interconnector_real_flows(fuelhh)

    imbalngc_df = pd.DataFrame(imbalngc)
    if not imbalngc_df.empty:
        imbalngc_df = imbalngc_df.rename(columns={"settlement_date": "settlementDate", "settlement_period": "settlementPeriod"})
        imbalngc_df = imbalngc_df[["settlementDate", "settlementPeriod", "imbalngc"]]
    else:
        imbalngc_df = pd.DataFrame(columns=["settlementDate", "settlementPeriod", "imbalngc"])

    out = latest_ndf
    for frame in (windfor_df, indo_df, wind_ot, itsdo_df, pv_df, prices_df, mid_df, ng_df, real_ic_df, imbalngc_df):
        out = pd.merge(out, frame, on=["settlementDate", "settlementPeriod"], how="outer")
    # Defensive backstop: this table's own PK is (settlementDate,
    # settlementPeriod) -- any upstream source that ever slips in more
    # than one row per period (confirmed live for MID, fixed above, but
    # kept here too in case another source does the same later) would
    # otherwise reach the DB as a primary-key violation instead of a
    # merge that just silently keeps the latest.
    out = out.drop_duplicates(subset=["settlementDate", "settlementPeriod"], keep="last")
    return out.sort_values(["settlementDate", "settlementPeriod"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# Day-ahead table
# ---------------------------------------------------------------------------

# entsoe_flows.py names columns f"{from}_to_{to}_net" after its own PAIRS
# list -- renamed here onto the notebook's own shorter column names.
ENTSOE_NET_COLUMN_RENAME = {
    "GB_ELECLINK_to_FR_net": "eleclink_net",
    "GB_IFA_to_FR_net": "uk_ifa_net",
    "GB_IFA2_to_FR_net": "uk_ifa2_net",
    "GB_to_NL_net": "uk_nl_net",
    "GB_to_BE_net": "uk_be_net",
    "GB_to_NO_net": "uk_norway_net",
    "GB_to_DK_1_net": "uk_dk_net",
}


def _cache_frame(rows: list[dict], value_cols: list[str]) -> pd.DataFrame:
    """Common shape for the four fragile/cached datasets below (ENTSO-E,
    SEMO, embedded forecast, EPEX): rows already normalized to
    `settlement_date`/`settlement_period` + their own value columns by the
    runner's own refresh method at fetch time (see
    engine/fundies_runner.py's module docstring on why these five datasets
    are cached in Postgres instead of kept as in-memory buffers) -- just
    rename onto this module's camelCase join keys.
    """
    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=["settlementDate", "settlementPeriod"] + value_cols)
    df = df.rename(columns={"settlement_date": "settlementDate", "settlement_period": "settlementPeriod"})
    return df[["settlementDate", "settlementPeriod"] + [c for c in value_cols if c in df.columns]]


def build_day_ahead(
    da_ndf: list[dict], latest_ndf_df: pd.DataFrame, da_windfor: list[dict], windfor_df: pd.DataFrame,
    entsoe_flows: list[dict], nuclear_output_usable: float | None, semo_flows: list[dict],
    embedded_forecast: list[dict], epex_hourly: list[dict],
) -> pd.DataFrame:
    """The notebook's `da_df`: day-ahead NDF/WINDFOR for today, with
    tomorrow's periods passed through from the already-fetched *latest*
    NDF/WINDFOR (the notebook's own `next_day_ndf`/`next_day_windfor`
    concat, since a fresh "day-ahead" call for a date that hasn't started
    yet would just return the same numbers), plus interconnector flows,
    nuclear day-ahead output, embedded wind/solar forecast, and EPEX prices.

    `entsoe_flows`, `semo_flows`, `embedded_forecast`, `epex_hourly` are
    each the ALREADY-shaped rows from their own Postgres cache table (see
    `_cache_frame()`'s own docstring) -- not the raw upstream shapes (raw
    ENTSO-E/SEMO/CSV/HTML parsing happens once, at fetch time, in
    engine/fundies_runner.py's refresh methods, not on every recompute).
    """
    da_ndf_today = _latest_publish_per_period(da_ndf, "settlementDate", "settlementPeriod", "demand", "da_ndf")
    today = date.today()
    next_day_ndf = latest_ndf_df[latest_ndf_df["settlementDate"] > today].rename(columns={"latest_ndf": "da_ndf"})
    da_ndf_full = pd.concat([da_ndf_today, next_day_ndf[["settlementDate", "settlementPeriod", "da_ndf"]]], ignore_index=True)

    da_windfor_today = build_windfor_frame(da_windfor, "da_windfor")
    da_windfor_today = da_windfor_today[da_windfor_today["settlementDate"] == today]
    next_day_windfor = windfor_df[windfor_df["settlementDate"] > today].rename(columns={"latestwindfor": "da_windfor"})
    da_windfor_full = pd.concat([da_windfor_today, next_day_windfor[["settlementDate", "settlementPeriod", "da_windfor"]]], ignore_index=True)

    out = pd.merge(da_ndf_full, da_windfor_full, on=["settlementDate", "settlementPeriod"], how="outer")

    entsoe_cols = ["eleclink_net", "uk_ifa_net", "uk_ifa2_net", "uk_nl_net", "uk_be_net", "uk_norway_net", "uk_dk_net"]
    out = pd.merge(out, _cache_frame(entsoe_flows, entsoe_cols), on=["settlementDate", "settlementPeriod"], how="left")

    out["nuke_214"] = nuclear_output_usable

    semo_cols = ["intew_net", "intmoyle_net", "intgrnl_net"]
    out = pd.merge(out, _cache_frame(semo_flows, semo_cols), on=["settlementDate", "settlementPeriod"], how="left")

    embedded_cols = ["embedded_wind_forecast", "embedded_solar_forecast"]
    out = pd.merge(out, _cache_frame(embedded_forecast, embedded_cols), on=["settlementDate", "settlementPeriod"], how="left")

    epex_cols = ["da_price", "da_volume"]
    out = pd.merge(out, _cache_frame(epex_hourly, epex_cols), on=["settlementDate", "settlementPeriod"], how="left")

    # Same PK backstop as build_real_time() -- da_ndf_full/da_windfor_full
    # each concatenate a "today" frame with a "tomorrow passthrough" frame,
    # which would double up if the raw day-ahead fetch's own window ever
    # overlaps what the passthrough already covers.
    out = out.drop_duplicates(subset=["settlementDate", "settlementPeriod"], keep="last")
    return out.sort_values(["settlementDate", "settlementPeriod"]).reset_index(drop=True)


# ---------------------------------------------------------------------------
# reference-app-style derived rows -- ported from the real Electron app's
# DeltaRowData/SummationRowData/MultipleSubtractionRowData (see
# Old World/reference_app_extracted/.../common-scripts/data-utils/data-types.js),
# computed here against our own real-time/day-ahead frames instead of that
# app's dead RNP-fed rows. These need BOTH frames together (the real
# the reference app table renders them as rows in the same table as everything else),
# so they're returned as their own small frame the caller appends onto the
# day-ahead output rather than a third persisted table.
# ---------------------------------------------------------------------------

def build_derived_rows(real_time: pd.DataFrame, day_ahead: pd.DataFrame) -> pd.DataFrame:
    merged = pd.merge(real_time, day_ahead, on=["settlementDate", "settlementPeriod"], how="outer")

    def _get(col: str) -> pd.Series:
        return merged[col] if col in merged.columns else pd.Series(np.nan, index=merged.index)

    indo = _get("indo")
    latest_ndf = _get("latest_ndf")
    da_ndf = _get("da_ndf")
    # DeltaRowData: variable (falls back to default) - baseline.
    variable = indo.where(indo.notna(), latest_ndf)
    indo_da_ndf_delta = variable - da_ndf

    wind_ot = _get("total_wind_outturn")
    latest_windfor = _get("latestwindfor")
    da_windfor = _get("da_windfor")
    wind_variable = wind_ot.where(wind_ot.notna(), latest_windfor)
    fake_wind_ot_da_winfor_delta = wind_variable - da_windfor

    domestic_tight_delta = indo_da_ndf_delta - fake_wind_ot_da_winfor_delta

    interconnector_cols = [
        "eleclink_net", "uk_ifa_net", "uk_ifa2_net", "uk_nl_net", "uk_be_net", "uk_norway_net", "uk_dk_net",
        "intew_net", "intmoyle_net", "intgrnl_net", "ng_vol",
    ]
    interconnector_ng = sum((_get(c).fillna(0) for c in interconnector_cols), pd.Series(0.0, index=merged.index))

    nuke_214 = _get("nuke_214")
    # MultipleSubtractionRowData's exact formula: demand - wind - interconnector_ng - nuclear.
    latest_resid = variable - wind_variable - interconnector_ng - nuke_214

    return pd.DataFrame({
        "settlementDate": merged["settlementDate"],
        "settlementPeriod": merged["settlementPeriod"],
        "indo_da_ndf_delta": indo_da_ndf_delta,
        "fake_wind_ot_da_winfor_delta": fake_wind_ot_da_winfor_delta,
        "domestic_tight_delta": domestic_tight_delta,
        "interconnector_ng": interconnector_ng,
        "latest_resid": latest_resid,
    })
