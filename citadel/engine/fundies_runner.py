"""Orchestrates the Fundies dashboard pipeline -- a third sibling of
engine/runner.py's Runner and engine/fpn_runner.py's FpnRunner, same
overall shape (debounced recompute, persist, broadcast) but with one
genuine difference: per the user's own request ("refreshing parallelly and
based on their set intervals"), **every dataset gets its own independent
asyncio loop at its own interval** (FUNDIES_REFRESH_INTERVALS below)
instead of one shared poll cycle -- closer to what "their set intervals"
(plural) actually means than either reference implementation manages in
practice (the real Zapdos app's own per-row `refreshInterval` config field
is real, but every row happens to be set to the same 60s; the notebook is
a single `while True: ... sleep(60)` loop for everything).

Five datasets (ENTSO-E, SEMO, IMBALNGC, the NESO embedded-forecast CSV,
EPEX) are the fragile/rate-limited/scrape-based ones the notebook itself
protects with a Drive pickle fallback -- here they're written straight to
their own small Postgres cache table on every successful fetch
(db.upsert_fundies_cache) and READ BACK from that table on every
recompute, regardless of which dataset's own tick triggered it, so a
slow/failed fetch never blanks a value that was already known good (see
schema.sql's own comment on these five tables). Every other dataset
(NDF/WINDFOR/INDO/ITSDO/FUELHH/bid-stack/PV_Live/system-prices/market-
index/NESO-SO-trades/DISBSAD/nuclear) is fast and reliably available from
Elexon/NESO directly, so it's kept as a plain in-memory buffer instead
(same reasoning FpnRunner's own FpnBuffers already establishes).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import httpx
import pandas as pd

from ..config import Settings
from ..ingest import elexon_rest, entsoe_flows, epex, neso, pvlive, semo
from ..settlement import current_period, rolling_window
from ..storage import db
from . import fundies as fundies_engine
from .fpn import fuel_type_reference

if TYPE_CHECKING:
    from ..api.fundies_broadcast import FundiesBroadcaster

logger = logging.getLogger("citadel.engine.fundies_runner")

DEBOUNCE_SECONDS = 2.0

# Per-dataset refresh cadence, in seconds -- module-level tuning constants
# (like fpn_runner.py's own DEBOUNCE_SECONDS/GENERATION_BY_FUEL_RETENTION),
# not `.env` settings, since these aren't environment-specific toggles.
# Slower sources are genuinely slower-moving (the embedded forecast CSV is
# a rolling daily publish; SEMO's two intraday auctions don't need
# per-minute polling) rather than an arbitrary choice.
FUNDIES_REFRESH_INTERVALS = {
    "bm_unit_reference": 300,
    "ndf": 60,
    "windfor": 60,
    "da_ndf": 300,
    "da_windfor": 300,
    "indo": 60,
    "itsdo": 60,
    "fuelhh": 60,
    "pv_live": 60,
    "system_prices": 60,
    "market_index": 60,
    "natgrid": 60,
    "imbalngc": 120,
    "entsoe": 60,
    "semo": 120,
    "embedded_forecast": 300,
    "epex": 60,
    "nuclear": 900,
}


def _yesterday_to_tomorrow_window() -> tuple[str, str]:
    """A fixed 3-day publish-time window (yesterday 00:00 -> tomorrow
    24:00 UTC) instead of the narrower rolling_window()-based one the
    pricing stack/FPN pipelines use -- the Fundies page's own date
    selector (see fundies.js) lets a trader look at yesterday's or
    tomorrow's fundamentals, not just the live window around now.
    """
    today_midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    frm = (today_midnight - timedelta(days=1)).strftime("%Y-%m-%dT%H:%MZ")
    to = (today_midnight + timedelta(days=2)).strftime("%Y-%m-%dT%H:%MZ")
    return frm, to


class FundiesRunner:
    def __init__(self, settings: Settings, pool, broadcaster: "FundiesBroadcaster", process_pool) -> None:
        self.settings = settings
        self.pool = pool
        self.broadcaster = broadcaster
        self.process_pool = process_pool
        self._http = httpx.AsyncClient()
        self._dirty = asyncio.Event()
        self._stop = asyncio.Event()

        self.bm_unit_reference = pd.DataFrame()
        self.buffers: dict[str, list[dict]] = {
            "ndf": [], "windfor": [], "da_ndf": [], "da_windfor": [], "indo": [], "itsdo": [],
            "fuelhh": [], "bid_stack": [], "pv_live": [], "system_prices": [], "market_index": [],
            "neso_trades": [], "disbsad": [],
        }
        self.nuclear_output_usable: float | None = None

    # -- per-dataset refresh methods -------------------------------------

    async def _refresh_bm_unit_reference(self) -> None:
        rows = await db.bm_unit_reference_all(self.pool)
        self.bm_unit_reference = pd.DataFrame([dict(r) for r in rows])
        self._dirty.set()

    async def _refresh_ndf(self) -> None:
        frm, to = _yesterday_to_tomorrow_window()
        self.buffers["ndf"] = await elexon_rest.fetch_ndf(self._http, frm, to)
        self._dirty.set()

    async def _refresh_da_ndf(self) -> None:
        # Publish-time window, not delivery-date -- wide enough to catch
        # yesterday's day-ahead publish (made ~1 day before its own
        # delivery date) through today's (covering tomorrow's delivery),
        # for the date selector's yesterday/tomorrow options.
        today_midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        frm = (today_midnight - timedelta(days=2)).strftime("%Y-%m-%dT%H:%MZ")
        to = (today_midnight + timedelta(days=1)).strftime("%Y-%m-%dT%H:%MZ")
        self.buffers["da_ndf"] = await elexon_rest.fetch_ndf(self._http, frm, to)
        self._dirty.set()

    async def _refresh_windfor(self) -> None:
        frm, to = _yesterday_to_tomorrow_window()
        self.buffers["windfor"] = await elexon_rest.fetch_windfor(self._http, frm, to)
        self._dirty.set()

    async def _refresh_da_windfor(self) -> None:
        today_midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        frm = (today_midnight - timedelta(days=2)).strftime("%Y-%m-%dT%H:%MZ")
        to = (today_midnight + timedelta(days=1)).strftime("%Y-%m-%dT%H:%MZ")
        self.buffers["da_windfor"] = await elexon_rest.fetch_windfor(self._http, frm, to)
        self._dirty.set()

    async def _refresh_indo(self) -> None:
        frm, to = _yesterday_to_tomorrow_window()
        self.buffers["indo"] = await elexon_rest.fetch_indo(self._http, frm, to)
        self._dirty.set()

    async def _refresh_itsdo(self) -> None:
        frm, to = _yesterday_to_tomorrow_window()
        self.buffers["itsdo"] = await elexon_rest.fetch_itsdo(self._http, frm, to)
        self._dirty.set()

    async def _refresh_fuelhh(self) -> None:
        cur = current_period()
        yesterday = cur.settlement_date - timedelta(days=1)
        # FUELHH is outturn (settled), so "tomorrow" never has any -- only
        # yesterday+today are worth fetching for the date selector.
        fuelhh = await elexon_rest.fetch_fuelhh(self._http, yesterday, cur.settlement_date)
        self.buffers["fuelhh"] = fuelhh

        # The wind-curtailment estimate needs the accepted-bid stack for
        # every period FUELHH has wind data for -- fetched here, right
        # after FUELHH itself, rather than as its own independent interval
        # (it has no meaning without a fresh FUELHH to pair it with).
        wind_periods = sorted({
            r["settlementPeriod"] for r in fuelhh
            if r.get("fuelType") == "WIND" and r.get("settlementDate") == cur.settlement_date.isoformat()
        })
        try:
            tasks = [elexon_rest.fetch_bid_stack(self._http, cur.settlement_date, sp) for sp in wind_periods]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            bid_stack: list[dict] = []
            for r in results:
                if not isinstance(r, BaseException):
                    bid_stack.extend(r)
            self.buffers["bid_stack"] = bid_stack
        except Exception:
            logger.warning("bid-stack fetch failed, wind curtailment estimate will fall back to metered outturn", exc_info=True)
            self.buffers["bid_stack"] = []
        self._dirty.set()

    async def _refresh_pv_live(self) -> None:
        # Was `today` -> `tomorrow` only -- same missing-yesterday bug
        # already fixed for _refresh_system_prices()/_refresh_market_index()
        # below: the Fundies date selector's "Yesterday" view had no
        # pv_live at all, since this never asked PV_Live's API for it.
        # Widened to `yesterday` -> `tomorrow`, one call covering all three
        # days PV_Live's own start/end range supports directly (unlike the
        # per-day REST calls those other two fixes needed).
        now = datetime.now(timezone.utc)
        yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%dT00:00")
        tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%dT00:00")
        self.buffers["pv_live"] = await pvlive.fetch_pv_live(self._http, yesterday, tomorrow)
        self._dirty.set()

    async def _refresh_system_prices(self) -> None:
        # Confirmed live: this only ever fetched TODAY, so the Fundies
        # date selector's "Yesterday" view had no NIV/Imbalance Price at
        # all. System prices are a settled outturn (no "tomorrow" -- a
        # future period has no settlement price yet), so yesterday+today
        # is the full useful range, same reasoning as the demand/wind
        # datasets' own _yesterday_to_tomorrow_window() (minus tomorrow).
        cur = current_period()
        yesterday = cur.settlement_date - timedelta(days=1)
        rows = await asyncio.gather(
            elexon_rest.fetch_system_price(self._http, yesterday),
            elexon_rest.fetch_system_price(self._http, cur.settlement_date),
        )
        self.buffers["system_prices"] = rows[0] + rows[1]
        self._dirty.set()

    async def _refresh_market_index(self) -> None:
        # Confirmed live: rolling_window()'s own min date only reaches a
        # few hours back from "now" (5 periods = 2.5h), nowhere near
        # yesterday -- MIP/MIV were missing for yesterday entirely and
        # incomplete for today's own earlier periods. Same
        # yesterday-to-tomorrow window as the demand/wind datasets now.
        frm, to = _yesterday_to_tomorrow_window()
        self.buffers["market_index"] = await elexon_rest.fetch_market_index(self._http, frm, to)
        self._dirty.set()

    async def _refresh_natgrid(self) -> None:
        cur = current_period()
        today_midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            trades = await neso.fetch_national_grid_trades(
                self._http, today_midnight.strftime("%Y-%m-%dT%H:%M:%S"), (today_midnight + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")
            )
            self.buffers["neso_trades"] = trades
        except Exception:
            # Keep the previous good trades rather than blanking the natgrid
            # row for the whole day over one failed fetch.
            logger.warning("NESO national grid trades fetch failed, keeping the last fetched trades", exc_info=True)
        try:
            self.buffers["disbsad"] = await elexon_rest.fetch_disbsad(self._http, cur.settlement_date, cur.settlement_period)
        except Exception:
            logger.warning("DISBSAD fetch failed, keeping the last fetched volumes", exc_info=True)
        self._dirty.set()

    async def _refresh_imbalngc(self) -> None:
        now = datetime.now(timezone.utc)
        frm = (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%MZ")
        to = now.strftime("%Y-%m-%dT%H:%MZ")
        rows = await elexon_rest.fetch_imbalngc(self._http, frm, to)
        cache_rows = [
            {"settlement_date": date.fromisoformat(r["settlementDate"]), "settlement_period": r["settlementPeriod"], "imbalngc": float(r["imbalance"]) * -1}
            for r in rows if r.get("imbalance") is not None
        ]
        await db.upsert_fundies_cache(self.pool, "fundies_imbalngc", db.FUNDIES_IMBALNGC_COLUMNS, cache_rows)
        self._dirty.set()

    async def _refresh_nuclear(self) -> None:
        cur = current_period()
        publish_date = cur.settlement_date - timedelta(days=3)
        try:
            rows = await elexon_rest.fetch_fou2t14d_nuclear(self._http, publish_date)
            today_rows = [r for r in rows if r.get("forecastDate") == cur.settlement_date.isoformat()]
            self.nuclear_output_usable = float(today_rows[0]["outputUsable"]) if today_rows else None
        except Exception:
            logger.warning("FOU2T14D nuclear fetch failed, nuke_214 keeps its last known value", exc_info=True)
        self._dirty.set()

    async def _refresh_entsoe(self) -> None:
        if not self.settings.entsoe_key:
            return
        try:
            date_range = entsoe_flows.get_entsoe_date_range()
            flows = await entsoe_flows.fetch_all_pairs(self.settings.entsoe_key, date_range)
            if not flows.empty:
                flows = flows.rename(columns=fundies_engine.ENTSOE_NET_COLUMN_RENAME)
                sd, sp = fundies_engine._sp_from_timestamps(pd.to_datetime(flows["gmt_time"], utc=True))
                flows = fundies_engine.broadcast_hourly_to_half_hourly(
                    flows.assign(settlementDate=sd, settlementPeriod=sp).drop(columns=["gmt_time"])
                )
                cache_rows = [
                    {"settlement_date": r["settlementDate"], "settlement_period": r["settlementPeriod"],
                     **{c: r.get(c) for c in db.FUNDIES_ENTSOE_FLOWS_COLUMNS}}
                    for r in flows.to_dict("records")
                ]
                await db.upsert_fundies_cache(self.pool, "fundies_entsoe_flows", db.FUNDIES_ENTSOE_FLOWS_COLUMNS, cache_rows)
        except Exception:
            logger.warning("ENTSO-E interconnector flow fetch failed, rows keep their last known values", exc_info=True)
        self._dirty.set()

    async def _refresh_semo(self) -> None:
        # Starts from yesterday (not just today) so the Fundies date
        # selector's "Yesterday" option has scheduled Ireland/NI flows too
        # -- same reasoning as entsoe_flows.get_entsoe_date_range()'s own
        # yesterday inclusion.
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
        end = (now + timedelta(hours=24)).strftime("%Y-%m-%dT00:00:00") if now.hour >= 15 else now.strftime("%Y-%m-%dT00:00:00")
        try:
            ida1, ida2 = await asyncio.gather(
                semo.fetch_scheduled_flows(self._http, "IDA1", start, end),
                semo.fetch_scheduled_flows(self._http, "IDA2", start, end),
            )
            flows = fundies_engine.semo_net_flows(ida1, ida2)
            if not flows.empty:
                cache_rows = [
                    {"settlement_date": r["settlementDate"], "settlement_period": r["settlementPeriod"],
                     **{c: r.get(c) for c in db.FUNDIES_SEMO_FLOWS_COLUMNS}}
                    for r in flows.to_dict("records")
                ]
                await db.upsert_fundies_cache(self.pool, "fundies_semo_flows", db.FUNDIES_SEMO_FLOWS_COLUMNS, cache_rows)
        except Exception:
            logger.warning("SEMO Ireland flow fetch failed, rows keep their last known values", exc_info=True)
        self._dirty.set()

    async def _refresh_embedded_forecast(self) -> None:
        try:
            rows = await neso.fetch_embedded_forecast(self._http)
            cache_rows = []
            for r in rows:
                try:
                    cache_rows.append({
                        "settlement_date": date.fromisoformat(r["SETTLEMENT_DATE"][:10]),
                        "settlement_period": int(r["SETTLEMENT_PERIOD"]),
                        "embedded_wind_forecast": float(r["EMBEDDED_WIND_FORECAST"]) if r.get("EMBEDDED_WIND_FORECAST") else None,
                        "embedded_solar_forecast": float(r["EMBEDDED_SOLAR_FORECAST"]) if r.get("EMBEDDED_SOLAR_FORECAST") else None,
                    })
                except (KeyError, ValueError):
                    continue
            await db.upsert_fundies_cache(self.pool, "fundies_embedded_forecast", db.FUNDIES_EMBEDDED_FORECAST_COLUMNS, cache_rows)
        except Exception:
            logger.warning("NESO embedded wind/solar forecast fetch failed, values keep their last known ones", exc_info=True)
        self._dirty.set()

    async def _store_epex_auction(self, html: str, delivery_date: date) -> None:
        parsed = epex.parse_auction_data(html)
        cache_rows = []
        for row in parsed["hourly_data"]:
            hour = int(row["hour_range"].split("-")[0].split(":")[0])
            sp = hour * 2 + 1
            for offset in (0, 1):
                cache_rows.append({
                    "settlement_date": delivery_date, "settlement_period": sp + offset,
                    "da_price": row["price"], "da_volume": row["volume"],
                })
        await db.upsert_fundies_cache(self.pool, "fundies_epex_da", db.FUNDIES_EPEX_DA_COLUMNS, cache_rows)

    async def _refresh_epex(self) -> None:
        existing_dates = await db.fundies_epex_existing_delivery_dates(self.pool)

        # Backfill yesterday's own auction once -- should_fetch_da_prices()'s
        # cutoff logic only ever targets today's or tomorrow's delivery
        # date (whichever the time of day means), so without this,
        # yesterday's da_price/da_volume are never fetched at all even
        # though EPEX's own site still has that auction's results.
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
        if yesterday not in existing_dates:
            try:
                html, delivery_date = await epex.fetch_da_prices_for_delivery_date(self._http, yesterday)
                await self._store_epex_auction(html, delivery_date)
            except Exception:
                logger.warning("EPEX yesterday backfill scrape failed, will retry next interval", exc_info=True)

        if epex.should_fetch_da_prices(existing_dates):
            try:
                html, delivery_date = await epex.fetch_da_prices(self._http)
                await self._store_epex_auction(html, delivery_date)
            except Exception:
                logger.warning("EPEX day-ahead auction scrape failed, da_price/da_volume keep their last known values", exc_info=True)
        self._dirty.set()

    # -- generic periodic wrapper -----------------------------------------

    async def _periodic(self, name: str, coro_fn) -> None:
        interval = FUNDIES_REFRESH_INTERVALS[name]
        while not self._stop.is_set():
            try:
                await coro_fn()
            except Exception:
                logger.exception("Fundies %s refresh failed, will retry next interval", name)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    # -- recompute ----------------------------------------------------------

    async def _recompute_loop(self) -> None:
        while not self._stop.is_set():
            await self._dirty.wait()
            await asyncio.sleep(DEBOUNCE_SECONDS)
            self._dirty.clear()
            try:
                await self._recompute_and_persist()
            except Exception:
                logger.exception("Fundies recompute cycle failed")

    async def _recompute_and_persist(self) -> None:
        if self.bm_unit_reference.empty or not self.buffers["ndf"]:
            return
        # See engine/runner.py's own fuel_ref assignment for why
        # other_fallback must be threaded through here too.
        fuel_ref = fuel_type_reference(self.bm_unit_reference, other_fallback=self.settings.fpn_other_fallback_enabled)

        cur = current_period()
        dates = sorted({
            cur.settlement_date - timedelta(days=1), cur.settlement_date, cur.settlement_date + timedelta(days=1),
        } | {sd for sd, _ in rolling_window()})

        entsoe_rows = await db.fundies_cache_rows(self.pool, "fundies_entsoe_flows", dates)
        semo_rows = await db.fundies_cache_rows(self.pool, "fundies_semo_flows", dates)
        imbalngc_rows = await db.fundies_cache_rows(self.pool, "fundies_imbalngc", dates)
        embedded_rows = await db.fundies_cache_rows(self.pool, "fundies_embedded_forecast", dates)
        epex_rows = await db.fundies_cache_rows(self.pool, "fundies_epex_da", dates)

        loop = asyncio.get_running_loop()
        real_time, day_ahead = await loop.run_in_executor(
            self.process_pool, _compute_fundies,
            dict(self.buffers), self.nuclear_output_usable, fuel_ref,
            [dict(r) for r in entsoe_rows], [dict(r) for r in semo_rows],
            [dict(r) for r in imbalngc_rows], [dict(r) for r in embedded_rows], [dict(r) for r in epex_rows],
        )
        if real_time.empty and day_ahead.empty:
            return

        await self._persist(real_time, day_ahead)
        await self.broadcaster.publish({"type": "fundies_update"})

    async def _persist(self, real_time: pd.DataFrame, day_ahead: pd.DataFrame) -> None:
        rt_rows = _rows_for(real_time, db.FUNDIES_REAL_TIME_COLUMNS, {
            "settlementDate": "settlement_date", "settlementPeriod": "settlement_period",
            "netImbalanceVolume": "net_imbalance_volume", "systemBuyPrice": "system_buy_price",
            "buyPriceAdjustment": "buy_price_adjustment", "sellPriceAdjustment": "sell_price_adjustment",
        })
        # Only the join keys need renaming -- the cache-sourced columns
        # (eleclink_net, embedded_wind_forecast, da_price, etc.) already
        # arrive snake_case from their own cache tables (see
        # fundies_engine.build_day_ahead()'s own docstring).
        da_rows = _rows_for(day_ahead, db.FUNDIES_DAY_AHEAD_COLUMNS, {
            "settlementDate": "settlement_date", "settlementPeriod": "settlement_period",
        })

        async def _write(table: str, columns: list[str], by_period: dict) -> None:
            # A period's rows are replaced wholesale, so a value this cycle
            # couldn't compute (a failed fetch, an empty buffer after a
            # restart) would overwrite the last good one with NULL. Carry the
            # stored value forward for any column that is empty now -- the
            # same "combine_first" behaviour the source notebook's pickles
            # gave it.
            stored: dict[tuple, dict] = {}
            for sd in {pd.Timestamp(k[0]).date() for k in by_period}:
                for rec in await self.pool.fetch(f"SELECT * FROM {table} WHERE settlement_date = $1", sd):  # noqa: S608 -- internal table name
                    stored[(rec["settlement_date"], rec["settlement_period"])] = dict(rec)
            for (sd, sp), period_rows in by_period.items():
                old_row = stored.get((pd.Timestamp(sd).date(), int(sp)))
                if old_row:
                    for row in period_rows:
                        for c in columns:
                            v = row.get(c)
                            if (v is None or (isinstance(v, float) and v != v)) and old_row.get(c) is not None:
                                row[c] = old_row[c]
                await db.replace_fpn_period_rows(self.pool, table, (), sd, int(sp), period_rows, columns)

        await _write("fundies_real_time", db.FUNDIES_REAL_TIME_COLUMNS, _group_by_period(rt_rows))
        await _write("fundies_day_ahead", db.FUNDIES_DAY_AHEAD_COLUMNS, _group_by_period(da_rows))

    async def run(self) -> None:
        loops = [self._periodic(name, getattr(self, f"_refresh_{name}")) for name in FUNDIES_REFRESH_INTERVALS]
        await asyncio.gather(*loops, self._recompute_loop())

    async def stop(self) -> None:
        self._stop.set()
        await self._http.aclose()


def _compute_fundies(
    buffers: dict, nuclear_output_usable, fuel_ref: pd.DataFrame,
    entsoe_rows: list[dict], semo_rows: list[dict], imbalngc_rows: list[dict],
    embedded_rows: list[dict], epex_rows: list[dict],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Runs in the shared ProcessPoolExecutor -- module-level (not a
    method) so it's picklable, same reasoning as fpn_runner.py calling
    fpn_engine.compute directly instead of a bound method. The five
    `*_rows` cache-table reads are already in their final, normalized
    per-SP shape (see fundies_engine.build_day_ahead()'s own docstring on
    why) -- passed straight through, no reshaping needed here.
    """
    real_time = fundies_engine.build_real_time(
        buffers["ndf"], buffers["windfor"], buffers["indo"], buffers["itsdo"],
        buffers["fuelhh"], buffers["bid_stack"], fuel_ref,
        buffers["pv_live"], buffers["system_prices"], buffers["market_index"],
        buffers["neso_trades"], buffers["disbsad"], imbalngc_rows,
    )
    windfor_df = fundies_engine.build_windfor_frame(buffers["windfor"], "latestwindfor")
    latest_ndf_df = fundies_engine._latest_publish_per_period(buffers["ndf"], "settlementDate", "settlementPeriod", "demand", "latest_ndf")

    day_ahead = fundies_engine.build_day_ahead(
        buffers["da_ndf"], latest_ndf_df, buffers["da_windfor"], windfor_df,
        entsoe_rows, nuclear_output_usable, semo_rows, embedded_rows, epex_rows,
    )

    derived = fundies_engine.build_derived_rows(real_time, day_ahead)
    day_ahead = pd.merge(day_ahead, derived, on=["settlementDate", "settlementPeriod"], how="left")
    return real_time, day_ahead


def _rows_for(df: pd.DataFrame, columns: list[str], rename: dict[str, str]) -> pd.DataFrame:
    if df.empty:
        return df
    renamed = df.rename(columns=rename)
    for c in columns:
        if c not in renamed.columns:
            renamed[c] = None
    renamed = renamed[columns].astype(object).where(pd.notnull(renamed[columns]), None)
    return renamed


def _group_by_period(df: pd.DataFrame) -> dict:
    if df.empty:
        return {}
    by_period: dict[tuple, list[dict]] = {}
    for row in df.to_dict("records"):
        key = (row["settlement_date"], row["settlement_period"])
        by_period.setdefault(key, []).append(row)
    return by_period
