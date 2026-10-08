-- Citadel Postgres schema.
--
-- Only the *computed* stack is persisted long-term, same scope as the
-- original notebook (which only ever held raw BOALF/BOD/PN/MEL/MIL/DISBSAD
-- in memory for one rolling window) -- just durable now instead of
-- overwritten every cycle.

CREATE TABLE IF NOT EXISTS bm_unit_reference (
    national_grid_bm_unit  TEXT PRIMARY KEY,
    elexon_bm_unit          TEXT,
    lead_party_name         TEXT,
    bm_unit_type            TEXT,
    generation_capacity_mw  DOUBLE PRECISION,
    fuel_type               TEXT,
    fetched_at              TIMESTAMPTZ NOT NULL
);

-- One row per (settlement_date, settlement_period, bm_unit, acceptance_number,
-- reversal) -- mirrors the notebook's `misik_stack` groupby exactly (see
-- pricingstack_dev.xlsx's `misik_stack` tab, confirmed column-for-column).
CREATE TABLE IF NOT EXISTS pricing_stack_rows (
    settlement_date     DATE NOT NULL,
    settlement_period   INTEGER NOT NULL,
    bm_unit             TEXT NOT NULL,
    stor_flag           BOOLEAN NOT NULL DEFAULT FALSE,
    deemed_bo_flag      BOOLEAN NOT NULL DEFAULT FALSE,
    so_flag             BOOLEAN NOT NULL DEFAULT FALSE,
    cadl_flag           BOOLEAN NOT NULL DEFAULT FALSE,
    acceptance_number   BIGINT NOT NULL DEFAULT 0,
    reversal            DOUBLE PRECISION NOT NULL DEFAULT 1,
    ap_mult_vol         DOUBLE PRECISION NOT NULL,
    delta               DOUBLE PRECISION NOT NULL,
    vol_to_price        DOUBLE PRECISION NOT NULL,
    disbsad_cost        DOUBLE PRECISION NOT NULL DEFAULT 0,
    m_orig_price        DOUBLE PRECISION NOT NULL,
    total_delta         DOUBLE PRECISION NOT NULL,
    delta_sign          DOUBLE PRECISION NOT NULL,
    action_sign         DOUBLE PRECISION NOT NULL,
    vol_for_price       DOUBLE PRECISION,
    misik_imb_price     DOUBLE PRECISION NOT NULL,
    total_misik_price   DOUBLE PRECISION NOT NULL,
    max_ta              TIMESTAMPTZ,
    computed_at         TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (settlement_date, settlement_period, bm_unit, acceptance_number, reversal)
);
CREATE INDEX IF NOT EXISTS idx_pricing_stack_sd_sp ON pricing_stack_rows(settlement_date, settlement_period);

-- The pricing stack's own genuinely per-minute NIV trajectory (one row per
-- settlement_date/settlement_period/spot_time) -- see engine/stack.py's
-- spot_time_niv()/SPOT_NIV_COLUMNS docstring. Distinct from
-- pricing_stack_rows.total_delta above (one MWh figure per whole period):
-- this is the real-time build-up of NIV minute by minute as bids/offers are
-- actually accepted, feeding engine/fpn.py's Delta Chart `delta` line
-- (`niv_spot_time_max` in the original notebook/the reference app naming).
CREATE TABLE IF NOT EXISTS pricing_stack_niv_spot_time (
    settlement_date     DATE NOT NULL,
    settlement_period   INTEGER NOT NULL,
    spot_time           TIMESTAMPTZ NOT NULL,
    niv_spot_time_max   DOUBLE PRECISION NOT NULL,
    computed_at         TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (settlement_date, settlement_period, spot_time)
);
CREATE INDEX IF NOT EXISTS idx_pricing_stack_niv_spot_time_sd_sp ON pricing_stack_niv_spot_time(settlement_date, settlement_period);

-- The pricing stack's own real, reversal/CADL-aware delta, kept per BM unit
-- and bucketed onto FUELINST's own 5-minute grid instead of collapsed to
-- one period-total figure -- see engine/stack.py's
-- spot_time_bm_unit_delta_5min()/UNIT_DELTA_5MIN_COLUMNS docstring.
-- engine/fpn.py's pricing_stack_delta_by_fuel_5min() does the bmUnit ->
-- fuel-type join and per-fuel sum, feeding the Real Time Generation
-- table's `_d` column with an actual per-5-minute figure instead of the
-- same period-total value repeated across every bucket in that period.
CREATE TABLE IF NOT EXISTS pricing_stack_unit_delta_5min (
    settlement_date     DATE NOT NULL,
    settlement_period   INTEGER NOT NULL,
    bm_unit             TEXT NOT NULL,
    start_time          TIMESTAMPTZ NOT NULL,
    delta               DOUBLE PRECISION NOT NULL,
    computed_at         TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (settlement_date, settlement_period, bm_unit, start_time)
);
CREATE INDEX IF NOT EXISTS idx_pricing_stack_unit_delta_5min_sd_sp ON pricing_stack_unit_delta_5min(settlement_date, settlement_period);

-- Elexon's real, official settlement system price per period -- the ground
-- truth this project's own `total_misik_price` is checked against.
CREATE TABLE IF NOT EXISTS settlement_prices (
    settlement_date     DATE NOT NULL,
    settlement_period   INTEGER NOT NULL,
    system_sell_price   DOUBLE PRECISION,
    net_imbalance_volume DOUBLE PRECISION,
    fetched_at          TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (settlement_date, settlement_period)
);

-- FPN Analytics -- see engine/fpn.py. Same scope decision as
-- pricing_stack_rows: only the current rolling window is kept (delete +
-- reinsert per recompute), no unbounded history.

CREATE TABLE IF NOT EXISTS fpn_by_fuel (
    settlement_date     DATE NOT NULL,
    settlement_period   INTEGER NOT NULL,
    spot_time           TIMESTAMPTZ NOT NULL,
    fuel_type           TEXT NOT NULL,
    fpn_spot_vol        DOUBLE PRECISION,
    delta               DOUBLE PRECISION,
    mel_spot_vol        DOUBLE PRECISION,
    mel_reduced_fpn     DOUBLE PRECISION,
    mil_spot_vol        DOUBLE PRECISION,
    mil_increased_fpn   DOUBLE PRECISION,
    adjusted_fpn        DOUBLE PRECISION,
    fuelinst_generation DOUBLE PRECISION,
    market_gen          DOUBLE PRECISION,
    market_gen_vs_fpn   DOUBLE PRECISION,
    market_gen_vs_adj_fpn DOUBLE PRECISION,
    mel_mil_drop        DOUBLE PRECISION,
    computed_at         TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (settlement_date, settlement_period, spot_time, fuel_type)
);
CREATE INDEX IF NOT EXISTS idx_fpn_by_fuel_sd_sp ON fpn_by_fuel(settlement_date, settlement_period);

CREATE TABLE IF NOT EXISTS fpn_worst_deviants (
    bm_unit             TEXT NOT NULL,
    settlement_date     DATE NOT NULL,
    settlement_period   INTEGER NOT NULL,
    spot_time           TIMESTAMPTZ NOT NULL,
    fuel_type           TEXT,
    fpn_spot_vol        DOUBLE PRECISION,
    adjusted_fpn        DOUBLE PRECISION,
    mel_mil_drop        DOUBLE PRECISION,
    current_mel         DOUBLE PRECISION,
    current_mil         DOUBLE PRECISION,
    mel_downside        DOUBLE PRECISION,
    mil_upside          DOUBLE PRECISION,
    mel_mil_downside    DOUBLE PRECISION,
    computed_at         TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (bm_unit, settlement_date, settlement_period, spot_time)
);
CREATE INDEX IF NOT EXISTS idx_fpn_worst_deviants_sd_sp ON fpn_worst_deviants(settlement_date, settlement_period);

-- Plant trip detector: one durable row per detected trip (edge-triggered,
-- see engine/fpn.py's detect_trips()) -- append-only history, not a
-- rolling window, so the worst-behavior profile has something to look
-- back over.
CREATE TABLE IF NOT EXISTS trip_events (
    id                  BIGSERIAL PRIMARY KEY,
    bm_unit             TEXT NOT NULL,
    fuel_type           TEXT,
    settlement_date     DATE NOT NULL,
    settlement_period   INTEGER NOT NULL,
    drop_mw             DOUBLE PRECISION NOT NULL,
    detected_at         TIMESTAMPTZ NOT NULL,
    status              TEXT NOT NULL DEFAULT 'open',  -- open | matched | resolved
    remit_mrid          TEXT,
    resolved_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_trip_events_bm_unit ON trip_events(bm_unit, detected_at);
CREATE INDEX IF NOT EXISTS idx_trip_events_status ON trip_events(status);

-- One row per REMIT revision (never overwritten -- see remit.py) so the
-- worst-behavior profile can measure how far a unit's own eventEndTime
-- estimate moved between its first and its last revision.
CREATE TABLE IF NOT EXISTS remit_revisions (
    mrid                  TEXT NOT NULL,
    revision_number       INTEGER NOT NULL,
    message_id            BIGINT NOT NULL,
    asset_id              TEXT NOT NULL,
    fuel_type             TEXT,
    event_status          TEXT,
    event_start_time      TIMESTAMPTZ,
    event_end_time        TIMESTAMPTZ,
    normal_capacity       DOUBLE PRECISION,
    available_capacity    DOUBLE PRECISION,
    unavailable_capacity  DOUBLE PRECISION,
    publish_time          TIMESTAMPTZ,
    cause                 TEXT,
    fetched_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (mrid, revision_number)
);
CREATE INDEX IF NOT EXISTS idx_remit_revisions_asset ON remit_revisions(asset_id, event_start_time);

-- One row per spot_time -- generation-vs-plan, demand-side error, and the
-- niv_estimate decision figure broadcast onto every minute of its
-- settlement period (see engine/fpn.py:compute's own docstring).
CREATE TABLE IF NOT EXISTS fpn_aggregated (
    settlement_date     DATE NOT NULL,
    settlement_period   INTEGER NOT NULL,
    spot_time           TIMESTAMPTZ NOT NULL,
    fpn_spot_vol        DOUBLE PRECISION,
    fuelinst_generation DOUBLE PRECISION,
    market_gen          DOUBLE PRECISION,
    market_gen_vs_fpn   DOUBLE PRECISION,
    market_gen_vs_adj_fpn DOUBLE PRECISION,
    adjusted_fpn        DOUBLE PRECISION,
    delta               DOUBLE PRECISION,
    misco_indo          DOUBLE PRECISION,
    misco_ndf           DOUBLE PRECISION,
    misco_indo_vs_ndf   DOUBLE PRECISION,
    misco_da_adj_ndf    DOUBLE PRECISION,
    dmd_risk            DOUBLE PRECISION,
    area_under_curve    DOUBLE PRECISION,
    auc_av              DOUBLE PRECISION,
    delta_av            DOUBLE PRECISION,
    niv_error           DOUBLE PRECISION,
    unexp_delta         DOUBLE PRECISION,
    wind_deviation      DOUBLE PRECISION,
    other_gen_deviation DOUBLE PRECISION,
    niv_estimate        DOUBLE PRECISION,
    spot_indo           DOUBLE PRECISION,
    spot_latest_ndf     DOUBLE PRECISION,
    niv_sp_max          DOUBLE PRECISION,
    computed_at         TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (settlement_date, settlement_period, spot_time)
);
CREATE INDEX IF NOT EXISTS idx_fpn_aggregated_sd_sp ON fpn_aggregated(settlement_date, settlement_period);
-- Added after the table's first release -- ALTER for databases that
-- already have the old column set (CREATE TABLE IF NOT EXISTS above is a
-- no-op against them). spot_indo/spot_latest_ndf: the raw (uncobbled)
-- INDO/NDF lines the real reference app "Forecast Chart" plots alongside the
-- misco_* smoothed ones. niv_sp_max: the pricing stack's own real,
-- already-settled NIV for the period (`total_delta` in pricing_stack_rows)
-- -- the notebook's own live NIV pickle, read in-process instead.
ALTER TABLE fpn_aggregated ADD COLUMN IF NOT EXISTS spot_indo DOUBLE PRECISION;
ALTER TABLE fpn_aggregated ADD COLUMN IF NOT EXISTS spot_latest_ndf DOUBLE PRECISION;
ALTER TABLE fpn_aggregated ADD COLUMN IF NOT EXISTS niv_sp_max DOUBLE PRECISION;

-- Real generation by fuel type, split into market-driven vs BM-action-
-- driven portions (see engine/fpn.py:compute_generation_by_fuel) -- a
-- normalized replacement for the Fuelinst notebook's wide `_r/_m/_d` pivot.
CREATE TABLE IF NOT EXISTS fpn_generation_by_fuel (
    ts                  TIMESTAMPTZ NOT NULL,
    settlement_period   INTEGER,
    fuel_type           TEXT NOT NULL,
    real_gen            DOUBLE PRECISION,
    market_gen          DOUBLE PRECISION,
    delta_gen           DOUBLE PRECISION,
    computed_at         TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (ts, fuel_type)
);
ALTER TABLE fpn_generation_by_fuel ADD COLUMN IF NOT EXISTS settlement_period INTEGER;
CREATE INDEX IF NOT EXISTS idx_fpn_generation_by_fuel_ts ON fpn_generation_by_fuel(ts);

-- Fundies dashboard (engine/fundies.py) -- one row per settlement period,
-- matching the notebook's own two Google Sheet tabs (fundies_rt_data,
-- da_data). Rebuilt wholesale on every FundiesRunner recompute (delete +
-- reinsert the day's rows via db.replace_fundies_period_rows), same
-- pattern as replace_fpn_period_rows.
CREATE TABLE IF NOT EXISTS fundies_real_time (
    settlement_date       DATE NOT NULL,
    settlement_period     INTEGER NOT NULL,
    latest_ndf            DOUBLE PRECISION,
    latestwindfor         DOUBLE PRECISION,
    indo                  DOUBLE PRECISION,
    wind_ot               DOUBLE PRECISION,
    shut_volume           DOUBLE PRECISION,
    total_wind_outturn    DOUBLE PRECISION,
    itso                  DOUBLE PRECISION,
    pv_live               DOUBLE PRECISION,
    net_imbalance_volume  DOUBLE PRECISION,
    system_buy_price      DOUBLE PRECISION,
    buy_price_adjustment  DOUBLE PRECISION,
    sell_price_adjustment DOUBLE PRECISION,
    market_index_price    DOUBLE PRECISION,
    market_index_volume   DOUBLE PRECISION,
    ng_vol                DOUBLE PRECISION,
    -- Real (metered, FUELHH-derived) interconnector flows, for the
    -- Interconnector Graphs page's "real vs scheduled" overlay -- see
    -- engine/fundies.py's interconnector_real_flows() docstring.
    real_ifa_net          DOUBLE PRECISION,
    real_ifa2_net         DOUBLE PRECISION,
    real_eleclink_net     DOUBLE PRECISION,
    real_nl_net           DOUBLE PRECISION,
    real_be_net           DOUBLE PRECISION,
    real_norway_net       DOUBLE PRECISION,
    real_dk_net           DOUBLE PRECISION,
    real_ew_net           DOUBLE PRECISION,
    real_moyle_net        DOUBLE PRECISION,
    real_grnl_net         DOUBLE PRECISION,
    imbalngc              DOUBLE PRECISION,
    computed_at           TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (settlement_date, settlement_period)
);
ALTER TABLE fundies_real_time ADD COLUMN IF NOT EXISTS wind_ot DOUBLE PRECISION;
ALTER TABLE fundies_real_time ADD COLUMN IF NOT EXISTS shut_volume DOUBLE PRECISION;
ALTER TABLE fundies_real_time ADD COLUMN IF NOT EXISTS real_ifa_net DOUBLE PRECISION;
ALTER TABLE fundies_real_time ADD COLUMN IF NOT EXISTS real_ifa2_net DOUBLE PRECISION;
ALTER TABLE fundies_real_time ADD COLUMN IF NOT EXISTS real_eleclink_net DOUBLE PRECISION;
ALTER TABLE fundies_real_time ADD COLUMN IF NOT EXISTS real_nl_net DOUBLE PRECISION;
ALTER TABLE fundies_real_time ADD COLUMN IF NOT EXISTS real_be_net DOUBLE PRECISION;
ALTER TABLE fundies_real_time ADD COLUMN IF NOT EXISTS real_norway_net DOUBLE PRECISION;
ALTER TABLE fundies_real_time ADD COLUMN IF NOT EXISTS real_dk_net DOUBLE PRECISION;
ALTER TABLE fundies_real_time ADD COLUMN IF NOT EXISTS real_ew_net DOUBLE PRECISION;
ALTER TABLE fundies_real_time ADD COLUMN IF NOT EXISTS real_moyle_net DOUBLE PRECISION;
ALTER TABLE fundies_real_time ADD COLUMN IF NOT EXISTS real_grnl_net DOUBLE PRECISION;

CREATE TABLE IF NOT EXISTS fundies_day_ahead (
    settlement_date               DATE NOT NULL,
    settlement_period             INTEGER NOT NULL,
    da_ndf                        DOUBLE PRECISION,
    da_windfor                    DOUBLE PRECISION,
    eleclink_net                  DOUBLE PRECISION,
    uk_ifa_net                    DOUBLE PRECISION,
    uk_ifa2_net                   DOUBLE PRECISION,
    uk_nl_net                     DOUBLE PRECISION,
    uk_be_net                     DOUBLE PRECISION,
    uk_norway_net                 DOUBLE PRECISION,
    uk_dk_net                     DOUBLE PRECISION,
    nuke_214                      DOUBLE PRECISION,
    intew_net                     DOUBLE PRECISION,
    intmoyle_net                  DOUBLE PRECISION,
    intgrnl_net                   DOUBLE PRECISION,
    embedded_wind_forecast        DOUBLE PRECISION,
    embedded_solar_forecast       DOUBLE PRECISION,
    da_price                      DOUBLE PRECISION,
    da_volume                     DOUBLE PRECISION,
    -- reference-app-style derived rows (see engine/fundies.py:build_derived_rows) --
    -- ride along on this table since they need both real-time and
    -- day-ahead inputs together, same as the real reference app table renders
    -- them as rows alongside everything else.
    indo_da_ndf_delta             DOUBLE PRECISION,
    fake_wind_ot_da_winfor_delta  DOUBLE PRECISION,
    domestic_tight_delta          DOUBLE PRECISION,
    interconnector_ng             DOUBLE PRECISION,
    latest_resid                  DOUBLE PRECISION,
    computed_at                   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (settlement_date, settlement_period)
);

-- Upsert-on-(settlement_date, settlement_period) caches, one per
-- independently-refreshing Fundies ingest source -- these replace the
-- notebook's own Drive pickles (scheduled_flows.pkl, semo_flows.pkl,
-- imbalngc_data.pkl, natgrid_embedded_data.pkl, epex_da_data.pkl) with
-- real durable upserts: "newest write wins, survive a failed fetch" via
-- ON CONFLICT DO UPDATE instead of a combine_first/dedup-and-resave cycle
-- against a file. FundiesRunner reads these every recompute regardless of
-- which dataset's own interval last fired, so a slow-refreshing source
-- (e.g. the embedded-forecast CSV, every 5 minutes) doesn't blank out
-- between its own ticks.
CREATE TABLE IF NOT EXISTS fundies_entsoe_flows (
    settlement_date   DATE NOT NULL,
    settlement_period INTEGER NOT NULL,
    eleclink_net      DOUBLE PRECISION,
    uk_ifa_net        DOUBLE PRECISION,
    uk_ifa2_net       DOUBLE PRECISION,
    uk_nl_net         DOUBLE PRECISION,
    uk_be_net         DOUBLE PRECISION,
    uk_norway_net     DOUBLE PRECISION,
    uk_dk_net         DOUBLE PRECISION,
    updated_at        TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (settlement_date, settlement_period)
);

CREATE TABLE IF NOT EXISTS fundies_semo_flows (
    settlement_date   DATE NOT NULL,
    settlement_period INTEGER NOT NULL,
    intew_net         DOUBLE PRECISION,
    intmoyle_net      DOUBLE PRECISION,
    intgrnl_net       DOUBLE PRECISION,
    updated_at        TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (settlement_date, settlement_period)
);

CREATE TABLE IF NOT EXISTS fundies_imbalngc (
    settlement_date   DATE NOT NULL,
    settlement_period INTEGER NOT NULL,
    imbalngc          DOUBLE PRECISION,
    updated_at        TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (settlement_date, settlement_period)
);

CREATE TABLE IF NOT EXISTS fundies_embedded_forecast (
    settlement_date         DATE NOT NULL,
    settlement_period       INTEGER NOT NULL,
    embedded_wind_forecast  DOUBLE PRECISION,
    embedded_solar_forecast DOUBLE PRECISION,
    updated_at              TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (settlement_date, settlement_period)
);

-- settlement_date here is the auction's *delivery* date (the day the
-- scraped prices/volumes apply to) -- also what epex.should_fetch_da_prices()
-- checks against to decide whether a delivery date's auction is already
-- stored and the scrape can be skipped this cycle.
CREATE TABLE IF NOT EXISTS fundies_epex_da (
    settlement_date   DATE NOT NULL,
    settlement_period INTEGER NOT NULL,
    da_price          DOUBLE PRECISION,
    da_volume         DOUBLE PRECISION,
    updated_at        TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (settlement_date, settlement_period)
);

CREATE TABLE IF NOT EXISTS refresh_log (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT NOT NULL,  -- 'iris' | 'rest'
    dataset     TEXT NOT NULL,
    ok          BOOLEAN NOT NULL,
    note        TEXT,
    ts          TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_refresh_log_ts ON refresh_log(ts);

-- National Grid trades, kept locally from both sources so a failed or revised
-- fetch never loses them and the two can be compared (see
-- ingest/natgrid_store.py). natgrid_trades is NESO's GTMA trade list, one row
-- per trade block (volume_mw held from start_time to end_time);
-- natgrid_trade_history keeps the previous version whenever NESO revises one.
CREATE TABLE IF NOT EXISTS natgrid_trades (
    id              TEXT PRIMARY KEY,
    start_time      TIMESTAMPTZ NOT NULL,
    end_time        TIMESTAMPTZ NOT NULL,
    volume_mw       DOUBLE PRECISION NOT NULL,
    price           DOUBLE PRECISION,
    cost            DOUBLE PRECISION,
    so_flag         TEXT,
    reason          TEXT,
    source_updated  TIMESTAMPTZ,
    first_seen_at   TIMESTAMPTZ NOT NULL,
    last_seen_at    TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_natgrid_trades_window ON natgrid_trades(start_time, end_time);

CREATE TABLE IF NOT EXISTS natgrid_trade_history (
    history_id      BIGSERIAL PRIMARY KEY,
    id              TEXT NOT NULL,
    changed_at      TIMESTAMPTZ NOT NULL,
    start_time      TIMESTAMPTZ,
    end_time        TIMESTAMPTZ,
    volume_mw       DOUBLE PRECISION,
    price           DOUBLE PRECISION,
    so_flag         TEXT,
    reason          TEXT
);
CREATE INDEX IF NOT EXISTS idx_natgrid_trade_history_id ON natgrid_trade_history(id);

-- Elexon DISBSAD actions (volume is MWh for the settlement period).
CREATE TABLE IF NOT EXISTS disbsad_actions (
    settlement_date   DATE NOT NULL,
    settlement_period INTEGER NOT NULL,
    action_id         INTEGER NOT NULL,
    volume            DOUBLE PRECISION,
    cost              DOUBLE PRECISION,
    price             DOUBLE PRECISION,
    so_flag           BOOLEAN,
    stor_flag         BOOLEAN,
    party_id          TEXT,
    asset_id          TEXT,
    service           TEXT,
    is_tendered       BOOLEAN,
    first_seen_at     TIMESTAMPTZ NOT NULL,
    last_seen_at      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (settlement_date, settlement_period, action_id)
);

-- Per-unit FPN / MEL / adjusted-FPN points for every tripped (or just
-- recovered) plant, saved as engine/fpn.py's worst_behaviour_series() produces
-- them (5-minute points, past and published plan). Without this the Plant
-- Trips page could only graph a trip while it was inside the rolling window;
-- with it, any trip keeps its chart. Later cycles overwrite the same minute,
-- so forecast points track the plant's latest plan.
CREATE TABLE IF NOT EXISTS trip_telemetry (
    bm_unit         TEXT NOT NULL,
    spot_time       TIMESTAMPTZ NOT NULL,
    fuel_type       TEXT,
    fpn             DOUBLE PRECISION,
    mel             DOUBLE PRECISION,
    adjusted_fpn    DOUBLE PRECISION,
    updated_at      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (bm_unit, spot_time)
);
ALTER TABLE trip_telemetry ADD COLUMN IF NOT EXISTS mil DOUBLE PRECISION;
