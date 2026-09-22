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
                    so_flag, acceptance_number, reversal, ap_mult_vol, delta, vol_to_price,
                    disbsad_cost, m_orig_price, total_delta, delta_sign, action_sign,
                    vol_for_price, misik_imb_price, total_misik_price, max_ta, computed_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21)
                """,
                [
                    (
                        r.settlement_date, r.settlement_period, r.bm_unit, r.stor_flag, r.deemed_bo_flag,
                        r.so_flag, r.acceptance_number, r.reversal, r.ap_mult_vol, r.delta, r.vol_to_price,
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
    current settlement period) -- backs the real Zapdos "Forecast Chart",
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
    Zapdos "Delta table"'s own `niv_sp_max` row -- in the original tools it
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


async def fpn_market_gen_vs_adj_fpn_series(pool: asyncpg.Pool, sd: date, periods: list[int]) -> list[asyncpg.Record]:
    """Per-minute, per-fuel-type `market_gen_vs_adj_fpn` across the window
    -- backs the real "Market_gen vs Adj_fpn" chart on the Zapdos home page
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


async def fpn_worst_deviants_current(pool: asyncpg.Pool) -> list[asyncpg.Record]:
    """Every worst-deviant row currently in the window (spans whichever
    settlement periods are still in scope), most recent minute first per
    unit -- there's no single "period" a trader is asking about here,
    unlike the other FPN views.
    """
    return await pool.fetch("SELECT * FROM fpn_worst_deviants ORDER BY bm_unit, spot_time DESC")


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
