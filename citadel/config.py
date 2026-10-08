"""Environment-based settings. Reads a local .env if present (see
.env.template) but never requires one -- every field has a default that
runs the app in REST-only mode against a local Postgres.

`CITADEL_ENV_FILE` (a real OS env var, not a .env entry -- it has to exist
before pydantic-settings decides which file to read) points this at a
different file entirely, e.g. `.env.dev` -- see .env.dev.template. This is
how a dev instance runs against its own database/port, with its own
feature-toggle values (see `reversal_side_fix_enabled` below), alongside
prod without either one's config touching the other.
"""

from __future__ import annotations

import os

from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = os.environ.get("CITADEL_ENV_FILE", ".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql://citadel:citadel@localhost:5432/citadel"

    iris_tenant_id: str = "4203b7a0-7773-4de5-b830-8b263a20426e"
    iris_namespace: str = "elexon-insights-iris"
    iris_client_id: str = ""
    iris_client_secret: str = ""
    iris_queue_name: str = ""

    rest_poll_interval_seconds: int = 5

    # Size of the shared recompute process pool (see api/app.py). 0 means
    # "one worker per CPU" (ProcessPoolExecutor's own default); set a small
    # number on a memory-constrained host -- each worker is ~130 MB.
    process_pool_workers: int = 0

    # Days of National Grid trades (NESO and Elexon DISBSAD) pulled into the local
    # database once at start, so the Natgrid page has history straight away. 0 = off.
    natgrid_backfill_days: int = 14

    # How many days of each table family to keep (storage/retention.py deletes older rows every
    # few hours so the database cannot grow for ever). 0 keeps that family for ever. The trip,
    # REMIT and settlement-price history tables are never pruned.
    retention_days_stack: int = 14       # pricing stack rows, NIV and per-unit deltas
    retention_days_fpn: int = 7          # FPN per-fuel, aggregated and worst-deviants rows
    retention_days_log: int = 7          # the refresh log
    retention_days_telemetry: int = 30   # per-unit trip telemetry
    retention_days_natgrid: int = 90     # stored NESO trades and DISBSAD actions
    retention_days_fundies: int = 90     # the Fundies cache tables

    # ENTSO-E Transparency Platform key -- register free at
    # https://transparency.entsoe.eu/. Needed only for the Fundies
    # dashboard's interconnector-flow rows/graphs (ingest/entsoe_flows.py);
    # everything else on that dashboard works without it. Empty means
    # those rows/graphs show "--" rather than failing the whole page.
    entsoe_key: str = ""

    # Purely a UI label ("dev"/"prod") -- lets the frontend show a badge so
    # it's obvious at a glance which instance a browser tab is pointed at
    # when both run side by side (see /api/health, web/app.js, web/fpn.js).
    # Not read anywhere else; changing it never affects engine behaviour.
    environment_label: str = "prod"

    # Currently a NO-OP -- see engine/stack.py's build_marginal_deltas()
    # docstring for the full history. Targets a real bug (T_FERRB-1
    # acceptance 14433, SP14 2026-09-24: Elexon prices it as an offer, this
    # engine as a wrong-sign bid) but THREE different fix attempts were each
    # confirmed live to make the computed NIV worse against Elexon's real
    # netImbalanceVolume, not better. Root cause turned out not to be a
    # missing pairId or bad band geometry (both ruled out empirically) but
    # that Elexon's real per-acceptance settlement volume follows an
    # allocation rule not reverse-engineerable from BOALF/BOD/PN alone.
    # Left off in both .env and .env.dev. Do not re-enable without either
    # the actual documented BSC settlement algorithm for per-acceptance
    # volume, or an ISPSTACK-based reconciliation pass (see that docstring).
    reversal_side_fix_enabled: bool = False

    # Opt-in, defaulted off -- see engine/fpn.py's fuel_type_reference()
    # docstring. Units tagged BATTERIES/LOAD RESPONSE/GAS/DIESEL/SOLAR/
    # generic INTERCONNECTOR in bm_unit_reference are otherwise silently
    # excluded from the FPN dashboard (FUELINST has no category for them),
    # not folded into OTHER -- confirmed live 2026-09-25 all 110 such units
    # are genuine, currently-registered BM units with no better-fitting
    # category available from either Elexon's live reference or the BMU
    # fuel type spreadsheet. Enabled in .env.dev only until validated there.
    fpn_other_fallback_enabled: bool = False

    # Opt-in, defaulted off -- see engine/stack.py's blend_disbsad()
    # docstring. Confirmed live 2026-09-25, SP39: 34 separate DISBSAD
    # actions (GBP212.50-235.00/MWh) get pre-summed into one blended-average
    # row before PAR Tagging ever sees them, discarding exactly the
    # per-action price detail needed to price PAR's 1 MWh boundary
    # correctly when it falls inside that combined block. Enabled in
    # .env.dev only until validated against real settlement periods there.
    disbsad_disaggregation_enabled: bool = False

    # Opt-in, dev-only until validated: price the stack from raw acceptances measured against the previous
    # acceptance in force and split across bid-offer bands (engine/acceptance_volumes.py).
    acceptance_model_enabled: bool = False

    # Opt-in, defaulted off -- the BM Stack page (engine/bm_stack.py,
    # /bm-stack): untouched bids/offers available to be called. Costs an
    # extra pandas pass inside every FPN recompute, so it only runs where
    # enabled. Enabled in .env.dev first.
    bm_stack_enabled: bool = False

    # Web hardening (api/security.py). /docs, /redoc and /openapi.json list every endpoint, so they
    # stay off unless this is set (dev only). Rate limit is per client address on /api/*; websocket
    # cap is concurrent sockets per client address. 0 turns a limit off.
    api_docs_enabled: bool = False
    rate_limit_per_minute: int = 600
    ws_max_per_ip: int = 20

    @property
    def iris_configured(self) -> bool:
        return bool(self.iris_client_id and self.iris_client_secret and self.iris_queue_name)


settings = Settings()
