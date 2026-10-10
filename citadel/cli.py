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

    args = parser.parse_args()
    if args.command == "serve":
        uvicorn.run("citadel.api.app:app", host=args.host, port=args.port, reload=args.reload)
    elif args.command == "backfill-demand":
        asyncio.run(_backfill_demand(args))


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
