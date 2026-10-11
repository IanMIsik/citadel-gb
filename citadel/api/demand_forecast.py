"""`GET /api/demand-forecast`: the demand-error model's predictions for the FPN page. Dev only -- app.py mounts this
router only when DEMAND_FORECASTER_ENABLED is true, so on prod the route does not exist (404) and the page shows nothing.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from citadel.engine.demand_live import DemandPredictor

router = APIRouter()


@router.get("/api/demand-forecast")
async def demand_forecast(request: Request):
    predictor = getattr(request.app.state, "demand_predictor", None)
    if predictor is None:
        predictor = request.app.state.demand_predictor = DemandPredictor(request.app.state.pool)
    await predictor.ensure_started()
    return predictor.state
