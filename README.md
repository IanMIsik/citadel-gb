# Citadel — real-time GB Balancing Mechanism pricing stack

A standalone rebuild of a personal tool ("Pricing Stack") that used to run
as a Google Colab notebook polling Elexon every ~7 seconds and pushing
results into a Google Sheet. This version:

- reconstructs the same per-BM-unit priced actions (from BOALF, BOD, PN,
  MEL and DISBSAD) the original notebook did,
- **prices them using Elexon's actual documented Imbalance Pricing
  methodology** (`citadel/engine/imbalance_price.py`: Classification / NIV
  Tagging / Replacement Price / PAR Tagging, per the BSC's Imbalance
  Pricing Guidance Note v15.0) rather than the original notebook's own
  heuristic -- validated live against real settlement periods (see below),
- ingests via Elexon's **IRIS** push service (Azure Service Bus/AMQP) when
  configured, falling back to REST polling otherwise,
- persists to **Postgres**, and
- serves a live web page over a **WebSocket**: unflagged actions (after
  Classification reclassifies any cheaper flagged ones into this group)
  and effectively-flagged actions each as one combined offers+bids stack,
  price descending -- most-expensive offers first, cheapest (most
  positive-priced) bids first, per the guide's own Ranked Set convention.

**Accuracy, backtested against real settlement periods (2026-09-18)**
after moving from the notebook's original heuristic to the guide's real
methodology: SP20 within £1.15/MWh, SP30 an exact match, SP40 within
£3.63/MWh, SP10 still off by ~£125/MWh (under investigation -- see
`citadel/engine/imbalance_price.py`'s KNOWN GAPS: BPA/SPA and Transmission
Loss Multiplier aren't implemented yet, no data source ingested for
either). Use `scripts/backtest_sp.py --pricing-method legacy` to compare
against the original heuristic on any period.

See `citadel/engine/stack.py`'s module docstring for how the upstream
per-row computation (which price band each MWh of an action falls into) is
derived from the original notebook, including two remaining discrepancies
found during that port (`include_case_6`, `use_mel_gate`) kept as toggles
for `scripts/backtest_sp.py` to investigate further.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -e ".[dev]"

docker compose up -d          # starts local Postgres
cp .env.template .env         # DATABASE_URL already points at the compose Postgres
```

### Run everything in Docker (no Python install needed)

```bash
cp .env.template .env         # optional: add ENTSOE_KEY / IRIS_* secrets
docker compose --profile app up -d --build
```

Open http://localhost:8000. This starts Postgres and the app together;
tables are created on first start. Set `HOST_PORT` to use a different host
port and `PROCESS_POOL_WORKERS` (default 2, ~130 MB each) to trade speed for
memory. Plain `docker compose up -d` still starts Postgres only, for local
development. Budget about 2 GiB of RAM for the full stack.

Run the server:

```bash
citadel serve --reload
```

Opens at `http://127.0.0.1:8000`. On startup it loads BM unit reference
data, then starts ingesting the current rolling settlement-period window
(current period -3..+2) either via REST polling (default,
`REST_POLL_INTERVAL_SECONDS` in `.env`, default 5s) or via IRIS if
configured (see below) -- either way the live page updates over the
WebSocket as soon as a recompute finishes.

## Running dev alongside prod

A second, complete instance -- same frontend, its own database and port --
can run side by side with prod without touching prod's data or behaviour:

```bash
psql -U citadel -h localhost -c "CREATE DATABASE citadel_dev"
cp .env.dev.template .env.dev   # DATABASE_URL already points at citadel_dev

CITADEL_ENV_FILE=.env.dev citadel serve --port 8001 --reload
```

`CITADEL_ENV_FILE` (a real OS env var, set before the process starts) tells
`citadel/config.py` which file to read instead of the default `.env` --
prod keeps running unmodified, reading plain `.env`. Dev runs on REST
polling only by default (see `.env.dev.template`), so it doesn't compete
with prod for the same IRIS queue. Open `http://127.0.0.1:8001/` (pricing
stack) or `/fpn` (FPN Analytics) directly -- it's the same app, same pages,
just pointed at its own database.

Risky, not-yet-validated engine changes go behind a feature toggle in
`citadel/config.py` (defaulted off), turned on in `.env.dev` only, so they
run in dev first and only become prod's default once validated against
real settlement periods there.

## Enabling real-time push (IRIS)

REST polling alone already beats the original notebook's 7-second loop
(5s default, configurable), but IRIS delivers each dataset update the
moment Elexon publishes it, no polling delay at all. To enable it:

1. Sign up yourself at <https://bmrs.elexon.co.uk/iris> (this is an account
   signup only you can complete).
2. Create a queue there (one is created by default) and a client secret.
3. Fill in `.env`: `IRIS_CLIENT_ID`, `IRIS_CLIENT_SECRET`, `IRIS_QUEUE_NAME`
   (the tenant ID and namespace defaults already match Elexon's current
   values).
4. Restart `citadel serve` -- it automatically prefers IRIS once all three
   are present (see `citadel/config.py`'s `iris_configured`), no code
   change needed. REST polling keeps running underneath at a low frequency
   as a safety net against a missed/dropped IRIS message.

## Investigating accuracy

```bash
python scripts/backtest_sp.py --date 2026-09-11 --sp 17
python scripts/backtest_sp.py --date 2026-09-11 --sp 17 --pricing-method legacy
python scripts/backtest_sp.py --date 2026-09-11 --sp 17 --include-case-6
python scripts/backtest_sp.py --date 2026-09-11 --sp 17 --no-mel-gate
```

Fetches that period's raw data fresh from Elexon's REST API, runs it
through the engine, and prints the computed price next to Elexon's actual
settlement system price. Once `GET /api/accuracy/{date}/{sp}` has computed
data to compare against (i.e. after the live server has processed that
period), it reports the same delta over HTTP.

## Running the tests

```bash
pytest
```

## What's not built yet

Multi-tenant auth/billing, production deployment hardening, a proper
frontend, full historical backfill, and a real accuracy-tracking dashboard
(the `/api/accuracy` endpoint exists but there's no UI for it yet) --
see the project plan for the full phased roadmap.
