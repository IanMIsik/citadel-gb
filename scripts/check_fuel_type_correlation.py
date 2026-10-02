"""Sanity-checks engine/fpn.py's unit-to-fuel-type classification: for each
fuel type, a settlement period's total FPN (planned generation, from
`fpn_by_fuel`) should be in the same ballpark as FUELINST's own real
generation for that fuel type (`fpn_generation_by_fuel`, sourced straight
from Elexon's FUELINST dataset, independent of the BM-unit reference this
project's own classification depends on). A fuel type where those two
numbers don't correlate at all -- wildly different magnitude, opposite
sign, or one side populated while the other is empty -- is the same
symptom a real misclassification produced twice before (COAL and the
"switching" bug): a BM unit's FPN landing in the wrong fuel-type bucket.

This is a rule-of-thumb check, not a precise test -- real generation
legitimately drifts from plan (wind curtailment being the obvious case),
so a moderate mismatch isn't automatically wrong. It's a repeatable
diagnostic in the same spirit as scripts/backtest_sp.py, not a pass/fail
gate.

Usage:
    python scripts/check_fuel_type_correlation.py
    python scripts/check_fuel_type_correlation.py --date 2026-09-22 --sp 22
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date

import asyncpg

from citadel.config import settings
from citadel.settlement import current_period, sp_end_utc, sp_start_utc

# A fuel type's FPN and its real generation are "suspicious" if one is
# near zero while the other isn't, or if their ratio falls outside this
# band -- loose enough to tolerate normal dispatch drift and wind
# curtailment, tight enough to still catch a unit in the wrong bucket.
RATIO_LOW, RATIO_HIGH = 0.3, 3.0
NEAR_ZERO_MW = 5.0


async def run(sd: date, sp: int) -> None:
    pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=2)
    try:
        fpn_rows = await pool.fetch(
            "SELECT fuel_type, AVG(fpn_spot_vol) AS avg_fpn FROM fpn_by_fuel "
            "WHERE settlement_date = $1 AND settlement_period = $2 GROUP BY fuel_type",
            sd, sp,
        )
        gen_rows = await pool.fetch(
            "SELECT fuel_type, AVG(real_gen) AS avg_real_gen FROM fpn_generation_by_fuel "
            "WHERE ts >= $1 AND ts < $2 GROUP BY fuel_type",
            sp_start_utc(sd, sp), sp_end_utc(sd, sp),
        )
    finally:
        await pool.close()

    fpn_by_fuel = {r["fuel_type"]: float(r["avg_fpn"]) for r in fpn_rows if r["avg_fpn"] is not None}
    gen_by_fuel = {r["fuel_type"]: float(r["avg_real_gen"]) for r in gen_rows if r["avg_real_gen"] is not None}
    # NATGRID is engine/fpn.py's own synthetic row (National Grid's BM-
    # adjacent trades folded in as a fuel type) -- it never appears in
    # FUELINST at all, by design, so it would always spuriously flag here.
    fuel_types = sorted((set(fpn_by_fuel) | set(gen_by_fuel)) - {"NATGRID"})

    print(f"Settlement date: {sd}  Period: {sp}\n")
    print(f"{'Fuel type':<14} {'FPN (MW)':>12} {'Real gen (MW)':>14}  Flag")
    print("-" * 60)

    suspicious: list[str] = []
    for ft in fuel_types:
        # A fuel type missing from one dataset (no BM unit currently
        # classified there, or FUELINST reporting nothing) reads as 0 for
        # this comparison -- only a genuine magnitude/sign mismatch is
        # worth flagging, not the mere absence of a row on one side.
        fpn = fpn_by_fuel.get(ft, 0.0)
        gen = gen_by_fuel.get(ft, 0.0)
        flag = ""
        if abs(fpn) < NEAR_ZERO_MW and abs(gen) >= NEAR_ZERO_MW:
            flag = "FPN ~0 but real generation isn't"
        elif abs(gen) < NEAR_ZERO_MW and abs(fpn) >= NEAR_ZERO_MW:
            flag = "real generation ~0 but FPN isn't"
        elif fpn != 0 and gen != 0 and (fpn / gen < 0):
            flag = "opposite sign"
        elif fpn != 0 and gen != 0:
            ratio = gen / fpn
            if not (RATIO_LOW <= ratio <= RATIO_HIGH):
                flag = f"ratio {ratio:.2f}x outside [{RATIO_LOW}, {RATIO_HIGH}]"
        if flag:
            suspicious.append(ft)
        print(f"{ft:<14} {fpn:>12.0f} {gen:>14.0f}  {flag}")

    print()
    if suspicious:
        print(f"Suspicious fuel types (possible misclassification): {', '.join(suspicious)}")
    else:
        print("No suspicious fuel types -- FPN and real generation correlate reasonably for all of them.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="YYYY-MM-DD settlement date (default: current)")
    parser.add_argument("--sp", type=int, help="Settlement period (default: current)")
    args = parser.parse_args()

    if args.date and args.sp:
        sd, sp = date.fromisoformat(args.date), args.sp
    else:
        cur = current_period()
        sd, sp = cur.settlement_date, cur.settlement_period

    asyncio.run(run(sd, sp))


if __name__ == "__main__":
    main()
