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
    computed_at         TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (settlement_date, settlement_period, spot_time)
);
CREATE INDEX IF NOT EXISTS idx_fpn_aggregated_sd_sp ON fpn_aggregated(settlement_date, settlement_period);

-- Real generation by fuel type, split into market-driven vs BM-action-
-- driven portions (see engine/fpn.py:compute_generation_by_fuel) -- a
-- normalized replacement for the Fuelinst notebook's wide `_r/_m/_d` pivot.
CREATE TABLE IF NOT EXISTS fpn_generation_by_fuel (
    ts                  TIMESTAMPTZ NOT NULL,
    fuel_type           TEXT NOT NULL,
    real_gen            DOUBLE PRECISION,
    market_gen          DOUBLE PRECISION,
    delta_gen           DOUBLE PRECISION,
    computed_at         TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (ts, fuel_type)
);
CREATE INDEX IF NOT EXISTS idx_fpn_generation_by_fuel_ts ON fpn_generation_by_fuel(ts);

CREATE TABLE IF NOT EXISTS refresh_log (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT NOT NULL,  -- 'iris' | 'rest'
    dataset     TEXT NOT NULL,
    ok          BOOLEAN NOT NULL,
    note        TEXT,
    ts          TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_refresh_log_ts ON refresh_log(ts);
