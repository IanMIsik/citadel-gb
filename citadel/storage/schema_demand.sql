-- Demand forecaster tables. NOT part of schema.sql on purpose: they are created only when
-- DEMAND_FORECASTER_ENABLED=true (dev) or by `citadel backfill-demand`, so a production
-- database never gets them by accident.

-- ---------------------------------------------------------------------------
-- Demand forecaster history (filled by `citadel backfill-demand`, see
-- ingest/demand_backfill.py). None of these tables is pruned by retention: a
-- model needs years of history. They exist so a demand-error model can be
-- trained on the forecast that was *available at the time* (a "vintage"),
-- not on the latest revised value.
-- ---------------------------------------------------------------------------

-- Elexon NDF / TSDF / WINDFOR vintages, kept only at a few lead times.
-- For lead_min = L the row is the latest forecast published at least L minutes
-- before the target period started (what a model running L minutes ahead could
-- have seen). staleness_min is how much older than that cut-off it is, in
-- minutes; a fresher value wins when two chunks offer the same slot.
CREATE TABLE IF NOT EXISTS demand_forecast_vintages (
    dataset       TEXT NOT NULL,
    target_start  TIMESTAMPTZ NOT NULL,
    lead_min      INTEGER NOT NULL,
    publish_time  TIMESTAMPTZ NOT NULL,
    staleness_min  DOUBLE PRECISION NOT NULL,
    value         DOUBLE PRECISION,
    PRIMARY KEY (dataset, target_start, lead_min)
);

-- Initial national demand outturn (INDO) and initial transmission system
-- demand outturn (ITSDO), as first published by Elexon.
CREATE TABLE IF NOT EXISTS demand_outturn (
    target_start       TIMESTAMPTZ PRIMARY KEY,
    settlement_date    DATE NOT NULL,
    settlement_period  INTEGER NOT NULL,
    indo               DOUBLE PRECISION,
    itsdo              DOUBLE PRECISION
);

-- FUELINST at its native 5-minute step: total across fuel types and the split.
CREATE TABLE IF NOT EXISTS demand_fuelinst_5min (
    start_time  TIMESTAMPTZ PRIMARY KEY,
    total_mw    DOUBLE PRECISION,
    by_fuel     JSONB NOT NULL
);

-- NESO "Historic Demand Data": final ND/TSD plus *estimated* embedded wind and
-- solar generation and capacity.
CREATE TABLE IF NOT EXISTS demand_neso_historic (
    settlement_date            DATE NOT NULL,
    settlement_period          INTEGER NOT NULL,
    nd                         DOUBLE PRECISION,
    tsd                        DOUBLE PRECISION,
    england_wales_demand       DOUBLE PRECISION,
    embedded_wind_generation   DOUBLE PRECISION,
    embedded_wind_capacity     DOUBLE PRECISION,
    embedded_solar_generation  DOUBLE PRECISION,
    embedded_solar_capacity    DOUBLE PRECISION,
    non_bm_stor                DOUBLE PRECISION,
    pump_storage_pumping       DOUBLE PRECISION,
    scottish_transfer          DOUBLE PRECISION,
    PRIMARY KEY (settlement_date, settlement_period)
);

-- NESO embedded wind/solar forecast archive, vintaged like demand_forecast_vintages.
CREATE TABLE IF NOT EXISTS demand_embedded_vintages (
    target_start     TIMESTAMPTZ NOT NULL,
    lead_min         INTEGER NOT NULL,
    publish_time     TIMESTAMPTZ NOT NULL,
    staleness_min     DOUBLE PRECISION NOT NULL,
    wind_forecast    DOUBLE PRECISION,
    wind_capacity    DOUBLE PRECISION,
    solar_forecast   DOUBLE PRECISION,
    solar_capacity   DOUBLE PRECISION,
    PRIMARY KEY (target_start, lead_min)
);

-- Sheffield Solar PV_Live national outturn; ts is the timestamp PV_Live publishes
-- (its half-hour convention is checked when features are built).
CREATE TABLE IF NOT EXISTS demand_pv_live (
    ts        TIMESTAMPTZ PRIMARY KEY,
    pv_live   DOUBLE PRECISION,
    pv_lower  DOUBLE PRECISION,
    pv_upper  DOUBLE PRECISION
);

-- Hourly weather at a few GB population centres (Open-Meteo historical forecast).
CREATE TABLE IF NOT EXISTS demand_weather (
    point                TEXT NOT NULL,
    ts                   TIMESTAMPTZ NOT NULL,
    temperature_2m       DOUBLE PRECISION,
    cloud_cover          DOUBLE PRECISION,
    shortwave_radiation  DOUBLE PRECISION,
    wind_speed_100m      DOUBLE PRECISION,
    PRIMARY KEY (point, ts)
);

CREATE TABLE IF NOT EXISTS demand_calendar (
    day     DATE NOT NULL,
    region  TEXT NOT NULL,
    title   TEXT,
    PRIMARY KEY (day, region)
);

-- Which backfill chunks finished, so a re-run only fetches what is missing.
CREATE TABLE IF NOT EXISTS demand_backfill_log (
    dataset       TEXT NOT NULL,
    chunk_start   DATE NOT NULL,
    chunk_end     DATE NOT NULL,
    row_count     INTEGER NOT NULL,
    completed_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (dataset, chunk_start)
);
