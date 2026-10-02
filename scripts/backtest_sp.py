"""Fetch a past settlement period's raw data from Elexon's REST API -- and
the surrounding +-window the engine needs for ramp/base-value continuity
across period boundaries, same as the live runner -- run it through the
ported engine, and compare the result to Elexon's own real settlement
system price. The repeatable tool for the accuracy work mentioned in the
project plan, rather than a one-off manual check.

Usage:
    python scripts/backtest_sp.py --date 2026-09-11 --sp 17
    python scripts/backtest_sp.py --date 2026-09-11 --sp 17 --include-case-6
    python scripts/backtest_sp.py --date 2026-09-11 --sp 17 --no-mel-gate
    python scripts/backtest_sp.py --date 2026-09-11 --sp 17 --reversal-side-fix
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date, timedelta

import httpx
import pandas as pd

from citadel.engine.stack import compute_stack
from citadel.ingest import elexon_rest
from citadel.settlement import sp_start_utc, window_around


def _mel_mil_ranges_for(sd: date, sp: int) -> list[tuple[str, str]]:
    """Hour-boundary (from, to) pairs spanning the target settlement
    period, wide enough (2 hours either side) to cover any MEL ramp whose
    minute-level window overlaps it.
    """
    anchor = sp_start_utc(sd, sp).replace(minute=0, second=0, microsecond=0)
    fmt = "%Y-%m-%dT%H:%MZ"
    offsets = range(-2, 3)
    timestamps = [anchor + timedelta(hours=h) for h in offsets]
    return [(timestamps[i].strftime(fmt), timestamps[i + 1].strftime(fmt)) for i in range(len(timestamps) - 1)]


async def run(
    target_date: date, sp: int, include_case_6: bool, use_mel_gate: bool, pricing_method: str, par_band_method: str,
    overlap_resolution: str, reversal_side_fix: bool, disaggregate_disbsad: bool,
) -> None:
    periods = window_around(target_date, sp)
    mel_mil_ranges = _mel_mil_ranges_for(target_date, sp)

    async with httpx.AsyncClient() as client:
        bundle = await elexon_rest.fetch_window(client, periods, mel_mil_ranges)
        actual_rows = await elexon_rest.fetch_system_price(client, target_date)
        mid_from = sp_start_utc(*periods[0]).strftime("%Y-%m-%dT%H:%MZ")
        mid_to = (sp_start_utc(*periods[-1]) + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%MZ")
        mid_rows = await elexon_rest.fetch_market_index(client, mid_from, mid_to)
    market_index_prices = elexon_rest.blend_market_index(mid_rows)

    result = compute_stack(
        pd.DataFrame(bundle.boalf), pd.DataFrame(bundle.bod), pd.DataFrame(bundle.pn),
        pd.DataFrame(bundle.mel), pd.DataFrame(bundle.disbsad),
        include_case_6=include_case_6, use_mel_gate=use_mel_gate,
        pricing_method=pricing_method, par_band_method=par_band_method,
        market_index_prices=market_index_prices, overlap_resolution=overlap_resolution,
        reversal_side_fix=reversal_side_fix, disaggregate_disbsad=disaggregate_disbsad,
    )

    actual = next((r for r in actual_rows if r.get("settlementPeriod") == sp), None)
    actual_price = actual.get("systemSellPrice") if actual else None

    print(f"Settlement date: {target_date}  Period: {sp}  (fetched window: {periods[0][1]}..{periods[-1][1]})")
    print(f"Raw counts -- BOALF: {len(bundle.boalf)}  BOD: {len(bundle.bod)}  PN: {len(bundle.pn)}  "
          f"MEL: {len(bundle.mel)}  DISBSAD: {len(bundle.disbsad)}")
    mip = market_index_prices.get((target_date, sp))
    print(f"Market Index Price for this period: {'GBP %.2f/MWh' % mip if mip is not None else 'not available'}")

    if not result.empty:
        result = result[(result["settlementDate"].dt.date == target_date) & (result["settlementPeriod"] == sp)]

    if result.empty:
        print("Computed stack: EMPTY for this period (no priced rows -- check raw counts above, or try --no-mel-gate)")
    else:
        computed_price = float(result.iloc[0]["total_misik_price"])
        print(f"Rows in stack for this period: {len(result)}")
        print(f"Computed price:  GBP {computed_price:.2f}/MWh")
        print(f"Actual price:    {'GBP %.2f/MWh' % actual_price if actual_price is not None else 'not available'}")
        if actual_price is not None:
            print(f"Delta:           GBP {computed_price - actual_price:+.2f}/MWh")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="YYYY-MM-DD settlement date")
    parser.add_argument("--sp", required=True, type=int, help="Settlement period (1-50)")
    parser.add_argument("--include-case-6", action="store_true", help="Include the excluded-by-default case 6 allocation")
    parser.add_argument("--no-mel-gate", dest="mel_gate", action="store_false", help="Disable the MEL inner-join gate")
    parser.add_argument("--par-band-method", choices=["interval", "ratio"], default="interval",
                         help="'interval' (default, corrected) or 'ratio' (original notebook's formula -- has a confirmed sign bug, see engine/stack.py). Only applies with --pricing-method legacy.")
    parser.add_argument("--pricing-method", choices=["guide", "legacy"], default="guide",
                         help="'guide' (default) -- Elexon's real Classification/NIV/PAR-Tagging methodology (engine/imbalance_price.py), or 'legacy' -- the original notebook's heuristic, for comparison.")
    parser.add_argument("--overlap-resolution", choices=["latest_wins", "reversal_aware"], default="reversal_aware",
                         help="'reversal_aware' (default) -- a superseded acceptance's own remaining window still contributes wherever its own marginal delta genuinely reverses sign; validated against 15 real settlement periods with zero regressions (see engine/stack.py's build_marginal_deltas()/_reversal_tail_rows() docstrings). 'latest_wins' -- a later acceptance fully supersedes an earlier overlapping one; kept for comparison.")
    parser.add_argument("--reversal-side-fix", action="store_true",
                         help="Only applies with --overlap-resolution reversal_aware (the default). A genuine reversal's marginal delta is computed relative to the acceptance it reverses (base_before) instead of relative to FPN, so it lands on the correct side of the bid/offer stack. Confirmed against T_FERRB-1/SP14 (acceptanceId 14433/14434) and systematic across 45/370 reversal-flagged acceptances sampled; defaulted off pending broader validation (see engine/stack.py's build_marginal_deltas() docstring).")
    parser.add_argument("--disaggregate-disbsad", action="store_true",
                         help="Prices each DISBSAD action at its own price instead of pre-summing every action sharing a (soFlag, storFlag) into one blended-average row before PAR Tagging runs. Confirmed live SP39 2026-09-25: 34 separate actions (GBP212.50-235.00/MWh) collapsed into one blended row, discarding the per-action detail PAR Tagging needs when its 1 MWh boundary falls inside that block (see engine/stack.py's blend_disbsad() docstring).")
    parser.set_defaults(mel_gate=True)
    args = parser.parse_args()

    asyncio.run(run(
        date.fromisoformat(args.date), args.sp, args.include_case_6, args.mel_gate,
        args.pricing_method, args.par_band_method, args.overlap_resolution, args.reversal_side_fix,
        args.disaggregate_disbsad,
    ))


if __name__ == "__main__":
    main()
