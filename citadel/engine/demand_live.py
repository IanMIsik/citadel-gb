"""Live demand-error predictions for the FPN page (dev only: only mounted when DEMAND_FORECASTER_ENABLED).

For every settlement period from three hours back to about eighteen hours ahead it reports what the saved model
expects INDO - NDF to be, with a 10-90% band. A period `lead` minutes from now uses the model's smallest trained lead
bucket that is at least that far ahead (a period 95 minutes away uses the 120-minute row), so the features only ever
contain what was already known at that earlier moment, exactly as in training. A period that has started or ended
uses the 0-minute row; once its INDO is published the actual error is returned next to the prediction.

The service refreshes itself every few minutes in the background (fresh Elexon/NESO vintages, the weather snapshot)
and the API just returns the cached result.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from citadel.engine import demand_features as feat
from citadel.engine import demand_model
from citadel.settlement import utc_to_settlement

logger = logging.getLogger(__name__)

LEADS = (0, 30, 60, 120, 240, 480, 720, 1080)
LOOK_BACK = timedelta(hours=3)
LOOK_AHEAD = timedelta(hours=18, minutes=30)
HISTORY_DAYS = 16
DEFAULT_MODEL_DIR = "data/models"
DEFAULT_REFRESH_SECONDS = 300
OFFICIAL_DATASETS = ["ndf", "windfor", "outturn", "fuelinst", "pv_live"]


def lead_bucket_for(start: datetime, now: datetime, leads=LEADS) -> int | None:
    """Smallest trained lead that is at least as far ahead as `start` is from `now` (0 for anything already started)."""
    need = max(0.0, (start - now).total_seconds() / 60.0)
    for lead in leads:
        if lead >= need:
            return lead
    return None


@dataclass
class LoadedModel:
    features: list[str]
    boosters: dict[float, object]
    offsets: dict[int, float]
    trained_through: str | None


def load_model(directory: str | Path) -> LoadedModel:
    import lightgbm as lgb

    d = Path(directory)
    meta = json.loads((d / "demand_gbm_meta.json").read_text())
    boosters = {float(q): lgb.Booster(model_file=str(d / f"demand_gbm_q{int(round(float(q) * 100))}.txt")) for q in meta["quantiles"]}
    offsets = {int(k): float(v) for k, v in meta.get("band", {}).get("static_offsets_mw", {}).items()}
    return LoadedModel(meta["features"], boosters, offsets, meta.get("trained_through"))


def _finite(v) -> float | None:
    return None if v is None or (isinstance(v, float) and not math.isfinite(v)) else float(v)


def predict_periods(base: pd.DataFrame, model: LoadedModel, now: datetime, leads=LEADS) -> list[dict]:
    """One record per period in the display window. `base` is demand_features.build_base output covering at least
    the recent history and the periods ahead."""
    now_ts = pd.Timestamp(now).tz_convert("UTC") if pd.Timestamp(now).tzinfo else pd.Timestamp(now, tz="UTC")
    window = base.index[(base.index >= (now_ts - LOOK_BACK).floor("30min")) & (base.index <= now_ts + LOOK_AHEAD)]
    present = [lead for lead in leads if f"ndf_L{lead}" in base.columns]
    if not len(window) or not present:
        return []
    feature_frames = {lead: feat.lead_features(base, lead)[0] for lead in present}
    chosen: dict[pd.Timestamp, int] = {}
    for start in window:
        lead = lead_bucket_for(start.to_pydatetime(), now_ts.to_pydatetime(), present)
        if lead is not None:
            chosen[start] = lead

    records: list[dict] = []
    for lead in sorted(set(chosen.values())):
        starts = [s for s, l in chosen.items() if l == lead]
        X = feature_frames[lead].reindex(starts).reindex(columns=model.features)
        ok = X["ndf"].notna() if "ndf" in X else pd.Series(False, index=X.index)
        X = X[ok]
        if X.empty:
            continue
        raw = pd.DataFrame({f"q{int(round(q * 100))}": b.predict(X) for q, b in sorted(model.boosters.items())}, index=X.index)
        arr = np.sort(raw.to_numpy(), axis=1)
        widen = model.offsets.get(lead, 0.0)
        for i, start in enumerate(X.index):
            q10, q50, q90 = arr[i, 0] - widen, arr[i, 1], arr[i, 2] + widen
            ndf = float(X.loc[start, "ndf"])
            indo = base["indo"].get(start) if "indo" in base else None
            sd, sp = utc_to_settlement(start.to_pydatetime())
            records.append({
                "settlement_date": sd.isoformat(), "settlement_period": int(sp), "start": start.isoformat(), "lead_used_min": int(lead),
                "ndf": ndf, "pred_error": _finite(q50), "pred_error_low": _finite(q10), "pred_error_high": _finite(q90),
                "pred_demand": _finite(ndf + q50),
                "actual_error": _finite(float(indo) - ndf) if indo is not None and not pd.isna(indo) and indo > 0 else None,
            })
    return sorted(records, key=lambda r: r["start"])


class DemandPredictor:
    def __init__(self, pool, model_dir: str | None = None, refresh_seconds: int | None = None):
        self.pool = pool
        self.model_dir = model_dir or os.environ.get("DEMAND_MODEL_DIR", DEFAULT_MODEL_DIR)
        self.refresh_seconds = refresh_seconds or int(os.environ.get("DEMAND_LIVE_REFRESH_SECONDS", DEFAULT_REFRESH_SECONDS))
        self.model: LoadedModel | None = None
        self.state: dict = {"enabled": True, "status": "starting", "computed_at": None, "periods": []}
        self._task: asyncio.Task | None = None
        self._last_weather_snapshot: datetime | None = None
        self._last_embedded: datetime | None = None

    async def ensure_started(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception as e:  # noqa: BLE001 -- keep serving the last good result
                logger.warning("demand predictor refresh failed", exc_info=True)
                self.state = {**self.state, "status": "error", "error": f"{type(e).__name__}: {e}"}
            await asyncio.sleep(self.refresh_seconds)

    async def refresh(self) -> None:
        now = datetime.now(timezone.utc)
        if self.model is None:
            try:
                self.model = await asyncio.to_thread(load_model, self.model_dir)
            except (FileNotFoundError, ImportError) as e:
                self.state = {"enabled": True, "status": "no_model", "computed_at": None, "periods": [],
                              "error": f"{e}. Train one with: citadel train-demand-model"}
                return
        await self._ingest(now)
        base = await demand_model.load_base(self.pool, now - timedelta(days=HISTORY_DAYS), now + timedelta(days=2),
                                            weather_override=await self._weather(now))
        periods = await asyncio.to_thread(predict_periods, base, self.model, now)
        self.state = {"enabled": True, "status": "ok", "computed_at": now.isoformat(), "model_trained_through": self.model.trained_through,
                      "periods": periods}

    async def _ingest(self, now: datetime) -> None:
        """Keep the demand_* tables fresh: official vintages every cycle, the weather snapshot hourly, NESO issues every 15 min."""
        import httpx

        from citadel.ingest import demand_backfill as bf
        from citadel.ingest import forecast_snapshots as fs

        today = now.date()
        try:
            await bf.run_backfill(self.pool, today - timedelta(days=2), today, OFFICIAL_DATASETS, concurrency=3, force=True)
        except Exception:  # noqa: BLE001
            logger.warning("live ingest of official vintages failed", exc_info=True)
        async with httpx.AsyncClient(timeout=httpx.Timeout(40.0)) as client:
            if self._last_weather_snapshot is None or now - self._last_weather_snapshot > timedelta(minutes=55):
                try:
                    await fs.snapshot_weather(client, self.pool)
                    self._last_weather_snapshot = now
                except Exception:  # noqa: BLE001
                    logger.warning("live weather snapshot failed", exc_info=True)
            if self._last_embedded is None or now - self._last_embedded > timedelta(minutes=15):
                try:
                    await fs.refresh_embedded_recent(client, self.pool, days=1)
                    self._last_embedded = now
                except Exception:  # noqa: BLE001
                    logger.warning("live embedded refresh failed", exc_info=True)

    async def _weather(self, now: datetime) -> pd.DataFrame:
        """Re-analysed weather for the past, topped up with the newest forecast snapshot for hours it doesn't cover yet."""
        start = now - timedelta(days=HISTORY_DAYS)
        cols = "point, ts, temperature_2m, cloud_cover, shortwave_radiation, wind_speed_100m"
        past = await self.pool.fetch(f"SELECT {cols} FROM demand_weather WHERE ts >= $1", start)  # noqa: S608 -- constant column list
        snap = await self.pool.fetch(
            f"SELECT DISTINCT ON (point, ts) {cols} FROM demand_weather_forecasts WHERE ts >= $1 AND issued_at <= $2 "  # noqa: S608
            "ORDER BY point, ts, issued_at DESC", start, now)
        frames = [pd.DataFrame([dict(r) for r in rows]) for rows in (past, snap) if rows]
        if not frames:
            return pd.DataFrame()
        both = pd.concat(frames, ignore_index=True)
        return both.drop_duplicates(subset=["point", "ts"], keep="first")
