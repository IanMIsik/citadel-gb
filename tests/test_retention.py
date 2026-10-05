from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace

from citadel.storage import retention

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def _settings(**over):
    base = dict(retention_days_stack=14, retention_days_fpn=7, retention_days_log=7,
                retention_days_telemetry=30, retention_days_natgrid=90, retention_days_fundies=90)
    base.update(over)
    return SimpleNamespace(**base)


class FakePool:
    def __init__(self, fail_on=None, per_table=3):
        self.calls, self.fail_on, self.per_table = [], fail_on, per_table

    async def execute(self, sql, cutoff):
        table = sql.split()[2]
        if table == self.fail_on:
            raise RuntimeError("no such table")
        self.calls.append((table, sql, cutoff))
        return f"DELETE {self.per_table}"


def test_cutoffs_use_each_familys_own_retention_and_the_right_type():
    by_table = {r.table: c for r, c in retention.cutoffs(_settings(), NOW)}
    assert by_table["pricing_stack_rows"] == date(2026, 9, 21)                       # 14 days, a DATE column
    assert by_table["fpn_by_fuel"] == date(2026, 9, 28)                              # 7 days
    assert by_table["refresh_log"] == datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)   # an instant for a timestamp column
    assert by_table["trip_telemetry"] == datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


def test_a_zero_retention_keeps_that_family_for_ever():
    tables = {r.table for r, _ in retention.cutoffs(_settings(retention_days_stack=0), NOW)}
    assert "pricing_stack_rows" not in tables and "pricing_stack_unit_delta_5min" not in tables
    assert "fpn_by_fuel" in tables


def test_history_tables_are_never_pruned():
    pruned = {r.table for r in retention.RULES}
    assert not pruned & {"trip_events", "remit_revisions", "settlement_prices", "bm_unit_reference", "natgrid_trade_history"}


def test_prune_once_deletes_old_rows_only_and_survives_a_failing_table():
    pool = FakePool(fail_on="refresh_log")
    deleted = asyncio.run(retention.prune_once(pool, _settings(), NOW))
    assert "refresh_log" not in deleted and deleted["pricing_stack_rows"] == 3          # the failure skipped one table, not all
    assert all(sql.startswith("DELETE FROM ") and " WHERE " in sql and " < $1" in sql for _, sql, _ in pool.calls)
    assert asyncio.run(retention.prune_once(FakePool(per_table=0), _settings(), NOW)) == {}
