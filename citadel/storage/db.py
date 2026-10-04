"""Postgres access -- asyncpg, plain SQL, dataclass rows. No ORM: the schema
is small and stable enough that raw SQL stays easier to reason about than
mapping-layer machinery, and every write here is an idempotent
upsert/replace so retried ingest cycles are always safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import asyncpg

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


@dataclass(frozen=True)
class BmUnitReferenceRow:
    national_grid_bm_unit: str
    elexon_bm_unit: str | None
    lead_party_name: str | None
    bm_unit_type: str | None
    generation_capacity_mw: float | None
    fuel_type: str | None


@dataclass(frozen=True)
class StackRow:
    settlement_date: date
    settlement_period: int
    bm_unit: str
    stor_flag: bool
    deemed_bo_flag: bool
    so_flag: bool
    cadl_flag: bool
    acceptance_number: int
    reversal: float
    ap_mult_vol: float
    delta: float
    vol_to_price: float
    disbsad_cost: float
    m_orig_price: float
    total_delta: float
    delta_sign: float
    action_sign: float
    vol_for_price: float | None
    misik_imb_price: float
    total_misik_price: float
    max_ta: datetime | None


@dataclass(frozen=True)
class SettlementPriceRow:
    settlement_date: date
    settlement_period: int
    system_sell_price: float | None
    net_imbalance_volume: float | None


async def create_pool(database_url: str) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(database_url, min_size=1, max_size=10)
    async with pool.acquire() as conn:
        await conn.execute(SCHEMA_PATH.read_text())
    return pool


async def upsert_bm_unit_reference(
    pool: asyncpg.Pool, rows: list[BmUnitReferenceRow], overwrite_fuel_type: bool = False
) -> int:
    """`overwrite_fuel_type=False` (default, used for the live Elexon
    reference fetch) preserves whatever fuel_type a unit already has and
    only fills it in when NULL -- Elexon's own live fuelType field is
    frequently null/coarse. `overwrite_fuel_type=True` is for a curated
    source that should win outright (see ingest/gbpw_import.py, which pulls
    the fuel-type mapping already built up in the sibling gbpw project from
    NESO's own BM Unit Fuel Type spreadsheet -- a real WIND/BATTERY/etc.
    category Elexon's live API mostly doesn't have).
    """
    if not rows:
        return 0
    ts = datetime.now(timezone.utc)
    fuel_type_sql = "excluded.fuel_type" if overwrite_fuel_type else "COALESCE(bm_unit_reference.fuel_type, excluded.fuel_type)"
    await pool.executemany(
        f"""
        INSERT INTO bm_unit_reference (
            national_grid_bm_unit, elexon_bm_unit, lead_party_name, bm_unit_type,
            generation_capacity_mw, fuel_type, fetched_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (national_grid_bm_unit) DO UPDATE SET
            elexon_bm_unit = excluded.elexon_bm_unit,
            lead_party_name = excluded.lead_party_name,
            bm_unit_type = excluded.bm_unit_type,
            generation_capacity_mw = excluded.generation_capacity_mw,
            fuel_type = {fuel_type_sql},
            fetched_at = excluded.fetched_at
        """,
        [
            (r.national_grid_bm_unit, r.elexon_bm_unit, r.lead_party_name, r.bm_unit_type,
             r.generation_capacity_mw, r.fuel_type, ts)
            for r in rows
        ],
    )
    return len(rows)


async def bm_unit_reference_all(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    """Every reference row -- engine/fpn.py's fuel-type join needs the
    whole table, unlike the pricing stack which never queries it at all.
    """
    return await pool.fetch("SELECT * FROM bm_unit_reference")


async def delete_stale_bm_unit_reference_duplicates(pool: asyncpg.Pool) -> int:
    """Removes rows created by the exact bug
    misco_fuel_type_reference.resolve_national_grid_bm_units() now prevents
    from recurring: a row keyed under a synthetic national_grid_bm_unit
    (== its own elexon_bm_unit) that only exists because a real row for that
    same elexon_bm_unit, under its own genuine national_grid_bm_unit, was
    already present. Only deletes when both conditions hold -- a unit whose
    real national_grid_bm_unit genuinely equals its elexon_bm_unit (common,
    not itself a bug) has no "other" row to compare against, so the EXISTS
    check is false for it and it's left untouched.
    """
    result = await pool.execute(
        """
        DELETE FROM bm_unit_reference dup
        WHERE dup.national_grid_bm_unit = dup.elexon_bm_unit
          AND EXISTS (
              SELECT 1 FROM bm_unit_reference real
              WHERE real.elexon_bm_unit = dup.elexon_bm_unit
                AND real.national_grid_bm_unit <> dup.national_grid_bm_unit
          )
        """
    )
    return int(result.split()[-1])


async def replace_period_rows(pool: asyncpg.Pool, sd: date, sp: int, rows: list[StackRow]) -> int:
    """Deletes every existing row for (sd, sp) and inserts `rows` in its
    place, in one transaction. A recompute reflects the full current truth
    for that period (an acceptance can be revised or a reversal can change
    which rows even exist), so upserting-in-place without a delete would
    leave stale rows behind that no longer exist in Elexon's data.
    """
    ts = datetime.now(timezone.utc)
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "DELETE FROM pricing_stack_rows WHERE settlement_date = $1 AND settlement_period = $2",
            sd, sp,
        )
        if rows:
            await conn.executemany(
                """
                INSERT INTO pricing_stack_rows (
                    settlement_date, settlement_period, bm_unit, stor_flag, deemed_bo_flag,
                    so_flag, cadl_flag, acceptance_number, reversal, ap_mult_vol, delta, vol_to_price,
                    disbsad_cost, m_orig_price, total_delta, delta_sign, action_sign,
                    vol_for_price, misik_imb_price, total_misik_price, max_ta, computed_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22)
                """,
                [
                    (
                        r.settlement_date, r.settlement_period, r.bm_unit, r.stor_flag, r.deemed_bo_flag,
                        r.so_flag, r.cadl_flag, r.acceptance_number, r.reversal, r.ap_mult_vol, r.delta, r.vol_to_price,
                        r.disbsad_cost, r.m_orig_price, r.total_delta, r.delta_sign, r.action_sign,
                        r.vol_for_price, r.misik_imb_price, r.total_misik_price, r.max_ta, ts,
                    )
                    for r in rows
                ],
            )
    return len(rows)


async def pricing_stack_delta_by_bm_unit(pool: asyncpg.Pool, sd: date, periods: list[int]) -> list[asyncpg.Record]:
    """Real accepted-offer/reduced-accepted-bid volume per BM unit, summed
    from the pricing stack's own already-computed `pricing_stack_rows` --
    the same reversal-aware, CADL-aware acceptance data the pricing stack
    page itself displays. engine/fpn.py uses this (not its own,
    independently re-derived delta) for `market_gen`, since FUELINST's raw
    generation reading already reflects accepted offers/bids, and removing
    their effect needs the actual accepted-volume figure, not a second one.

    Excludes synthetic DISBSAD rows (`bm_unit LIKE 'disbsad_%'`, from
    stack.py's own `blend_disbsad`) -- those represent National Grid's own
    non-BM balancing actions, not a specific generating unit, and
    engine/fpn.py already accounts for that separately (`natgrid_trade_rows`).
    """
    return await pool.fetch(
        """
        SELECT settlement_date, settlement_period, bm_unit, SUM(delta) AS delta
        FROM pricing_stack_rows
        WHERE settlement_date = $1 AND settlement_period = ANY($2::int[])
          AND bm_unit NOT LIKE 'disbsad_%'
        GROUP BY settlement_date, settlement_period, bm_unit
        """,
        sd, periods,
    )


async def stack_for_period(pool: asyncpg.Pool, sd: date, sp: int) -> list[asyncpg.Record]:
    return await pool.fetch(
        """
        SELECT * FROM pricing_stack_rows
        WHERE settlement_date = $1 AND settlement_period = $2
        ORDER BY bm_unit, acceptance_number
        """,
        sd, sp,
    )


async def upsert_settlement_price(pool: asyncpg.Pool, row: SettlementPriceRow) -> None:
    await pool.execute(
        """
        INSERT INTO settlement_prices (settlement_date, settlement_period, system_sell_price, net_imbalance_volume, fetched_at)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (settlement_date, settlement_period) DO UPDATE SET
            system_sell_price = excluded.system_sell_price,
            net_imbalance_volume = excluded.net_imbalance_volume,
            fetched_at = excluded.fetched_at
        """,
        row.settlement_date, row.settlement_period, row.system_sell_price, row.net_imbalance_volume,
        datetime.now(timezone.utc),
    )


async def settlement_price_for_period(pool: asyncpg.Pool, sd: date, sp: int) -> asyncpg.Record | None:
    return await pool.fetchrow(
        "SELECT * FROM settlement_prices WHERE settlement_date = $1 AND settlement_period = $2",
        sd, sp,
    )


async def log_refresh(pool: asyncpg.Pool, source: str, dataset: str, ok: bool, note: str = "") -> None:
    await pool.execute(
        "INSERT INTO refresh_log (source, dataset, ok, note, ts) VALUES ($1, $2, $3, $4, $5)",
        source, dataset, ok, note, datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# FPN Analytics -- see engine/fpn.py. Rows are plain dicts (already
# snake_case, one dict per DataFrame row) rather than dataclasses: these
# four tables are wide and engine/fpn.py's own column set is the single
# source of truth for their shape, so a parallel dataclass per table would
# just be one more place for the two to drift apart.
# ---------------------------------------------------------------------------

async def replace_fpn_period_rows(pool: asyncpg.Pool, table: str, key_cols: tuple[str, ...], sd: date, sp: int, rows: list[dict], columns: list[str]) -> int:
    """Deletes every existing `table` row for (sd, sp) and inserts `rows`
    in its place -- same "current window, always overwritten" semantics as
    `replace_period_rows`, generalised across the four FPN tables that
    share it (`fpn_by_fuel`, `fpn_worst_deviants`, `fpn_aggregated`).
    `key_cols` must include settlement_date/settlement_period.
    """
    ts = datetime.now(timezone.utc)
    col_list = ", ".join(columns + ["computed_at"])
    placeholders = ", ".join(f"${i + 1}" for i in range(len(columns) + 1))
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            f"DELETE FROM {table} WHERE settlement_date = $1 AND settlement_period = $2", sd, sp,  # noqa: S608 -- table/columns are internal constants, never user input
        )
        if rows:
            await conn.executemany(
                f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})",  # noqa: S608
                [tuple(r.get(c) for c in columns) + (ts,) for r in rows],
            )
    return len(rows)


FPN_BY_FUEL_COLUMNS = [
    "settlement_date", "settlement_period", "spot_time", "fuel_type", "fpn_spot_vol", "delta",
    "mel_spot_vol", "mel_reduced_fpn", "mil_spot_vol", "mil_increased_fpn", "adjusted_fpn",
    "fuelinst_generation", "market_gen", "market_gen_vs_fpn", "market_gen_vs_adj_fpn", "mel_mil_drop",
]

FPN_WORST_DEVIANTS_COLUMNS = [
    "bm_unit", "settlement_date", "settlement_period", "spot_time", "fuel_type", "fpn_spot_vol",
    "adjusted_fpn", "mel_mil_drop", "current_mel", "current_mil", "mel_downside", "mil_upside", "mel_mil_downside",
]

FPN_AGGREGATED_COLUMNS = [
    "settlement_date", "settlement_period", "spot_time", "fpn_spot_vol", "fuelinst_generation", "market_gen",
    "market_gen_vs_fpn", "market_gen_vs_adj_fpn", "adjusted_fpn", "delta", "misco_indo", "misco_ndf",
    "misco_indo_vs_ndf", "misco_da_adj_ndf", "dmd_risk", "area_under_curve", "auc_av", "delta_av", "niv_error",
    "unexp_delta", "wind_deviation", "other_gen_deviation", "niv_estimate",
    "spot_indo", "spot_latest_ndf", "niv_sp_max",
]


async def fpn_by_fuel_for_period(pool: asyncpg.Pool, sd: date, sp: int) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT * FROM fpn_by_fuel WHERE settlement_date = $1 AND settlement_period = $2 ORDER BY fuel_type, spot_time",
        sd, sp,
    )


async def fpn_aggregated_for_period(pool: asyncpg.Pool, sd: date, sp: int) -> list[asyncpg.Record]:
    return await pool.fetch(
        "SELECT * FROM fpn_aggregated WHERE settlement_date = $1 AND settlement_period = $2 ORDER BY spot_time",
        sd, sp,
    )


async def fpn_by_fuel_sp_summary(pool: asyncpg.Pool, sd: date, periods: list[int]) -> list[asyncpg.Record]:
    """Per (settlement_period, fuel_type) period-average of fpn_spot_vol/
    mel_mil_drop/market_gen_vs_adj_fpn -- backs the FPN-by-fuel, MEL/MIL-
    drop, and market-gen-vs-adjusted-FPN tables on the FPN Analytics page
    (fpn_delta, the period-over-period FPN change, is derived from this
    same fpn_spot_vol figure client-side rather than computed here).
    """
    return await pool.fetch(
        """
        SELECT settlement_period, fuel_type,
               AVG(fpn_spot_vol) AS fpn_spot_vol,
               AVG(mel_spot_vol) AS mel_spot_vol,
               AVG(mel_mil_drop) AS mel_mil_drop,
               AVG(market_gen_vs_adj_fpn) AS market_gen_vs_adj_fpn
        FROM fpn_by_fuel
        WHERE settlement_date = $1 AND settlement_period = ANY($2::int[])
        GROUP BY settlement_period, fuel_type
        ORDER BY settlement_period, fuel_type
        """,
        sd, periods,
    )


async def fpn_decision_drivers_sp(pool: asyncpg.Pool, sd: date, periods: list[int]) -> list[asyncpg.Record]:
    """Per-settlement-period averages of the decision table's own drivers
    -- backs both the compact AUC/delta/niv_error/dmd_risk/dmd_error/unexp
    table and the niv_estimate decision table, one row per period instead
    of per minute.
    """
    return await pool.fetch(
        """
        SELECT settlement_period,
               AVG(area_under_curve) AS auc,
               AVG(delta) AS delta,
               AVG(niv_error) AS niv_error,
               AVG(dmd_risk) AS dmd_risk,
               COALESCE(AVG(misco_indo_vs_ndf), AVG(dmd_risk)) AS dmd_error,
               AVG(unexp_delta) AS unexp,
               AVG(wind_deviation) AS wind_deviation,
               AVG(other_gen_deviation) AS other_gen_deviation,
               AVG(niv_estimate) AS niv_estimate,
               AVG(niv_sp_max) AS niv_sp_max
        FROM fpn_aggregated
        WHERE settlement_date = $1 AND settlement_period = ANY($2::int[])
        GROUP BY settlement_period
        ORDER BY settlement_period
        """,
        sd, periods,
    )


async def fpn_aggregated_for_periods(pool: asyncpg.Pool, sd: date, periods: list[int]) -> list[asyncpg.Record]:
    """Per-minute rows across the whole rolling window (not just the
    current settlement period) -- backs the real reference app "Forecast Chart",
    which plots every SP in the window as one continuous multi-line series,
    not just the ~30 minutes of whichever period is current.
    """
    return await pool.fetch(
        "SELECT * FROM fpn_aggregated WHERE settlement_date = $1 AND settlement_period = ANY($2::int[]) ORDER BY spot_time",
        sd, periods,
    )


async def pricing_stack_niv_by_period(pool: asyncpg.Pool, sd: date, periods: list[int]) -> list[asyncpg.Record]:
    """The pricing stack's own real, already-settled NIV per period
    (`total_delta` -- see engine/imbalance_price.py's docstring; it's the
    same value repeated on every row of a period's `pricing_stack_rows`,
    so MAX picks it out without needing a DISTINCT ON). This is the real
    the reference app "Delta table"'s own `niv_sp_max` row -- in the original tools it
    arrived via a `spot_niv.pkl` handoff from the sibling notebook; here
    it's the same in-process read already established for `market_gen`.
    """
    return await pool.fetch(
        """
        SELECT settlement_date, settlement_period, MAX(total_delta) AS niv_sp_max
        FROM pricing_stack_rows
        WHERE settlement_date = $1 AND settlement_period = ANY($2::int[])
        GROUP BY settlement_date, settlement_period
        """,
        sd, periods,
    )


async def replace_pricing_stack_niv_spot_time_period(pool: asyncpg.Pool, sd: date, sp: int, rows: list[dict]) -> int:
    """Same "current window, always overwritten" pattern as
    replace_period_rows -- one period's worth of engine/stack.py's
    spot_time_niv() rows (settlement_date, settlement_period, spot_time,
    niv_spot_time_max).
    """
    ts = datetime.now(timezone.utc)
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "DELETE FROM pricing_stack_niv_spot_time WHERE settlement_date = $1 AND settlement_period = $2",
            sd, sp,
        )
        if rows:
            await conn.executemany(
                """
                INSERT INTO pricing_stack_niv_spot_time
                    (settlement_date, settlement_period, spot_time, niv_spot_time_max, computed_at)
                VALUES ($1, $2, $3, $4, $5)
                """,
                [(sd, sp, r["spot_time"], r["niv_spot_time_max"], ts) for r in rows],
            )
    return len(rows)


async def pricing_stack_niv_spot_time_by_period(pool: asyncpg.Pool, sd: date, periods: list[int]) -> list[asyncpg.Record]:
    """The pricing stack's real per-minute NIV trajectory across the current
    window -- see schema.sql's pricing_stack_niv_spot_time docstring.
    engine/fpn.py merges this onto its own per-minute `aggregated` table by
    (settlement_date, settlement_period, spot_time) for the Delta Chart's
    `delta` line.
    """
    return await pool.fetch(
        """
        SELECT settlement_date, settlement_period, spot_time, niv_spot_time_max
        FROM pricing_stack_niv_spot_time
        WHERE settlement_date = $1 AND settlement_period = ANY($2::int[])
        """,
        sd, periods,
    )


async def replace_pricing_stack_unit_delta_5min_period(pool: asyncpg.Pool, sd: date, sp: int, rows: list[dict]) -> int:
    """Same "current window, always overwritten" pattern as
    replace_pricing_stack_niv_spot_time_period -- one period's worth of
    engine/stack.py's spot_time_bm_unit_delta_5min() rows (settlement_date,
    settlement_period, bm_unit, start_time, delta).
    """
    ts = datetime.now(timezone.utc)
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "DELETE FROM pricing_stack_unit_delta_5min WHERE settlement_date = $1 AND settlement_period = $2",
            sd, sp,
        )
        if rows:
            await conn.executemany(
                """
                INSERT INTO pricing_stack_unit_delta_5min
                    (settlement_date, settlement_period, bm_unit, start_time, delta, computed_at)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                [(sd, sp, r["bm_unit"], r["start_time"], r["delta"], ts) for r in rows],
            )
    return len(rows)


async def pricing_stack_unit_delta_5min_by_period(pool: asyncpg.Pool, sd: date, periods: list[int]) -> list[asyncpg.Record]:
    """The pricing stack's real, per-BM-unit delta bucketed onto FUELINST's
    own 5-minute grid, across the current window -- see schema.sql's
    pricing_stack_unit_delta_5min docstring. engine/fpn.py's
    pricing_stack_delta_by_fuel_5min() does the bmUnit -> fuel-type join and
    per-fuel sum this feeds into.
    """
    return await pool.fetch(
        """
        SELECT settlement_date, settlement_period, bm_unit, start_time, delta
        FROM pricing_stack_unit_delta_5min
        WHERE settlement_date = $1 AND settlement_period = ANY($2::int[])
        """,
        sd, periods,
    )


async def fpn_market_gen_vs_adj_fpn_series(pool: asyncpg.Pool, sd: date, periods: list[int]) -> list[asyncpg.Record]:
    """Per-minute, per-fuel-type `market_gen_vs_adj_fpn` across the window
    -- backs the real "Market_gen vs Adj_fpn" chart on the reference app home page
    (per-fuel-type toggleable lines, not one aggregate line).
    """
    return await pool.fetch(
        """
        SELECT spot_time, fuel_type, market_gen_vs_adj_fpn
        FROM fpn_by_fuel
        WHERE settlement_date = $1 AND settlement_period = ANY($2::int[])
        ORDER BY spot_time
        """,
        sd, periods,
    )


async def fpn_worst_deviants_current(pool: asyncpg.Pool, window: list[tuple[date, int]]) -> list[asyncpg.Record]:
    """Every worst-deviant row currently in the window (spans whichever
    settlement periods are still in scope), most recent minute first per
    unit -- there's no single "period" a trader is asking about here,
    unlike the other FPN views.

    Explicitly filtered to `window` (the caller's own `rolling_window()`)
    rather than a bare `SELECT *` -- confirmed live that a period falling
    out of the rolling window is never revisited by the recompute cycle's
    own delete+reinsert (`replace_fpn_period_rows` only touches keys it's
    actively recomputing this cycle), so its rows just sit in the table
    forever. Without this filter the dashboard showed days-old rows
    indefinitely, mixed in with -- and often ranked ahead of, since this
    has no ORDER BY severity -- genuinely current ones.
    """
    if not window:
        return []
    dates = [sd for sd, _ in window]
    periods = [sp for _, sp in window]
    return await pool.fetch(
        """
        SELECT w.* FROM fpn_worst_deviants w
        JOIN UNNEST($1::date[], $2::int[]) AS win(settlement_date, settlement_period)
          ON w.settlement_date = win.settlement_date AND w.settlement_period = win.settlement_period
        ORDER BY w.bm_unit, w.spot_time DESC
        """,
        dates, periods,
    )


async def upsert_fpn_generation_by_fuel(pool: asyncpg.Pool, rows: list[dict]) -> int:
    if not rows:
        return 0
    ts = datetime.now(timezone.utc)
    await pool.executemany(
        """
        INSERT INTO fpn_generation_by_fuel (ts, settlement_period, fuel_type, real_gen, market_gen, delta_gen, computed_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (ts, fuel_type) DO UPDATE SET
            settlement_period = excluded.settlement_period, real_gen = excluded.real_gen,
            market_gen = excluded.market_gen, delta_gen = excluded.delta_gen, computed_at = excluded.computed_at
        """,
        [(r["ts"], r.get("settlement_period"), r["fuel_type"], r.get("real_gen"), r.get("market_gen"), r.get("delta_gen"), ts) for r in rows],
    )
    return len(rows)


async def prune_fpn_generation_by_fuel(pool: asyncpg.Pool, older_than: datetime) -> None:
    await pool.execute("DELETE FROM fpn_generation_by_fuel WHERE ts < $1", older_than)


async def fpn_generation_by_fuel_recent(pool: asyncpg.Pool, since: datetime) -> list[asyncpg.Record]:
    return await pool.fetch("SELECT * FROM fpn_generation_by_fuel WHERE ts >= $1 ORDER BY ts", since)


# ---------------------------------------------------------------------------
# Fundies -- see engine/fundies.py. The two main tables reuse
# replace_fpn_period_rows() above (same PK shape: settlement_date,
# settlement_period). The five cache tables below replace the notebook's
# own Drive pickles (scheduled_flows.pkl, semo_flows.pkl, imbalngc_data.pkl,
# natgrid_embedded_data.pkl, epex_da_data.pkl) with real upserts -- each
# dataset's own FundiesRunner loop writes to its cache table on its own
# interval; every recompute reads all five regardless of which one(s) just
# ticked, so a slower-refreshing source doesn't blank out between its own
# writes (same "combine_first survives a failed fetch" resilience the
# notebook's pickles gave it, via ON CONFLICT DO UPDATE instead of a file).
# ---------------------------------------------------------------------------

FUNDIES_REAL_TIME_COLUMNS = [
    "settlement_date", "settlement_period", "latest_ndf", "latestwindfor", "indo",
    "wind_ot", "shut_volume", "total_wind_outturn",
    "itso", "pv_live", "net_imbalance_volume", "system_buy_price", "buy_price_adjustment",
    "sell_price_adjustment", "market_index_price", "market_index_volume", "ng_vol",
    "real_ifa_net", "real_ifa2_net", "real_eleclink_net", "real_nl_net", "real_be_net",
    "real_norway_net", "real_dk_net", "real_ew_net", "real_moyle_net", "real_grnl_net",
    "imbalngc",
]

FUNDIES_DAY_AHEAD_COLUMNS = [
    "settlement_date", "settlement_period", "da_ndf", "da_windfor",
    "eleclink_net", "uk_ifa_net", "uk_ifa2_net", "uk_nl_net", "uk_be_net", "uk_norway_net", "uk_dk_net",
    "nuke_214", "intew_net", "intmoyle_net", "intgrnl_net",
    "embedded_wind_forecast", "embedded_solar_forecast", "da_price", "da_volume",
    "indo_da_ndf_delta", "fake_wind_ot_da_winfor_delta", "domestic_tight_delta", "interconnector_ng", "latest_resid",
]

FUNDIES_ENTSOE_FLOWS_COLUMNS = ["eleclink_net", "uk_ifa_net", "uk_ifa2_net", "uk_nl_net", "uk_be_net", "uk_norway_net", "uk_dk_net"]
FUNDIES_SEMO_FLOWS_COLUMNS = ["intew_net", "intmoyle_net", "intgrnl_net"]
FUNDIES_IMBALNGC_COLUMNS = ["imbalngc"]
FUNDIES_EMBEDDED_FORECAST_COLUMNS = ["embedded_wind_forecast", "embedded_solar_forecast"]
FUNDIES_EPEX_DA_COLUMNS = ["da_price", "da_volume"]


async def upsert_fundies_cache(pool: asyncpg.Pool, table: str, value_columns: list[str], rows: list[dict]) -> int:
    """Generic upsert for the five Fundies cache tables above -- all share
    the same (settlement_date, settlement_period) PK plus `updated_at`,
    differing only in their value columns.
    """
    if not rows:
        return 0
    ts = datetime.now(timezone.utc)
    columns = ["settlement_date", "settlement_period"] + value_columns
    col_list = ", ".join(columns + ["updated_at"])
    placeholders = ", ".join(f"${i + 1}" for i in range(len(columns) + 1))
    # COALESCE keeps the previously stored value whenever the new row has
    # NULL there -- e.g. ENTSO-E timing out for one interconnector pair
    # leaves that pair's column missing from the fetch, and without this the
    # upsert would blank the last good scheduled flow with NULL.
    update_set = ", ".join(f"{c} = COALESCE(excluded.{c}, {table}.{c})" for c in value_columns) + ", updated_at = excluded.updated_at"
    await pool.executemany(
        f"""
        INSERT INTO {table} ({col_list}) VALUES ({placeholders})
        ON CONFLICT (settlement_date, settlement_period) DO UPDATE SET {update_set}
        """,  # noqa: S608 -- table/columns are internal constants, never user input
        [tuple(r.get(c) for c in columns) + (ts,) for r in rows],
    )
    return len(rows)


async def fundies_cache_rows(pool: asyncpg.Pool, table: str, sds: list[date]) -> list[asyncpg.Record]:
    return await pool.fetch(
        f"SELECT * FROM {table} WHERE settlement_date = ANY($1::date[])",  # noqa: S608
        sds,
    )


async def fundies_epex_existing_delivery_dates(pool: asyncpg.Pool) -> set[date]:
    """Every delivery date already stored -- feeds
    epex.should_fetch_da_prices()'s "is this delivery date already
    fetched" check, so a cleared auction stops being re-scraped.
    """
    rows = await pool.fetch("SELECT DISTINCT settlement_date FROM fundies_epex_da")
    return {r["settlement_date"] for r in rows}


async def fundies_real_time_for_date(pool: asyncpg.Pool, sd: date) -> list[asyncpg.Record]:
    return await pool.fetch("SELECT * FROM fundies_real_time WHERE settlement_date = $1 ORDER BY settlement_period", sd)


async def fundies_day_ahead_for_date(pool: asyncpg.Pool, sd: date) -> list[asyncpg.Record]:
    return await pool.fetch("SELECT * FROM fundies_day_ahead WHERE settlement_date = $1 ORDER BY settlement_period", sd)


# ---------------------------------------------------------------------------
# Plant trip detector + REMIT
# ---------------------------------------------------------------------------

async def insert_trip_events(pool: asyncpg.Pool, trips: list) -> int:
    """Pure append -- unlike replace_fpn_period_rows's delete+insert window,
    trips need durable history (the worst-behavior profile looks back over
    it), so this never deletes anything. `trips` are engine/fpn.py's
    TripEvent dataclass instances.
    """
    if not trips:
        return 0
    await pool.executemany(
        """
        INSERT INTO trip_events (bm_unit, fuel_type, settlement_date, settlement_period, drop_mw, detected_at)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        [(t.bm_unit, t.fuel_type, t.settlement_date, t.settlement_period, t.drop_mw, t.detected_at) for t in trips],
    )
    return len(trips)


async def fetch_open_trips(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    return await pool.fetch("SELECT * FROM trip_events WHERE status != 'resolved' ORDER BY detected_at DESC")


async def fetch_recent_trips(pool: asyncpg.Pool, limit: int = 50) -> list[asyncpg.Record]:
    return await pool.fetch(
        """
        SELECT t.*, r.event_end_time, r.event_status, r.unavailable_capacity, r.available_capacity, r.normal_capacity
        FROM trip_events t
        LEFT JOIN LATERAL (
            SELECT * FROM remit_revisions rr WHERE rr.mrid = t.remit_mrid ORDER BY rr.revision_number DESC LIMIT 1
        ) r ON true
        ORDER BY t.detected_at DESC LIMIT $1
        """,
        limit,
    )


async def set_trip_remit_match(pool: asyncpg.Pool, trip_id: int, mrid: str) -> None:
    await pool.execute("UPDATE trip_events SET remit_mrid = $1, status = 'matched' WHERE id = $2", mrid, trip_id)


async def resolve_trip(pool: asyncpg.Pool, trip_id: int, resolved_at: datetime) -> None:
    await pool.execute("UPDATE trip_events SET status = 'resolved', resolved_at = $1 WHERE id = $2", resolved_at, trip_id)


async def resolve_open_trips_for_unit(pool: asyncpg.Pool, bm_unit: str, resolved_at: datetime) -> None:
    """The unit's MIL/MEL drop has cleared (engine/fpn.py's detect_trips full
    recovery) -- close every still-open trip row for it."""
    await pool.execute(
        "UPDATE trip_events SET status = 'resolved', resolved_at = $1 WHERE bm_unit = $2 AND status != 'resolved'",
        resolved_at, bm_unit,
    )


async def upsert_remit_revisions(pool: asyncpg.Pool, revisions: list[dict]) -> int:
    """One row per revision, `ON CONFLICT DO UPDATE` only to make repeated
    polls idempotent (a revision's own fields never actually change once
    published) -- never deletes, so the full history stays queryable for
    the worst-behavior profile.
    """
    if not revisions:
        return 0
    ts = datetime.now(timezone.utc)
    cols = [
        "mrid", "revision_number", "message_id", "asset_id", "fuel_type", "event_status",
        "event_start_time", "event_end_time", "normal_capacity", "available_capacity",
        "unavailable_capacity", "publish_time", "cause",
    ]
    col_list = ", ".join(cols + ["fetched_at"])
    placeholders = ", ".join(f"${i + 1}" for i in range(len(cols) + 1))
    update_set = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in ("mrid", "revision_number"))
    await pool.executemany(
        f"""
        INSERT INTO remit_revisions ({col_list}) VALUES ({placeholders})
        ON CONFLICT (mrid, revision_number) DO UPDATE SET {update_set}, fetched_at = excluded.fetched_at
        """,
        [tuple(r.get(c) for c in cols) + (ts,) for r in revisions],
    )
    return len(revisions)


async def fetch_remit_revisions(pool: asyncpg.Pool, mrid: str) -> list[asyncpg.Record]:
    return await pool.fetch("SELECT * FROM remit_revisions WHERE mrid = $1 ORDER BY revision_number", mrid)


async def fetch_worst_behavior_profile(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    """Per bm_unit: how many outages, how many revisions each averaged, and
    how far the expected return time (eventEndTime) slipped between a
    unit's first and last revision of the same outage -- the "worst
    behavior" leaderboard for the new Trips page, ranked worst-first.
    """
    return await pool.fetch(
        """
        WITH per_mrid AS (
            SELECT
                mrid,
                asset_id,
                MAX(fuel_type) AS fuel_type,
                COUNT(*) AS revision_count,
                (array_agg(event_end_time ORDER BY revision_number ASC))[1] AS first_end_time,
                (array_agg(event_end_time ORDER BY revision_number DESC))[1] AS last_end_time,
                (array_agg(event_status ORDER BY revision_number DESC))[1] AS final_status
            FROM remit_revisions
            GROUP BY mrid, asset_id
        )
        SELECT
            asset_id AS bm_unit,
            fuel_type,
            COUNT(*) AS outage_count,
            AVG(revision_count) AS avg_revisions,
            AVG(EXTRACT(EPOCH FROM (last_end_time - first_end_time)) / 3600.0) AS avg_slippage_hours,
            MAX(EXTRACT(EPOCH FROM (last_end_time - first_end_time)) / 3600.0) AS worst_slippage_hours,
            SUM(CASE WHEN last_end_time > first_end_time THEN 1 ELSE 0 END) AS times_slipped_later
        FROM per_mrid
        WHERE first_end_time IS NOT NULL AND last_end_time IS NOT NULL
        GROUP BY asset_id, fuel_type
        ORDER BY avg_slippage_hours DESC NULLS LAST
        """
    )
