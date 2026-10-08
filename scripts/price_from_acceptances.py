"""Price settled periods from raw acceptances with the previous-acceptance band split
(engine/acceptance_volumes.py) and compare with Elexon's published system price. Nothing here
touches the live engine.

Usage:
    python scripts/price_from_acceptances.py --date 2026-10-08 --sp 1-24
    python scripts/price_from_acceptances.py --date 2026-10-07 --sp 1-48 --cadl published
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date, timedelta

import httpx
import pandas as pd

from citadel.engine.acceptance_volumes import (
    acceptance_bands, compare_bands, elexon_non_bm_rows, elexon_stack_rows, price_from_bands)
from citadel.engine.stack import compute_cadl_flags
from citadel.ingest import elexon_rest
from citadel.settlement import sp_start_utc

BASE = "https://data.elexon.co.uk/bmrs/api/v1"


def _rows(client: httpx.Client, path: str, **params) -> list[dict]:
    r = client.get(f"{BASE}/{path}", params={**params, "format": "json"}, timeout=120)
    r.raise_for_status()
    body = r.json()
    return body["data"] if isinstance(body, dict) else body


async def _market_index(sd: date) -> dict:
    async with httpx.AsyncClient() as ac:
        # British Summer Time: SP1 starts the previous evening in UTC, so start a day early
        raw = await elexon_rest.fetch_market_index(
            ac, f"{(sd - timedelta(days=1)).isoformat()}T00:00:00Z", f"{sd.isoformat()}T23:59:59Z")
    return elexon_rest.blend_market_index(raw)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--sp", required=True, help="a period or a range such as 1-24")
    ap.add_argument("--cadl", choices=["none", "published", "engine"], default="none",
                    help="CADL flags: none, the ones Elexon publishes, or the engine's own (stack.compute_cadl_flags, "
                         "fed this period plus two either side)")
    args = ap.parse_args()
    sd = date.fromisoformat(args.date)
    lo, _, hi = args.sp.partition("-")
    mips = asyncio.run(_market_index(sd))
    ok = total = 0
    with httpx.Client() as c:
        actual = {r["settlementPeriod"]: r["systemSellPrice"] for r in _rows(c, f"balancing/settlement/system-prices/{sd.isoformat()}")}
        print("SP   from raw acceptances | Elexon actual |  diff | band rows matching Elexon")
        for sp in range(int(lo), int(hi or lo) + 1):
            if sp not in actual:
                continue
            p = {"settlementDate": sd.isoformat(), "settlementPeriod": sp}
            offers = _rows(c, f"balancing/settlement/stack/all/offer/{sd.isoformat()}/{sp}")
            bids = _rows(c, f"balancing/settlement/stack/all/bid/{sd.isoformat()}/{sp}")
            mine = acceptance_bands(_rows(c, "balancing/acceptances/all", **p), _rows(c, "datasets/PN", **p),
                                    _rows(c, "balancing/bid-offer/all", **p), sp_start_utc(sd, sp))
            theirs = elexon_stack_rows(offers, bids)
            cadl = None
            if args.cadl == "published":
                cadl = {(k[0], k[1]) for k, v in theirs.items() if v["cadl"]}
            elif args.cadl == "engine":
                seen, window = set(), []
                for q in range(max(1, sp - 2), sp + 3):
                    for r in _rows(c, "balancing/acceptances/all", settlementDate=sd.isoformat(), settlementPeriod=q):
                        key = (r["bmUnit"], r["acceptanceNumber"], r["timeFrom"])
                        if key not in seen:
                            seen.add(key)
                            window.append(r)
                flags = compute_cadl_flags(pd.DataFrame(window))
                cadl = {(r.bmUnit, int(r.acceptanceNumber)) for r in flags.itertuples() if r.cadl_flag}
            price = price_from_bands(mine + elexon_non_bm_rows(offers, bids), mips.get((sd, sp)), cadl)
            cmp = compare_bands(mine, theirs)
            diff = price - actual[sp]
            ok += abs(diff) < 0.05
            total += 1
            print(f"{sp:2d}   {price:10.2f}            | {actual[sp]:10.2f}    | {diff:6.2f} | {cmp['matching']}/{cmp['rows']}")
    print(f"\n{ok}/{total} periods within 5p of Elexon's price")


if __name__ == "__main__":
    main()
