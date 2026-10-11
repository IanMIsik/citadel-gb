"""`GET /api/demand-forecast`: the demand-error model's predictions for the FPN page, and
`GET /api/demand-forecast/tracking`: how the logged live predictions have actually done. Dev only -- app.py mounts this
router only when DEMAND_FORECASTER_ENABLED is true, so on prod these routes do not exist (404) and the page shows nothing.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request

from citadel.engine import demand_tracking
from citadel.engine.demand_live import DemandPredictor
from citadel.storage import db

router = APIRouter()


@router.get("/api/demand-forecast")
async def demand_forecast(request: Request):
    predictor = getattr(request.app.state, "demand_predictor", None)
    if predictor is None:
        predictor = request.app.state.demand_predictor = DemandPredictor(request.app.state.pool)
    await predictor.ensure_started()
    return predictor.state


@router.get("/api/demand-forecast/tracking")
async def demand_forecast_tracking(request: Request, days: int = Query(14, ge=1, le=365)):
    """Live predictions made in the last `days` days whose outcome is now known, scored per lead time."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    scores = demand_tracking.scores_frame(await db.demand_prediction_scores(request.app.state.pool, since))
    summary = demand_tracking.tracking_summary(scores)
    return {"days": days, "scored_live_predictions": int(summary["n"].sum()) if len(summary) else 0,
            "by_lead": json.loads(summary.to_json(orient="records"))}
