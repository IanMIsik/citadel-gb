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
            paths = demand_model.save_models(final, columns, args.save_dir, {
                "trained_through": str(ds.index.max()), "rows": len(ds), "n_estimators": args.rounds})
            print(f"final model (all data) saved to {paths[0].parent} in {time.time() - started:.0f}s")
    finally:
        await pool.close()
