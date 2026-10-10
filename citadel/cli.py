"""`citadel serve` -- thin wrapper so `pip install -e .` gives a console
script, same convention as the sibling gbpw project's `gbpw serve`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import date, datetime, timezone

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(prog="citadel")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the API + web server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")

    backfill = sub.add_parser(
        "backfill-demand",
        help="Download the history the demand forecaster trains on (writes to the database in DATABASE_URL)")
    backfill.add_argument("--from", dest="start", type=date.fromisoformat, default=date(2022, 1, 1),
                          help="first day (default 2022-01-01, about where Elexon's NDF vintages begin)")
    backfill.add_argument("--to", dest="end", type=date.fromisoformat, default=None, help="last day (default today)")
    backfill.add_argument("--datasets", default="all",
                          help="comma list of: ndf,tsdf,windfor,outturn,fuelinst,neso_historic,embedded_forecast,pv_live,weather,calendar")
    backfill.add_argument("--concurrency", type=int, default=3)
    backfill.add_argument("--force", action="store_true", help="refetch chunks that are already logged as done")

    evaluate = sub.add_parser(
        "eval-demand-baseline",
        help="Score the published NDF and simple corrections against INDO (reads the demand_* tables)")
    evaluate.add_argument("--from", dest="start", type=date.fromisoformat, default=None)
    evaluate.add_argument("--to", dest="end", type=date.fromisoformat, default=None, help="exclusive")
    evaluate.add_argument("--csv", default=None, help="also write every scored period to this CSV file")
    evaluate.add_argument("--validate-live", action="store_true",
                          help="also compare the replica of the live misco_* maths with fpn_aggregated")

    args = parser.parse_args()
    if args.command == "serve":
        uvicorn.run("citadel.api.app:app", host=args.host, port=args.port, reload=args.reload)
    elif args.command == "backfill-demand":
        asyncio.run(_backfill_demand(args))
    elif args.command == "eval-demand-baseline":
        asyncio.run(_eval_demand_baseline(args))


async def _eval_demand_baseline(args: argparse.Namespace) -> None:
    import pandas as pd

    from citadel.config import settings
    from citadel.engine import demand_eval
    from citadel.storage import db

    pd.set_option("display.width", 200, "display.max_columns", 30, "display.float_format", lambda v: f"{v:,.1f}")
    pool = await db.create_pool(settings.database_url, demand_schema=True)
    try:
        start = datetime(args.start.year, args.start.month, args.start.day, tzinfo=timezone.utc) if args.start else None
        end = datetime(args.end.year, args.end.month, args.end.day, tzinfo=timezone.utc) if args.end else None
        frame = await demand_eval.load_frame(pool, start, end)
        if frame.empty:
            print("No demand history found; run `citadel backfill-demand` first.")
            return
        table = demand_eval.evaluation_table(frame)
        print(f"{frame.index.min():%Y-%m-%d} .. {frame.index.max():%Y-%m-%d}, {frame['indo'].notna().sum():,} periods with INDO\n")
        print("Residual MW after each correction (smaller is better), by lead time (minutes before the period):")
        print(demand_eval.score(table).to_string(index=False))
        recent = table[table["target_start"] >= table["target_start"].max() - pd.Timedelta(days=365)]
        print("\nLast 12 months only:")
        print(demand_eval.score(recent).to_string(index=False))
        if args.csv:
            table.to_csv(args.csv, index=False)
            print(f"\nwrote {len(table):,} rows to {args.csv}")
        if args.validate_live:
            await _validate_live(pool, frame, demand_eval)
    finally:
        await pool.close()


async def _validate_live(pool, frame, demand_eval) -> None:
    import pandas as pd

    live = await pool.fetch("SELECT spot_time, misco_indo, misco_ndf, misco_indo_vs_ndf FROM fpn_aggregated ORDER BY spot_time")
    if not live:
        print("\nfpn_aggregated is empty in this database; nothing to validate against.")
        return
    live_df = pd.DataFrame([dict(r) for r in live])
    first, last = pd.to_datetime(live_df["spot_time"], utc=True).min(), pd.to_datetime(live_df["spot_time"], utc=True).max()
    warm = first - pd.Timedelta(days=1)
    fuel = await pool.fetch("SELECT start_time, total_mw FROM demand_fuelinst_5min WHERE start_time >= $1 AND start_time <= $2",
                            warm.to_pydatetime(), last.to_pydatetime())
    out = await pool.fetch("SELECT settlement_date AS \"settlementDate\", settlement_period AS \"settlementPeriod\", indo FROM demand_outturn "
                           "WHERE target_start >= $1 AND target_start <= $2", warm.to_pydatetime(), last.to_pydatetime())
    ndf = await pool.fetch("SELECT o.settlement_date AS \"settlementDate\", o.settlement_period AS \"settlementPeriod\", v.value AS latest_ndf "
                           "FROM demand_forecast_vintages v JOIN demand_outturn o ON o.target_start = v.target_start "
                           "WHERE v.dataset = 'NDF' AND v.lead_min = 0 AND v.target_start >= $1 AND v.target_start <= $2",
                           warm.to_pydatetime(), last.to_pydatetime())
    to_df = lambda rows: pd.DataFrame([dict(r) for r in rows])  # noqa: E731
    indo, ndf = to_df(out), to_df(ndf)
    for d in (indo, ndf):
        d["settlementDate"] = pd.to_datetime(d["settlementDate"])
    replica = demand_eval.live_replica(to_df(fuel), ndf, indo, warm.tz_localize(None), last.tz_localize(None))
    print("\nReplica of the live misco_* maths vs fpn_aggregated (this database):")
    print(demand_eval.compare_with_live(replica, live_df).to_string(index=False))


async def _backfill_demand(args: argparse.Namespace) -> None:
    from urllib.parse import urlsplit

    from citadel.config import settings
    from citadel.ingest import demand_backfill
    from citadel.storage import db

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    datasets = demand_backfill.resolve_datasets(args.datasets)
    end = args.end or datetime.now(timezone.utc).date()
    logging.info("Backfilling %s from %s to %s into database %r", ", ".join(datasets), args.start, end,
                 urlsplit(settings.database_url).path.lstrip("/"))
    pool = await db.create_pool(settings.database_url, demand_schema=True)
    try:
        summary = await demand_backfill.run_backfill(pool, args.start, end, datasets, concurrency=args.concurrency, force=args.force)
    finally:
        await pool.close()
    for name, row in summary.items():
        logging.info("%-18s fetched %4d chunk(s), skipped %4d, %9d rows, %d failed", name, row["chunks"], row["skipped"], row["rows"], row["failed"])
    if any(row["failed"] for row in summary.values()):
        logging.warning("Some chunks failed; run the same command again to retry only those.")


if __name__ == "__main__":
    main()
