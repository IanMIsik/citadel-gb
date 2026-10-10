from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from citadel.ingest import demand_backfill as bf

UTC = timezone.utc


def dt(h, m=0, day=15, month=1, year=2024):
    return datetime(year, month, day, h, m, tzinfo=UTC)


def test_picker_is_as_of_each_lead_and_prefers_the_freshest_eligible_forecast():
    target = dt(12)
    p = bf.VintagePicker()
    p.add(target, dt(10, 50), "a")           # 70 min ahead
    p.add(target, dt(11, 1), "b")            # 59 min ahead (fresher than a)
    p.add(target, dt(8, 0), "c")             # 240 min ahead
    p.add(target, dt(9, 10), "d")            # 170 min ahead: the only one old enough for a 2-hour lead
    got = {lead: payload for _t, lead, _p, _s, payload in p.items()}
    # lead 0 and 30: "b" is the latest published at least that long before; lead 60: "a" (b was too late)
    assert got == {0: "b", 30: "b", 60: "a", 120: "d", 240: "c"}


def test_picker_staleness_is_age_beyond_the_cutoff():
    p = bf.VintagePicker()
    p.add(dt(12), dt(10, 50), "a")           # 70 min ahead
    stale = {lead: s for _t, lead, _p, s, _x in p.items()}
    assert stale == {30: 40.0, 60: 10.0}


def test_picker_never_uses_a_forecast_published_after_the_period_started():
    p = bf.VintagePicker()
    p.add(dt(12), dt(12, 1), "after start")
    assert list(p.items()) == []


def test_picker_drops_forecasts_beyond_horizon_and_too_stale():
    p = bf.VintagePicker()
    p.add(dt(12), dt(12) - timedelta(minutes=max(bf.LEAD_MINUTES) + bf.DEFAULT_MAX_STALE_MIN + 1), "too early")
    assert list(p.items()) == []
    wide = bf.VintagePicker(max_stale_min=360)
    wide.add(dt(12), dt(8, 0), "windfor-like")   # 240 min ahead; a 360-minute allowance reaches back to lead 0
    assert {lead for _t, lead, *_ in wide.items()} == {0, 30, 60, 120, 240}


def test_picker_tie_prefers_later_publish_and_streaming_order_does_not_matter():
    for order in ((dt(11, 0), dt(9, 30)), (dt(9, 30), dt(11, 0))):
        p = bf.VintagePicker()
        for pub in order:
            p.add(dt(12), pub, pub.strftime("%H%M"))
        got = {lead: payload for _t, lead, _p, _s, payload in p.items()}
        assert got[60] == "1100" and got[0] == "1100"      # the 11:00 forecast is fresher for both
        assert got[120] == "0930"                          # only 09:30 is old enough for a 2-hour lead


def test_vintage_rows_keeps_national_boundary_only():
    api = [
        {"startTime": "2024-01-15T12:00:00Z", "publishTime": "2024-01-15T11:01:00Z", "boundary": "N", "demand": 30000},
        {"startTime": "2024-01-15T12:00:00Z", "publishTime": "2024-01-15T11:01:00Z", "boundary": "E", "demand": 99},
    ]
    rows = bf.vintage_rows("NDF", api, "demand")
    assert {r[2] for r in rows} == {0, 30} and all(r[0] == "NDF" and r[5] == 30000 for r in rows)


def test_vintage_rows_windfor_has_no_boundary_column_and_allows_longer_staleness():
    api = [{"startTime": "2024-01-15T12:00:00Z", "publishTime": "2024-01-15T08:30:00Z", "generation": 4200}]
    rows = bf.vintage_rows("WINDFOR", api, "generation", max_stale_min=360)
    assert 120 in {r[2] for r in rows} and all(r[5] == 4200 for r in rows)


def test_outturn_rows():
    api = [{"startTime": "2024-01-15T12:00:00Z", "settlementDate": "2024-01-15", "settlementPeriod": 25,
            "initialDemandOutturn": 31000, "initialTransmissionSystemDemandOutturn": 32000}]
    assert bf.outturn_rows(api) == [(dt(12), date(2024, 1, 15), 25, 31000, 32000)]


def test_fuelinst_rows_sum_per_block_and_keep_split():
    api = [
        {"startTime": "2024-01-15T12:00:00Z", "fuelType": "CCGT", "generation": 10000},
        {"startTime": "2024-01-15T12:00:00Z", "fuelType": "INTFR", "generation": -500},
        {"startTime": "2024-01-15T12:05:00Z", "fuelType": "CCGT", "generation": 10100},
        {"startTime": "2024-01-15T12:05:00Z", "fuelType": "WIND", "generation": None},
    ]
    rows = bf.fuelinst_rows(api)
    assert [r[0] for r in rows] == [dt(12), dt(12, 5)]
    assert rows[0][1] == 9500 and json.loads(rows[0][2]) == {"CCGT": 10000.0, "INTFR": -500.0}
    assert rows[1][1] == 10100


def test_neso_historic_rows_handles_missing_columns_and_date_formats():
    recs = [
        {"SETTLEMENT_DATE": "2024-01-15T00:00:00", "SETTLEMENT_PERIOD": 3, "ND": 30000, "TSD": "31000",
         "EMBEDDED_SOLAR_GENERATION": 0, "EMBEDDED_WIND_GENERATION": 1500},
        {"SETTLEMENT_DATE": "01-Jan-2010", "SETTLEMENT_PERIOD": "1", "ND": 40000},
        {"SETTLEMENT_DATE": "01-Jan-23", "SETTLEMENT_PERIOD": "2", "ND": 41000},
        {"SETTLEMENT_DATE": None, "SETTLEMENT_PERIOD": 1},
    ]
    rows = bf.neso_historic_rows(recs)
    assert len(rows) == 3 and rows[2][0] == date(2023, 1, 1)
    cols = ("nd", "tsd", "england_wales_demand", "embedded_wind_generation", "embedded_wind_capacity",
            "embedded_solar_generation", "embedded_solar_capacity", "non_bm_stor", "pump_storage_pumping", "scottish_transfer")
    first = dict(zip(("settlement_date", "settlement_period", *cols), rows[0]))
    assert first["nd"] == 30000 and first["tsd"] == 31000 and first["embedded_wind_generation"] == 1500
    assert first["england_wales_demand"] is None
    assert rows[1][0] == date(2010, 1, 1)


EMBEDDED_HEADER = "DATE_GMT,TIME_GMT,SETTLEMENT_DATE,SETTLEMENT_PERIOD,EMBEDDED_WIND_FORECAST,EMBEDDED_WIND_CAPACITY,EMBEDDED_SOLAR_FORECAST,EMBEDDED_SOLAR_CAPACITY,Forecast_Datetime"


def _emb(period, forecast_at, wind, solar=0):
    return f"2024-01-15T00:00:00Z,12:30:00,2024-01-15T00:00:00Z,{period},{wind},6000,{solar},15000,{forecast_at}"


def test_embedded_reducer_streams_blocks_and_keeps_as_of_vintages():
    # SP25 starts 12:00 UTC in January. Forecasts made 70, 59 and 240 minutes ahead.
    lines = [EMBEDDED_HEADER, _emb(25, "2024-01-15T10:50:00Z", 100), _emb(25, "2024-01-15T11:01:00Z", 200, 5)]
    r = bf.EmbeddedReducer()
    r.feed(lines[:2])                      # header + first row in one block ...
    r.feed(lines[2:] + [_emb(25, "2024-01-15T08:00:00Z", 300), "garbage,row"])   # ... the rest in another
    rows = {row[1]: row for row in r.rows()}
    assert set(rows) == {0, 30, 60, 240}
    assert rows[60][4] == 100.0 and rows[30][4] == 200.0 and rows[0][4] == 200.0 and rows[240][4] == 300.0
    assert rows[30][4:] == (200.0, 6000.0, 5.0, 15000.0)
    assert rows[60][0] == dt(12)


def test_embedded_reducer_ignores_forecasts_far_ahead():
    r = bf.EmbeddedReducer()
    r.feed([EMBEDDED_HEADER, _emb(25, "2024-01-13T12:00:00Z", 100)])
    assert r.rows() == []


def test_plan_chunks_sizes():
    start, end = date(2024, 1, 1), date(2024, 1, 20)
    assert len(bf.plan_chunks("ndf", start, end)) == 20
    out = bf.plan_chunks("outturn", start, end)
    assert [(c.start, c.end) for c in out] == [(date(2024, 1, 1), date(2024, 1, 7)), (date(2024, 1, 8), date(2024, 1, 14)),
                                               (date(2024, 1, 15), date(2024, 1, 20))]
    assert len(bf.plan_chunks("pv_live", start, end)) == 1
    years = bf.plan_chunks("neso_historic", date(2022, 6, 1), date(2024, 2, 1))
    assert [(c.start.year, c.start.month) for c in years] == [(2022, 1), (2023, 1), (2024, 1)]
    weather = bf.plan_chunks("weather", date(2024, 1, 1), date(2024, 6, 1))
    assert {c.dataset for c in weather} == {f"weather:{p}" for p in bf.WEATHER_POINTS}
    assert all((c.end - c.start).days < 120 for c in weather)
    with pytest.raises(ValueError):
        bf.plan_chunks("nope", start, end)


def test_is_finished_skips_old_logged_chunks_but_redoes_recent_ones():
    today = date(2026, 10, 10)
    old = bf.Chunk("ndf", date(2026, 1, 1), date(2026, 1, 1))
    recent = bf.Chunk("ndf", date(2026, 10, 9), date(2026, 10, 9))
    done = {("ndf", old.start): old.end, ("ndf", recent.start): recent.end}
    assert bf.is_finished(old, done, today)
    assert not bf.is_finished(recent, done, today)
    assert not bf.is_finished(bf.Chunk("ndf", date(2025, 1, 1), date(2025, 1, 1)), done, today)
    # logged with a different span than planned now -> not finished
    assert not bf.is_finished(bf.Chunk("outturn", date(2026, 1, 1), date(2026, 1, 7)), {("outturn", date(2026, 1, 1)): date(2026, 1, 3)}, today)
    cal = bf.Chunk("calendar", date(2000, 1, 1), date(2100, 1, 1))
    assert bf.is_finished(cal, {("calendar", cal.start): cal.end}, today)


def test_weather_and_calendar_rows():
    rows = bf.weather_rows("london", {"time": ["2024-01-15T00:00", "2024-01-15T01:00"], "temperature_2m": [3.5, None],
                                      "cloud_cover": [80, 90], "shortwave_radiation": [0, 0], "wind_speed_100m": [20.1, 18.0]})
    assert rows[0] == ("london", dt(0), 3.5, 80, 0, 20.1) and rows[1][2] is None
    cal = bf.calendar_rows({"england-and-wales": {"events": [{"date": "2024-12-25", "title": "Christmas Day"}]},
                            "scotland": {"events": [{"date": "2024-01-02", "title": "2nd January"}]}})
    assert (date(2024, 12, 25), "england-and-wales", "Christmas Day") in cal and len(cal) == 2


def test_pv_live_rows():
    rows = bf.pv_live_rows([{"start_time": "2024-01-15T12:00:00Z", "pv_live": 1.5, "pv_lower_limit": 1.0, "pv_upper_limit": 2.0}])
    assert rows == [(dt(12), 1.5, 1.0, 2.0)]


def test_resolve_datasets():
    assert bf.resolve_datasets("all") == list(bf.ALL_DATASETS)
    assert bf.resolve_datasets("ndf, outturn") == ["ndf", "outturn"]
    with pytest.raises(ValueError):
        bf.resolve_datasets("ndf,bogus")


class _Conn:
    def __init__(self, log):
        self.log = log

    async def execute(self, sql):
        self.log.append(sql)


class _Acquire:
    def __init__(self, log):
        self.conn = _Conn(log)

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self):
        self.log = []

    def acquire(self):
        return _Acquire(self.log)


def _create(monkeypatch, **kwargs):
    import asyncio

    from citadel.storage import db

    pool = _FakePool()

    async def fake_create_pool(*a, **k):
        return pool

    monkeypatch.setattr(db.asyncpg, "create_pool", fake_create_pool)
    asyncio.run(db.create_pool("postgresql://x/y", **kwargs))
    return pool.log


def test_demand_tables_are_only_created_when_asked(monkeypatch):
    plain = _create(monkeypatch)
    assert len(plain) == 1 and "demand_forecast_vintages" not in plain[0]     # production path: base schema only
    dev = _create(monkeypatch, demand_schema=True)
    assert len(dev) == 2 and "demand_forecast_vintages" in dev[1]


def test_base_schema_has_no_demand_forecaster_tables():
    from citadel.storage import db

    assert "demand_" not in db.SCHEMA_PATH.read_text()
    assert "demand_backfill_log" in db.DEMAND_SCHEMA_PATH.read_text()


def test_demand_forecaster_flag_defaults_off():
    from citadel.config import Settings

    assert Settings.model_fields["demand_forecaster_enabled"].default is False
