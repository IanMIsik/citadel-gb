"""Orchestrates the live pipeline: keep a rolling in-memory window of raw
BOALF/BOD/PN/MEL/DISBSAD records current (via IRIS push if configured, REST
poll otherwise), recompute the stack whenever something changes (debounced
to absorb bursts of IRIS messages), persist the result, and broadcast it to
connected WebSocket clients.

This is a "streaming-triggered recompute", not a fully incremental
algorithm: each trigger reruns engine.stack.compute_stack over the whole
current window rather than updating state incrementally row-by-row. That
keeps the ported pricing math exactly as-is (see stack.py's own docstring on
why fidelity to the original mattered more than a rewrite) while still
getting the real latency win -- a recompute is triggered by an actual
message arriving, not by waiting out a fixed poll interval.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx
import pandas as pd

from ..config import Settings
from ..ingest import elexon_rest
from ..ingest.iris_client import run_iris_consumer_with_restart
from ..settlement import PERIOD_MINUTES, rolling_window, sp_start_utc
from ..storage import db
from . import stack as stack_engine

if TYPE_CHECKING:
    # Only for the type hint below -- engine/ deliberately doesn't depend on
    # api/ at runtime (that would invert the intended layering); Runner only
    # needs an object with an async publish(sd, sp, records) method.
    from ..api.broadcast import Broadcaster

logger = logging.getLogger("citadel.engine.runner")

DEBOUNCE_SECONDS = 1.0
MEL_MIL_LOOKAHEAD_HOURS = (-2, -1, 0, 1, 2)
# Each settlement period's DB write (delete+insert, its own transaction) and
# broadcast touch disjoint rows/messages, so periods can persist concurrently
# instead of one full round-trip at a time -- bounded so a wide rolling
# window (up to ~8 periods) doesn't grab every connection in the pool at
# once and starve API request handlers sharing it (see storage/db.py's
# create_pool, max_size=10).
PERSIST_CONCURRENCY = 4


def _records_from_payload(payload) -> list[dict]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        if "data" in payload and isinstance(payload["data"], list):
            return payload["data"]
        return [payload]
    return []


class WindowBuffers:
    """Raw per-dataset records for the current rolling window. IRIS
    messages `add()` individual records (deduped by the whole record, since
    the same acceptance/PN/etc. can legitimately be re-published); a REST
    poll cycle `replace()`s a dataset wholesale with a fresh fetch.
    """

    def __init__(self) -> None:
        self._data: dict[str, list[dict]] = {"boalf": [], "bod": [], "pn": [], "mel": [], "mil": [], "disbsad": []}

    def add(self, dataset_key: str, records: list[dict]) -> None:
        self._data[dataset_key].extend(records)

    def replace(self, dataset_key: str, records: list[dict]) -> None:
        self._data[dataset_key] = records

    def frame(self, dataset_key: str) -> pd.DataFrame:
        records = self._data[dataset_key]
        df = pd.DataFrame(records)
        return df.drop_duplicates() if not df.empty else df


IRIS_DATASET_TO_BUFFER_KEY = {"BOALF": "boalf", "BOD": "bod", "PN": "pn", "MELS": "mel", "MILS": "mil", "DISBSAD": "disbsad"}
# MIL is unused by this module's own pricing pipeline (see engine/stack.py's
# module docstring) but IS used by engine/fpn.py's adjusted-FPN clamp, which
# shares this Runner's buffers -- see engine/fpn_runner.py.


class Runner:
    def __init__(self, settings: Settings, pool, broadcaster: Broadcaster, process_pool=None) -> None:
        self.settings = settings
        self.pool = pool
        self.broadcaster = broadcaster
        self.buffers = WindowBuffers()
        self.market_index_prices: dict[tuple, float] = {}
        # Optional -- when supplied (see api/app.py's lifespan), the
        # per-cycle compute_stack() call below runs in its own OS process
        # instead of inline on this event loop, so a heavy recompute can
        # never delay WebSocket broadcasts or the FPN dashboard's own
        # recompute running alongside it. None keeps the old inline
        # behaviour (e.g. for tests constructing a Runner directly).
        self.process_pool = process_pool
        self._dirty = asyncio.Event()
        self._stop = asyncio.Event()
        self._http = httpx.AsyncClient()

    async def _rest_refresh_once(self) -> None:
        periods = rolling_window()
        now = datetime.now(timezone.utc)
        mel_mil_ranges = elexon_rest_generate_hour_pairs(now)
        bundle = await elexon_rest.fetch_window(self._http, periods, mel_mil_ranges)
        self.buffers.replace("boalf", bundle.boalf)
        self.buffers.replace("bod", bundle.bod)
        self.buffers.replace("pn", bundle.pn)
        self.buffers.replace("mel", bundle.mel)
        self.buffers.replace("mil", bundle.mil)
        self.buffers.replace("disbsad", bundle.disbsad)
        await db.log_refresh(self.pool, "rest", "window", True, note=f"{len(periods)} periods")
        self._dirty.set()
        await self._refresh_settlement_prices(periods)
        await self._refresh_market_index(periods)

    async def _refresh_market_index(self, periods: list[tuple]) -> None:
        """Market Index Price for the current window -- feeds
        engine.imbalance_price's Replacement Price fallback (see
        engine/stack.py's own market_index_prices docstring). Cached on the
        instance rather than persisted: it's only ever used at the moment
        of a recompute, and re-fetched every REST cycle anyway.
        """
        from_iso = sp_start_utc(*periods[0]).strftime("%Y-%m-%dT%H:%MZ")
        to_iso = (sp_start_utc(*periods[-1]) + pd.Timedelta(minutes=PERIOD_MINUTES)).strftime("%Y-%m-%dT%H:%MZ")
        try:
            rows = await elexon_rest.fetch_market_index(self._http, from_iso, to_iso)
        except Exception:
            logger.warning("market index fetch failed for %s..%s", from_iso, to_iso, exc_info=True)
            return
        self.market_index_prices = elexon_rest.blend_market_index(rows)

    async def _refresh_settlement_prices(self, periods: list[tuple]) -> None:
        """Elexon's real settlement system price, for the accuracy-tracking
        endpoint (/api/accuracy) -- fetched for every distinct settlement
        date in the current window. A period often isn't settled yet (still
        "indicative"/latest-run only) -- that's fine, this just means the
        stored value keeps updating until it's final, same tolerance the
        original notebook had toward Elexon's own settlement run revisions.
        """
        dates = sorted({sd for sd, _ in periods})
        for sd in dates:
            try:
                rows = await elexon_rest.fetch_system_price(self._http, sd)
            except Exception:
                logger.warning("settlement price fetch failed for %s", sd, exc_info=True)
                continue
            for r in rows:
                await db.upsert_settlement_price(self.pool, db.SettlementPriceRow(
                    settlement_date=sd,
                    settlement_period=r["settlementPeriod"],
                    system_sell_price=r.get("systemSellPrice"),
                    net_imbalance_volume=r.get("netImbalanceVolume"),
                ))

    async def _rest_poll_loop(self) -> None:
        """Runs continuously as the backfill/seed path -- always on, even
        when IRIS is active, but at a much slower cadence once IRIS is
        doing the real-time work (IRIS covers the update latency; REST
        here just guards against a missed/dropped IRIS message).
        """
        interval = self.settings.rest_poll_interval_seconds
        while not self._stop.is_set():
            try:
                await self._rest_refresh_once()
            except Exception:
                logger.exception("REST refresh cycle failed, will retry next interval")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _on_iris_message(self, dataset: str, payload: dict) -> None:
        key = IRIS_DATASET_TO_BUFFER_KEY.get(dataset)
        if key is None:
            return
        self.buffers.add(key, _records_from_payload(payload))
        self._dirty.set()

    async def _recompute_loop(self) -> None:
        while not self._stop.is_set():
            await self._dirty.wait()
            # Debounce: absorb a burst of near-simultaneous IRIS messages
            # (a settlement-period boundary tends to publish several
            # datasets' revisions within milliseconds of each other) into
            # one recompute instead of one per message.
            await asyncio.sleep(DEBOUNCE_SECONDS)
            self._dirty.clear()
            try:
                await self._recompute_and_persist()
            except Exception:
                logger.exception("recompute cycle failed")

    async def _persist_period(self, sd, sp, group: pd.DataFrame) -> None:
        rows = [
            db.StackRow(
                settlement_date=sd.date() if hasattr(sd, "date") else sd,
                settlement_period=int(sp),
                bm_unit=r["bmUnit"],
                stor_flag=bool(r["storFlag"]),
                deemed_bo_flag=bool(r["deemedBoFlag"]),
                so_flag=bool(r["soFlag"]),
                acceptance_number=int(r["acceptanceNumber"]),
                reversal=float(r["reversal"]),
                ap_mult_vol=float(r["ap_mult_vol"]),
                delta=float(r["delta"]),
                vol_to_price=float(r["vol_to_price"]),
                disbsad_cost=float(r["disbsad_cost"]),
                m_orig_price=float(r["m_orig_price"]),
                total_delta=float(r["total_delta"]),
                delta_sign=float(r["delta_sign"]),
                action_sign=float(r["action_sign"]),
                vol_for_price=float(r["vol_for_price"]) if pd.notna(r["vol_for_price"]) else None,
                misik_imb_price=float(r["misik_imb_price"]),
                total_misik_price=float(r["total_misik_price"]),
                max_ta=r["max_ta"].to_pydatetime() if hasattr(r["max_ta"], "to_pydatetime") else None,
            )
            for _, r in group.iterrows()
        ]
        await db.replace_period_rows(self.pool, rows[0].settlement_date, rows[0].settlement_period, rows)

        # The rolling window (see settlement.rolling_window) reaches 2
        # periods into the future -- deliberately so: Gate Closure (1
        # hour before delivery) has already passed for both of them by
        # definition, so their BOD/FPN data is final, not speculative,
        # same as the user's own original tool showing current_sp+1/+2.
        # These ARE broadcast (unlike an earlier, over-corrected version
        # of this code that suppressed them entirely) -- the live page
        # tells them apart from the truly-delivering period via the
        # `is_current` flag on each message (see broadcast.py), not by
        # withholding the data.
        await self.broadcaster.publish(rows[0].settlement_date, rows[0].settlement_period, [vars(r) for r in rows])

    async def _persist_spot_niv_period(self, sd, sp, group: pd.DataFrame) -> None:
        rows = [{"spot_time": r.spot_time.to_pydatetime(), "niv_spot_time_max": float(r.niv_spot_time_max)} for r in group.itertuples()]
        sd_date = sd.date() if hasattr(sd, "date") else sd
        await db.replace_pricing_stack_niv_spot_time_period(self.pool, sd_date, int(sp), rows)

    async def _recompute_and_persist(self) -> None:
        boalf = self.buffers.frame("boalf")
        if boalf.empty:
            return
        bod, pn, mel, disbsad = self.buffers.frame("bod"), self.buffers.frame("pn"), self.buffers.frame("mel"), self.buffers.frame("disbsad")
        compute = functools.partial(
            stack_engine.compute_stack, boalf, bod, pn, mel, disbsad,
            market_index_prices=self.market_index_prices, return_spot_niv=True,
        )
        if self.process_pool is not None:
            loop = asyncio.get_running_loop()
            result, spot_niv = await loop.run_in_executor(self.process_pool, compute)
        else:
            result, spot_niv = compute()
        if result.empty:
            return

        # Each period's own persist+broadcast used to run one full round-trip
        # at a time (delete+insert, then wait for the next period); every
        # period touches disjoint rows/messages, so there's no correctness
        # reason for that serialisation -- bounded by a semaphore (not a
        # bare gather) so a wide window doesn't claim every pool connection
        # at once (see PERSIST_CONCURRENCY).
        semaphore = asyncio.Semaphore(PERSIST_CONCURRENCY)

        async def _bounded(coro) -> None:
            async with semaphore:
                await coro

        tasks = [_bounded(self._persist_period(sd, sp, group)) for (sd, sp), group in result.groupby(["settlementDate", "settlementPeriod"])]
        if not spot_niv.empty:
            tasks += [_bounded(self._persist_spot_niv_period(sd, sp, group)) for (sd, sp), group in spot_niv.groupby(["settlementDate", "settlementPeriod"])]
        await asyncio.gather(*tasks)

    async def run(self) -> None:
        tasks = [asyncio.create_task(self._rest_poll_loop()), asyncio.create_task(self._recompute_loop())]
        if self.settings.iris_configured:
            logger.info("IRIS credentials found -- starting real-time push consumer")
            tasks.append(asyncio.create_task(run_iris_consumer_with_restart(self.settings, self._on_iris_message, self._stop)))
        else:
            logger.info("IRIS not configured (.env) -- running on REST polling only, every %ss", self.settings.rest_poll_interval_seconds)
        await asyncio.gather(*tasks)

    async def stop(self) -> None:
        self._stop.set()
        await self._http.aclose()


def elexon_rest_generate_hour_pairs(now: datetime) -> list[tuple[str, str]]:
    """(from, to) ISO hour-boundary pairs spanning `MEL_MIL_LOOKAHEAD_HOURS`
    around `now` -- MELS/MILS are windowed by publish time, not settlement
    period, same as the original notebook's generate_mel_mil_date_pairs().
    """
    base = now.replace(minute=0, second=0, microsecond=0)
    timestamps = [base + pd.Timedelta(hours=h) for h in MEL_MIL_LOOKAHEAD_HOURS]
    fmt = "%Y-%m-%dT%H:%MZ"
    return [(timestamps[i].strftime(fmt), timestamps[i + 1].strftime(fmt)) for i in range(len(timestamps) - 1)]
