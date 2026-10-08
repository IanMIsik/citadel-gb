"""Check the previous-acceptance volume rule (engine/acceptance_volumes.py) against Elexon's
published per-acceptance stack, for one or more settlement periods.

Usage:
    python scripts/compare_acceptance_volumes.py --date 2026-10-08 --sp 23
    python scripts/compare_acceptance_volumes.py --date 2026-10-08 --sp 17-24 --worst 8
"""

from __future__ import annotations

import argparse
from datetime import date

import httpx

from citadel.engine.acceptance_volumes import acceptance_volumes, compare, elexon_stack_volumes
from citadel.settlement import sp_start_utc

BASE = "https://data.elexon.co.uk/bmrs/api/v1"


def _rows(client: httpx.Client, path: str, **params) -> list[dict]:
    r = client.get(f"{BASE}/{path}", params={**params, "format": "json"}, timeout=90)
    r.raise_for_status()
    body = r.json()
    return body["data"] if isinstance(body, dict) else body


def run(client: httpx.Client, sd: date, sp: int, worst: int) -> dict:
    p = {"settlementDate": sd.isoformat(), "settlementPeriod": sp}
    boalf = _rows(client, "balancing/acceptances/all", **p)
    pn = _rows(client, "datasets/PN", **p)
    offers = _rows(client, f"balancing/settlement/stack/all/offer/{sd.isoformat()}/{sp}")
    bids = _rows(client, f"balancing/settlement/stack/all/bid/{sd.isoformat()}/{sp}")
    mine = acceptance_volumes(boalf, pn, sp_start_utc(sd, sp))
    theirs = elexon_stack_volumes(offers, bids)
    res = compare(mine, theirs)
    print(f"SP{sp}: {res['matching']}/{res['pairs']} (unit, acceptance) pairs match to 0.01 MWh | "
          f"offers mine {res['mine_offers']:.2f} vs Elexon {res['their_offers']:.2f} | "
          f"bids mine {res['mine_bids']:.2f} vs Elexon {res['their_bids']:.2f}")
    for err, (unit, acc), a, b in res["wrong"][:worst]:
        print(f"    {unit} #{acc}: mine offer {a[0]:.3f} bid {a[1]:.3f} | Elexon offer {b[0]:.3f} bid {b[1]:.3f}  (err {err:.3f})")
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True)
    ap.add_argument("--sp", required=True, help="a period, or a range like 17-24")
    ap.add_argument("--worst", type=int, default=5, help="how many worst mismatches to list per period")
    args = ap.parse_args()
    lo, _, hi = args.sp.partition("-")
    sps = range(int(lo), int(hi or lo) + 1)
    sd = date.fromisoformat(args.date)
    with httpx.Client() as client:
        for sp in sps:
            run(client, sd, sp, args.worst)


if __name__ == "__main__":
    main()
