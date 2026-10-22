"""ENTSO-E scheduled flows: the day-ahead schedule is kept for every pair (tomorrow included) and is replaced, period
by period, by the total (intraday-updated) schedule as soon as ENTSO-E publishes it."""

from __future__ import annotations

import asyncio

import pandas as pd

from citadel.ingest import entsoe_flows
from citadel.storage.db import FUNDIES_ENTSOE_SCHEDULED_COLUMNS, merge_scheduled_flows


def test_tomorrow_is_always_asked_for():
    morning = pd.Timestamp("2026-10-14 08:00", tz="Europe/London")
    days = entsoe_flows.get_entsoe_date_range(morning)
    assert [d.strftime("%Y-%m-%d") for d in days] == ["2026-10-13", "2026-10-14", "2026-10-15"]


def test_day_ahead_value_stands_until_the_intraday_one_arrives():
    rows = merge_scheduled_flows([
        {"settlement_period": 1, "uk_ifa_net": None, "da_uk_ifa_net": 500.0},                 # tomorrow: day-ahead only
        {"settlement_period": 2, "uk_ifa_net": 480.0, "da_uk_ifa_net": 500.0},                # intraday has come through
        {"settlement_period": 3, "uk_ifa_net": float("nan"), "da_uk_ifa_net": 510.0},         # a gap is not a value
        {"settlement_period": 4, "uk_ifa_net": 0.0, "da_uk_ifa_net": 300.0},                  # zero is a real intraday value
        {"settlement_period": 5, "uk_ifa_net": None, "da_uk_ifa_net": None},
    ])
    by = {r["settlement_period"]: r for r in rows}
    assert (by[1]["uk_ifa_net"], by[1]["uk_ifa_net_source"]) == (500.0, "day-ahead")
    assert (by[2]["uk_ifa_net"], by[2]["uk_ifa_net_source"]) == (480.0, "intraday")
    assert (by[3]["uk_ifa_net"], by[3]["uk_ifa_net_source"]) == (510.0, "day-ahead")
    assert (by[4]["uk_ifa_net"], by[4]["uk_ifa_net_source"]) == (0.0, "intraday")
    assert (by[5]["uk_ifa_net"], by[5]["uk_ifa_net_source"]) == (None, None)
    assert by[2]["da_uk_ifa_net"] == 500.0                                                   # the day-ahead figure is kept alongside
    assert all(c + "_source" in by[1] for c in FUNDIES_ENTSOE_SCHEDULED_COLUMNS)


class FakeClient:
    """total schedule: today only; day-ahead: today and tomorrow."""

    def __init__(self):
        self.calls = []

    def query_scheduled_exchanges(self, a, b, start, end, dayahead):
        self.calls.append((a, b, dayahead))
        days = 2 if dayahead else 1
        idx = pd.date_range(start, periods=24 * days, freq="h", tz="Europe/London")
        return pd.Series(100.0 if dayahead else 90.0, index=idx)


def test_each_schedule_is_fetched_on_its_own_and_named_apart():
    client = FakeClient()
    start = pd.Timestamp("2026-10-14", tz="Europe/London")
    total = asyncio.run(entsoe_flows.fetch_pair_net_flow(client, "GB", "NO", start, start + pd.Timedelta(days=2), dayahead=False))
    da = asyncio.run(entsoe_flows.fetch_pair_net_flow(client, "GB", "NO", start, start + pd.Timedelta(days=2), dayahead=True))
    assert total.name == "GB_to_NO_net" and da.name == "da_GB_to_NO_net"
    assert len(total) == 24 and len(da) == 48                                  # day-ahead reaches tomorrow, total does not
    assert {c[2] for c in client.calls} == {False, True}


def test_one_schedule_failing_does_not_cost_the_other():
    class Flaky(FakeClient):
        def query_scheduled_exchanges(self, a, b, start, end, dayahead):
            if not dayahead:
                raise RuntimeError("no total schedule yet")
            return super().query_scheduled_exchanges(a, b, start, end, dayahead)

    start = pd.Timestamp("2026-10-14", tz="Europe/London")
    end = start + pd.Timedelta(days=2)
    assert asyncio.run(entsoe_flows.fetch_pair_net_flow(Flaky(), "GB", "NO", start, end, dayahead=False)) is None
    assert asyncio.run(entsoe_flows.fetch_pair_net_flow(Flaky(), "GB", "NO", start, end, dayahead=True)) is not None
