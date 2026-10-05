"""Keeps the database from growing for ever.

Citadel recomputes every few seconds and writes per-period rows each time, so a few tables
grow all day (on prod: ~195 MB of per-unit 5-minute deltas, ~130 MB of stack rows, ~100 MB
of per-fuel rows and a 16 MB refresh log after a few weeks). Old days are never read again by
the pages, so this deletes rows older than a per-family retention. A retention of 0 days keeps
that family for ever.

Deliberately NOT pruned: trip_events, remit_revisions (the durable history the worst-behavior
profile is built from), settlement_prices (tiny, and the accuracy history), bm_unit_reference,
natgrid_trade_history (tiny audit trail of NESO revisions).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Rule:
    table: str
    column: str
    kind: str          # "date": a DATE column compared with a date; "time": a timestamp compared with an instant
    family: str        # which retention setting governs it


# (table, column, kind, family). The family names are the Settings attributes `retention_days_<family>`.
RULES: tuple[Rule, ...] = (
    Rule("pricing_stack_rows", "settlement_date", "date", "stack"),
    Rule("pricing_stack_niv_spot_time", "settlement_date", "date", "stack"),
    Rule("pricing_stack_unit_delta_5min", "settlement_date", "date", "stack"),
    Rule("fpn_by_fuel", "settlement_date", "date", "fpn"),
    Rule("fpn_aggregated", "settlement_date", "date", "fpn"),
    Rule("fpn_worst_deviants", "settlement_date", "date", "fpn"),
    Rule("refresh_log", "ts", "time", "log"),
    Rule("trip_telemetry", "spot_time", "time", "telemetry"),
    Rule("natgrid_trades", "end_time", "time", "natgrid"),
    Rule("disbsad_actions", "settlement_date", "date", "natgrid"),
    Rule("fundies_real_time", "settlement_date", "date", "fundies"),
    Rule("fundies_day_ahead", "settlement_date", "date", "fundies"),
    Rule("fundies_entsoe_flows", "settlement_date", "date", "fundies"),
    Rule("fundies_semo_flows", "settlement_date", "date", "fundies"),
    Rule("fundies_imbalngc", "settlement_date", "date", "fundies"),
    Rule("fundies_embedded_forecast", "settlement_date", "date", "fundies"),
    Rule("fundies_epex_da", "settlement_date", "date", "fundies"),
)

# First run waits this long after start-up (the runners are busy backfilling), then it repeats.
FIRST_RUN_DELAY_SECONDS = 600
RUN_EVERY_SECONDS = 6 * 3600


def retention_days(settings, family: str) -> int:
    return int(getattr(settings, f"retention_days_{family}", 0) or 0)


def cutoffs(settings, now: datetime | None = None) -> list[tuple[Rule, date | datetime]]:
    """Each rule whose family has a retention, with the cutoff before which its rows are deleted."""
    now = now or datetime.now(timezone.utc)
    out: list[tuple[Rule, date | datetime]] = []
    for rule in RULES:
        days = retention_days(settings, rule.family)
        if days <= 0:
            continue
        out.append((rule, (now - timedelta(days=days)).date() if rule.kind == "date" else now - timedelta(days=days)))
    return out


def _deleted(status: str) -> int:
    """asyncpg's execute() returns e.g. 'DELETE 123'."""
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0


async def prune_once(pool, settings, now: datetime | None = None) -> dict[str, int]:
    """Delete every row older than its retention. Returns {table: rows deleted} for the tables
    that lost any. A table that fails (say it does not exist yet) is logged and skipped; it never
    stops the others."""
    deleted: dict[str, int] = {}
    for rule, cutoff in cutoffs(settings, now):
        try:
            status = await pool.execute(f"DELETE FROM {rule.table} WHERE {rule.column} < $1", cutoff)  # noqa: S608 -- internal constants
        except Exception:
            logger.warning("retention: pruning %s failed", rule.table, exc_info=True)
            continue
        if n := _deleted(status):
            deleted[rule.table] = n
    return deleted


async def run_retention_loop(pool, settings) -> None:
    await asyncio.sleep(FIRST_RUN_DELAY_SECONDS)
    while True:
        try:
            deleted = await prune_once(pool, settings)
            if deleted:
                logger.info("retention: deleted %s", ", ".join(f"{t}={n}" for t, n in sorted(deleted.items())))
            else:
                logger.info("retention: nothing old enough to delete")
        except Exception:
            logger.exception("retention run failed")
        await asyncio.sleep(RUN_EVERY_SECONDS)
