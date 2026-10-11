from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd
import pytest

from citadel.ingest import demand_backfill as bf
from citadel.ingest import forecast_snapshots as fs

UTC = timezone.utc


def snap(issued, temp_by_hour, point="london"):
    rows = [{"issued_at": issued, "point": point, "ts": pd.Timestamp(f"2025-06-01 {h:02d}:00", tz="UTC"), "temperature_2m": t,
             "cloud_cover": 50.0, "shortwave_radiation": 100.0, "wind_speed_100m": 10.0} for h, t in temp_by_hour.items()]
    return pd.DataFrame(rows)


def stamp(h, m=0):
    return pd.Timestamp(f"2025-06-01 {h:02d}:{m:02d}", tz="UTC")


def test_issue_time_is_the_hour_it_was_taken():
    assert fs.issue_time(datetime(2026, 10, 11, 9, 47, 12, tzinfo=UTC)) == datetime(2026, 10, 11, 9, 0, tzinfo=UTC)


def test_forecast_rows_carry_the_issue_time():
    hourly = {"time": ["2026-10-11T10:00", "2026-10-11T11:00"], "temperature_2m": [12.0, 13.0], "cloud_cover": [40, 50],
              "shortwave_radiation": [200, 250], "wind_speed_100m": [9.0, 11.0]}
    issued = datetime(2026, 10, 11, 9, 0, tzinfo=UTC)
    rows = fs.forecast_rows(issued, "london", hourly)
    assert rows[0] == (issued, "london", datetime(2026, 10, 11, 10, 0, tzinfo=UTC), 12.0, 40, 200, 9.0) and len(rows) == 2


def test_asof_weather_picks_the_newest_issue_at_least_lead_minutes_before_the_period():
    snaps = pd.concat([snap(stamp(8), {12: 10.0, 13: 10.0}), snap(stamp(9), {12: 20.0, 13: 20.0}), snap(stamp(11), {12: 30.0, 13: 30.0})])
    target = [stamp(12)]
    assert fs.asof_weather(snaps, target, 120)["temperature_2m"].iloc[0] == 20.0     # T = 10:00 -> the 09:00 issue
    assert fs.asof_weather(snaps, target, 240)["temperature_2m"].iloc[0] == 10.0     # T = 08:00 -> the 08:00 issue (inclusive)
    assert fs.asof_weather(snaps, target, 0)["temperature_2m"].iloc[0] == 30.0       # T = 12:00 -> the 11:00 issue
    assert pd.isna(fs.asof_weather(snaps, target, 1080)["temperature_2m"].iloc[0])   # nothing was issued that early


def test_asof_weather_ignores_snapshots_issued_after_decision_time():
    early = pd.concat([snap(stamp(8), {12: 10.0, 13: 10.0}), snap(stamp(9), {12: 20.0, 13: 20.0})])
    later = pd.concat([early, snap(stamp(11), {12: 99.0, 13: 99.0})])
    a = fs.asof_weather(early, [stamp(12)], 120)["temperature_2m"].iloc[0]
    b = fs.asof_weather(later, [stamp(12)], 120)["temperature_2m"].iloc[0]
    assert a == b == 20.0


def test_asof_weather_interpolates_hourly_values_onto_the_half_hour_and_averages_points():
    snaps = pd.concat([snap(stamp(9), {12: 10.0, 13: 20.0}, "london"), snap(stamp(9), {12: 20.0, 13: 40.0}, "glasgow")])
    out = fs.asof_weather(snaps, [stamp(12, 30)], 60)
    assert out["temperature_2m"].iloc[0] == pytest.approx(22.5)        # mean of (15, 30)


def test_asof_weather_with_no_snapshots_is_all_nan():
    out = fs.asof_weather(pd.DataFrame(), [stamp(12)], 60)
    assert out.shape == (1, 4) and out.isna().all().all()


def test_weather_day1_chunks_start_when_the_forecasts_do():
    chunks = bf.plan_chunks("weather_day1", date(2022, 1, 1), date(2024, 6, 30))
    assert {c.dataset.split(":")[0] for c in chunks} == {"weather_day1"} and min(c.start for c in chunks) == bf.WEATHER_DAY1_FROM
    assert bf.plan_chunks("weather_day1", date(2022, 1, 1), date(2023, 1, 1)) == []
    assert bf.store_for("weather_day1:london").__name__ == "_store_weather_day1"


def test_day1_payload_is_renamed_to_the_plain_variables():
    payload = {"time": ["2025-03-01T00:00"], **{f"{v}_previous_day1": [1.5] for v in bf.WEATHER_VARS}, "temperature_2m": [99.0]}
    rows = bf.weather_rows("london", bf.day1_hourly(payload))
    assert rows[0][2:] == (1.5, 1.5, 1.5, 1.5)               # the previous-day values, not the plain temperature_2m


def test_embedded_reducer_accepts_records_like_the_sql_endpoint_returns():
    header = "DATE_GMT,TIME_GMT,SETTLEMENT_DATE,SETTLEMENT_PERIOD,EMBEDDED_WIND_FORECAST,EMBEDDED_WIND_CAPACITY,EMBEDDED_SOLAR_FORECAST,EMBEDDED_SOLAR_CAPACITY,Forecast_Datetime"
    line = "2024-01-15T00:00:00Z,12:30:00,2024-01-15T00:00:00Z,25,200,6000,5,15000,2024-01-15T11:01:00Z"
    from_lines = bf.EmbeddedReducer()
    from_lines.feed([header, line])
    from_records = bf.EmbeddedReducer()
    from_records.feed_records([{"SETTLEMENT_DATE": "2024-01-15T00:00:00Z", "SETTLEMENT_PERIOD": 25, "EMBEDDED_WIND_FORECAST": 200,
                                "EMBEDDED_WIND_CAPACITY": 6000, "EMBEDDED_SOLAR_FORECAST": 5, "EMBEDDED_SOLAR_CAPACITY": 15000,
                                "Forecast_Datetime": "2024-01-15T11:01:00Z"}])
    assert sorted(from_lines.rows()) == sorted(from_records.rows()) and from_records.rows()
