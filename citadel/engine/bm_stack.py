"""BM Stack: the bids and offers that are still *available* (untouched) for
National Grid to call, per settlement period -- aggregated by price, tagged
with fuel type and unit details. Ported from the user's
`bid_offer_data_continous.ipynb` (which fed a Tableau dashboard) with two
deliberate corrections:

1. The notebook took `MEL - FPN` as available capacity and ignored what has
   already been accepted (BOALF). "Untouched" volume here is net of the
   unit's current accepted deviation (`delta` = boalf level - FPN): the part
   of a band an acceptance has already consumed is not shown.
2. The notebook computed `sum_of_prev_vb` but never used it, so every band
   counted its volume from FPN (over-counting any multi-band unit). Band
   geometry here is the pricing engine's own (stack.build_bod_ladder:
   `levelTo` is a band width, `lower = sum(previous bands)`, `upper = lower +
   levelTo`), which was validated against Elexon's real stack.

Offer side (pairId > 0): headroom above FPN up to MEL. Bid side (pairId < 0):
headroom down from FPN towards MIL (a missing MIL is treated as 0, i.e. a
generator can be turned down to zero but not import).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .stack import build_bod_ladder

BM_STACK_COLUMNS = [
    "settlement_date", "settlement_period", "side", "bm_unit", "fuel_type", "lead_party", "capacity_mw",
    "price", "pair_id", "mw", "fpn_mw", "mel_mw", "accepted_mw",
]
# Below this a band is rounding noise, not real availability.
MIN_MW = 0.01


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=BM_STACK_COLUMNS)


def compute_bm_stack(
    by_unit: pd.DataFrame,
    bod_df: pd.DataFrame | None,
    fuel_ref: pd.DataFrame,
    bm_unit_reference: pd.DataFrame,
    window_periods: list[tuple],
    minute_step: int = 5,
    exclude_fuel_prefix: str | None = None,
) -> pd.DataFrame:
    """`by_unit`: engine/fpn.py's per-unit per-minute frame (bmUnit,
    settlementDate, settlementPeriod, spot_time, fpn_spot_vol, delta,
    mel_spot_vol, mel_reduced_fpn, mil_spot_vol). `window_periods`: the
    (date, sp) pairs to report. `minute_step`: only every Nth minute is
    sampled (default 5 -- availability varies slowly and this is ~5x
    cheaper than every minute); a band's MW is the mean over sampled minutes.
    """
    if by_unit is None or by_unit.empty or bod_df is None or bod_df.empty or not window_periods:
        return _empty()

    periods = pd.DataFrame({
        "settlementDate": pd.to_datetime([sd for sd, _ in window_periods]),
        "settlementPeriod": [int(sp) for _, sp in window_periods],
    })
    bu = by_unit.copy()
    bu["settlementDate"] = pd.to_datetime(bu["settlementDate"])
    bu = bu.merge(periods, on=["settlementDate", "settlementPeriod"])
    bu = bu[bu["spot_time"].dt.minute % minute_step == 0]
    # Availability needs a real MEL reading -- a unit with none that minute
    # can't be assessed (mel_spot_vol is +inf where fpn.explode_and_merge
    # found no MEL, i.e. "no constraint", which would show phantom capacity).
    bu = bu[np.isfinite(bu["mel_spot_vol"])]
    if bu.empty:
        return _empty()

    mil = bu["mil_spot_vol"].where(np.isfinite(bu["mil_spot_vol"]), 0.0)
    bu = bu.assign(
        up_total=np.maximum(bu["mel_spot_vol"] - bu["fpn_spot_vol"], 0.0),
        down_total=np.abs(np.minimum(mil - bu["mel_reduced_fpn"], 0.0)),
    )

    ladder = build_bod_ladder(bod_df, bu["bmUnit"].unique()).drop_duplicates()
    ladder["settlementDate"] = pd.to_datetime(ladder["settlementDate"])
    ladder["settlementPeriod"] = ladder["settlementPeriod"].astype(int)
    ladder = ladder[ladder["pairId"] != 0].copy()
    prev = sum(ladder[f"levelTo{x}"].abs() for x in range(1, 5))
    ladder["lo"] = prev
    ladder["hi"] = prev + ladder["levelTo"].abs()
    ladder["is_offer"] = ladder["pairId"] > 0
    ladder["price"] = np.where(ladder["is_offer"], ladder["offer"], ladder["bid"])
    ladder = ladder[["settlementDate", "settlementPeriod", "bmUnit", "pairId", "is_offer", "price", "lo", "hi"]]

    m = bu.merge(ladder, on=["settlementDate", "settlementPeriod", "bmUnit"])
    if m.empty:
        return _empty()

    total = np.where(m["is_offer"], m["up_total"], m["down_total"])
    consumed = np.where(m["is_offer"], np.maximum(m["delta"], 0.0), np.maximum(-m["delta"], 0.0))
    m["mw"] = np.maximum(np.minimum(m["hi"], total) - np.maximum(m["lo"], consumed), 0.0)

    keys = ["settlementDate", "settlementPeriod", "is_offer", "bmUnit", "price", "pairId"]
    out = m.groupby(keys, as_index=False)["mw"].mean()
    out = out[(out["mw"] >= MIN_MW) & out["price"].notna()]
    if out.empty:
        return _empty()

    unit_ctx = bu.groupby(["settlementDate", "settlementPeriod", "bmUnit"], as_index=False).agg(
        fpn_mw=("fpn_spot_vol", "mean"), mel_mw=("mel_spot_vol", "mean"), accepted_mw=("delta", "mean"),
    )
    out = out.merge(unit_ctx, on=["settlementDate", "settlementPeriod", "bmUnit"], how="left")
    out = out.merge(fuel_ref[["bmUnit", "FT"]].drop_duplicates(subset=["bmUnit"]), on="bmUnit", how="left")

    ref = bm_unit_reference[["elexon_bm_unit", "lead_party_name", "generation_capacity_mw"]].drop_duplicates(subset=["elexon_bm_unit"])
    ref = ref.rename(columns={"elexon_bm_unit": "bmUnit"})
    out = out.merge(ref, on="bmUnit", how="left")

    out = out.rename(columns={
        "settlementDate": "settlement_date", "settlementPeriod": "settlement_period", "bmUnit": "bm_unit",
        "FT": "fuel_type", "lead_party_name": "lead_party", "generation_capacity_mw": "capacity_mw", "pairId": "pair_id",
    })
    out["side"] = np.where(out["is_offer"], "offer", "bid")
    out["fuel_type"] = out["fuel_type"].fillna("NO_FUEL")
    if exclude_fuel_prefix:
        out = out[~out["fuel_type"].str.startswith(exclude_fuel_prefix)]
    out["settlement_date"] = out["settlement_date"].dt.strftime("%Y-%m-%d")
    for col in ("mw", "fpn_mw", "mel_mw", "accepted_mw"):
        out[col] = out[col].round(3)
    out["price"] = out["price"].round(2)
    out["capacity_mw"] = pd.to_numeric(out["capacity_mw"], errors="coerce")
    out = out[BM_STACK_COLUMNS].astype(object).where(out[BM_STACK_COLUMNS].notna(), None)
    return out.reset_index(drop=True)
