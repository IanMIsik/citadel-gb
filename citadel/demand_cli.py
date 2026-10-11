"""`citadel train-demand-model`: walk-forward evaluation of the first demand-error model, then a final fit.

Dev only. Reads the `demand_*` tables in DATABASE_URL (run with CITADEL_ENV_FILE=.env.dev).
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import pandas as pd

MODEL_LABELS = {"ndf_as_published": "NDF as published", "persist_last": "Last period's error", "model_q50": "Model (median)"}
MODEL_METHODS = ("ndf_as_published", "persist_last", "model_q50")


async def train_demand_model(args: argparse.Namespace) -> None:
    from citadel.config import settings
    from citadel.engine import demand_eval, demand_features, demand_model
    from citadel.storage import db

    pd.set_option("display.width", 200, "display.max_columns", 30, "display.float_format", lambda v: f"{v:,.2f}")
    pool = await db.create_pool(settings.database_url, demand_schema=True)
    try:
        started = time.time()
        base = await demand_model.load_base(pool)
        if base.empty:
            print("No demand history found; run `citadel backfill-demand` first.")
            return
        ds = demand_features.build_dataset(base)
        columns = demand_features.feature_columns(ds)
        print(f"{len(ds):,} training rows ({ds.index.min():%Y-%m-%d} to {ds.index.max():%Y-%m-%d}), {len(columns)} features, "
              f"leads {sorted(ds['lead_min'].unique())}, built in {time.time() - started:.0f}s")

        years = [int(y) for y in args.years.split(",")]
        params = {"num_boost_round": args.rounds}
        started = time.time()
        preds, fitted = demand_model.walk_forward(ds, years, params)
        if preds.empty:
            print("No fold had both training and test data.")
            return
        print(f"walk-forward over {years} done in {time.time() - started:.0f}s\n")

        by_lead = demand_model.score_predictions(preds)
        print("MW error by lead time, all test years together (smaller is better):")
        print(by_lead.drop(columns=["coverage"], errors="ignore").query("method != 'band_coverage_10_90'").to_string(index=False))
        cov = by_lead[by_lead["method"] == "band_coverage_10_90"][["lead_min", "coverage"]]
        print("\nShare of actual errors that fell inside the model's 10-90% band (target is 80%):")
        print(cov.to_string(index=False))

        by_fold = demand_model.score_predictions(preds, ["fold", "lead_min"])
        print("\nMedian-model improvement over the published NDF, by test year (negative is better):")
        pivot = by_fold[by_fold["method"] == "model_q50"].pivot(index="lead_min", columns="fold", values="mae_vs_ndf_pct")
        print(pivot.to_string())

        last_year = max(fitted)
        imp = demand_model.feature_importance(fitted[last_year], columns)
        print(f"\nWhat the model leans on (median model, test year {last_year}, share of gain):")
        print(imp.to_string(index=False))

        preds = demand_model.calibrated_band(preds)
        print("\nShare of actual errors inside the 10-90% band after calibration (target 0.80), held-out years:")
        print_band_tables(demand_model, preds)

        if args.csv:
            preds.reset_index().to_csv(args.csv, index=False)
            print(f"\nwrote {len(preds):,} predictions to {args.csv}")
        if args.html:
            recent = preds[preds["fold"] == last_year]
            keep = lambda s: s[s["method"].isin(MODEL_METHODS)]  # noqa: E731
            page = demand_eval.report_html(
                keep(demand_model.score_predictions(preds)), keep(demand_model.score_predictions(recent)),
                f"walk-forward test years {', '.join(map(str, years))}; 'last 12 months' button = test year {last_year}",
                methods=MODEL_METHODS, labels=MODEL_LABELS, title="Demand-error model vs baselines")
            Path(args.html).write_text(page, encoding="utf-8")
            print(f"wrote report page {args.html}")

        if not args.no_final:
            started = time.time()
            final = demand_model.fit_quantiles(ds, columns, params)
            offsets = demand_model.static_offsets(preds)
            paths = demand_model.save_models(final, columns, args.save_dir, {
                "trained_through": str(ds.index.max()), "rows": len(ds), "n_estimators": args.rounds,
                "band": {"alpha": demand_model.DEFAULT_ALPHA, "window_days": demand_model.DEFAULT_WINDOW_DAYS,
                         "static_offsets_mw": {str(k): v for k, v in offsets.items()}}})
            print(f"final model (all data) saved to {paths[0].parent} in {time.time() - started:.0f}s")
    finally:
        await pool.close()


def print_band_tables(demand_model, cal: pd.DataFrame) -> None:
    test = cal[cal["fold"] >= sorted(cal["fold"].unique())[min(1, len(cal["fold"].unique()) - 1)]]
    summary = demand_model.band_summary(test)
    print(summary.pivot(index="lead_min", columns="band", values="coverage").round(3).to_string())
    print("\nMean band width in MW (narrower is better, at the same coverage):")
    print(summary.pivot(index="lead_min", columns="band", values="width_mw").round(0).to_string())


async def calibrate_demand_band(args: argparse.Namespace) -> None:
    """Recalibrate the band from a saved predictions CSV (no database needed)."""
    from citadel.engine import demand_model

    pd.set_option("display.width", 200)
    p = pd.read_csv(args.predictions, parse_dates=["target_start"]).set_index("target_start")
    p.index = pd.to_datetime(p.index, utc=True)
    cal = demand_model.calibrated_band(p, args.alpha, args.window_days)
    print(f"{len(cal):,} predictions, test years {sorted(cal['fold'].unique())}\n")
    print_band_tables(demand_model, cal)
    summary = demand_model.band_summary(cal[cal["fold"] >= sorted(cal["fold"].unique())[min(1, len(cal["fold"].unique()) - 1)]])
    if args.csv:
        cal.reset_index().to_csv(args.csv, index=False)
        print(f"\nwrote {args.csv}")
    if args.html:
        Path(args.html).write_text(band_report_html(summary, args.alpha, args.window_days), encoding="utf-8")
        print(f"wrote report page {args.html}")


_BAND_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Demand band calibration</title><style>
:root{--bg:#fff;--fg:#1c2330;--muted:#5b6575;--line:#d9dee7;--card:#f6f8fb}
@media (prefers-color-scheme:dark){:root{--bg:#10141b;--fg:#e6eaf2;--muted:#9aa5b8;--line:#2a3140;--card:#171c26}}
body{margin:0;padding:24px 16px;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}main{max-width:980px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}p.sub{color:var(--muted);margin:0 0 16px}.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px;margin:0 0 18px}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}th,td{padding:5px 8px;border-bottom:1px solid var(--line);text-align:right}th:first-child,td:first-child{text-align:left}
</style></head><body><main><h1>Does the 10-90% band hold 80% of outcomes?</h1>
<p class="sub">Held-out years only. "Raw" is the model's own band; "Rolling" widens it by how far recent, already-published errors fell outside
(last __WINDOW__ days, per lead time); "Static" uses the previous test years. The dashed line is the 80% target.</p>
<div class="card"><canvas id="cov" role="img" aria-label="Coverage by lead time for each band"></canvas></div>
<div class="card"><table id="tbl"></table></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<script>
const ROWS = __ROWS__, BANDS = [["raw","Raw","#8a94a6"],["static","Static","#e08a00"],["roll","Rolling","#2f6fed"]];
const leads = [...new Set(ROWS.map(r => r.lead_min))].sort((a, b) => a - b);
const get = (b, l, k) => (ROWS.find(r => r.band === b && r.lead_min === l) || {})[k];
new Chart(document.getElementById("cov"), {type: "bar", data: {labels: leads.map(l => l === 0 ? "at start" : l < 60 ? l + " min" : (l / 60) + " h"),
  datasets: BANDS.map(([b, t, c]) => ({label: t, backgroundColor: c, data: leads.map(l => get(b, l, "coverage"))})).concat(
    [{type: "line", label: "Target", borderColor: "#c0392b", borderDash: [6, 4], pointRadius: 0, data: leads.map(() => 1 - __ALPHA__)}])},
  options: {scales: {y: {min: 0.5, max: 1, title: {display: true, text: "share of outcomes inside the band"}}}}});
document.getElementById("tbl").innerHTML = "<tr><th>Lead</th>" + BANDS.map(([b, t]) => "<th>" + t + " coverage</th><th>" + t + " width MW</th>").join("") + "</tr>" +
  leads.map(l => "<tr><td>" + l + " min</td>" + BANDS.map(([b]) => "<td>" + (100 * get(b, l, "coverage")).toFixed(1) + "%</td><td>" + Math.round(get(b, l, "width_mw")).toLocaleString() + "</td>").join("") + "</tr>").join("");
</script></main></body></html>"""


def band_report_html(summary: pd.DataFrame, alpha: float, window_days: int) -> str:
    import json

    rows = json.loads(summary[["lead_min", "band", "coverage", "width_mw"]].to_json(orient="records"))
    return (_BAND_PAGE.replace("__ROWS__", json.dumps(rows)).replace("__ALPHA__", str(alpha)).replace("__WINDOW__", str(window_days)))


async def snapshot_demand_forecasts(args: argparse.Namespace) -> None:
    """One snapshot cycle, or a loop (Ctrl+C to stop). Writes to DATABASE_URL, so run it with CITADEL_ENV_FILE=.env.dev."""
    import asyncio
    import logging
    from datetime import datetime, timezone

    from citadel.config import settings
    from citadel.ingest import forecast_snapshots
    from citadel.storage import db

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    pool = await db.create_pool(settings.database_url, demand_schema=True)
    try:
        first = True
        while True:
            out = await forecast_snapshots.snapshot_cycle(pool, embedded_days=3 if first else args.embedded_days)
            first = False
            print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z  {out}", flush=True)
            if args.loop_minutes <= 0:
                break
            await asyncio.sleep(args.loop_minutes * 60)
    finally:
        await pool.close()


async def eval_demand_weather(args: argparse.Namespace) -> None:
    """Does weather really help once it is honest? Trains the same median model three ways on the period where the
    day-ahead weather forecasts exist (from 2024-03-10) and compares walk-forward error."""
    from datetime import datetime, timezone

    from citadel.config import settings
    from citadel.engine import demand_features, demand_model
    from citadel.ingest.demand_backfill import WEATHER_DAY1_FROM
    from citadel.storage import db

    pd.set_option("display.width", 200, "display.float_format", lambda v: f"{v:,.1f}")
    years = [int(y) for y in args.years.split(",")]
    start = datetime(WEATHER_DAY1_FROM.year, WEATHER_DAY1_FROM.month, WEATHER_DAY1_FROM.day, tzinfo=timezone.utc)
    variants = {"hindsight weather": "demand_weather", "day-ahead forecast weather": "demand_weather_day1", "no weather": None}
    pool = await db.create_pool(settings.database_url, demand_schema=True)
    try:
        table = {}
        for name, source in variants.items():
            base = await demand_model.load_base(pool, start=start, weather_table=source)
            ds = demand_features.build_dataset(base)
            preds, _ = demand_model.walk_forward(ds, years, {"num_boost_round": args.rounds}, quantiles=(0.5,))
            g = preds.dropna(subset=["y", "q50"])
            model_mae = (g["y"] - g["q50"]).abs().groupby(g["lead_min"]).mean()
            ndf_mae = g["y"].abs().groupby(g["lead_min"]).mean()
            table[name] = 100.0 * (model_mae / ndf_mae - 1.0)
            print(f"{name}: {len(g):,} test periods scored", flush=True)
        print()
        print(f"Median-model MAE change versus the published NDF, test years {years}, trained from {start:%Y-%m-%d} (more negative is better):")
        print(pd.DataFrame(table).to_string())
    finally:
        await pool.close()


async def demand_prediction_tracking(args: argparse.Namespace) -> None:
    """Score the logged live predictions (made while the dev server was running) against the outturn."""
    from datetime import datetime, timedelta, timezone

    from citadel.config import settings
    from citadel.engine import demand_tracking
    from citadel.storage import db

    pd.set_option("display.width", 200, "display.float_format", lambda v: f"{v:,.1f}")
    pool = await db.create_pool(settings.database_url, demand_schema=True)
    try:
        await db.score_demand_predictions(pool)
        total = await pool.fetchval("SELECT count(*) FROM demand_prediction_log")
        first = await pool.fetchval("SELECT min(predicted_at) FROM demand_prediction_log")
        scores = demand_tracking.scores_frame(await db.demand_prediction_scores(pool, datetime.now(timezone.utc) - timedelta(days=args.days)))
        summary = demand_tracking.tracking_summary(scores)
        print(f"{total:,} predictions logged" + (f" since {first:%Y-%m-%d %H:%M}Z" if first else "") +
              f"; {int(summary['n'].sum()) if len(summary) else 0:,} live ones scored in the last {args.days} days")
        if summary.empty:
            print("Nothing scored yet: predictions are logged while the dev server runs and scored once INDO is published (about 30 minutes after a period).")
            return
        print()
        print("Per lead (MW; model_mae vs ndf_mae: smaller is better; coverage should be about 0.80):")
        print(summary.to_string(index=False))
    finally:
        await pool.close()
