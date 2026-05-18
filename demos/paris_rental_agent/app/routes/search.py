"""Search routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..db import get_db
from ..dependencies import get_current_user
from ..models import SearchRun, User
from ..schemas import SearchRunIn
from ..services import assistant_tools

router = APIRouter(prefix="/api", tags=["search"])


@router.post("/search-runs")
async def create_search_run(
    payload: SearchRunIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    result = await assistant_tools.run_apartment_search(
        db,
        current_user.id,
        max_results=payload.max_results,
        refresh=payload.refresh,
        allow_unconfirmed_profile=payload.allow_unconfirmed_profile,
    )
    if not result.get("ok"):
        err = result.get("error")
        if err == "search_profile_not_confirmed":
            raise HTTPException(status_code=409, detail=result)
        if err == "search_profile_incomplete":
            raise HTTPException(status_code=400, detail=result)
        if err == "tavily_not_configured":
            raise HTTPException(status_code=503, detail=result)
        if err == "tavily_search_failed":
            raise HTTPException(status_code=502, detail=result)
        raise HTTPException(status_code=500, detail=result)
    return result


@router.get("/search-runs/latest")
def get_latest_search_run(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    last_run = (
        db.query(SearchRun)
        .filter(SearchRun.user_id == current_user.id)
        .order_by(SearchRun.started_at.desc())
        .first()
    )
    if not last_run:
        return {"ok": True, "search_run": None}
    return {
        "ok": True,
        "search_run": {
            "id": last_run.id,
            "status": last_run.status,
            "result_count": last_run.result_count,
            "error_message": last_run.error_message,
            "started_at": last_run.started_at.isoformat() if last_run.started_at else None,
            "completed_at": last_run.completed_at.isoformat() if last_run.completed_at else None,
        },
    }


@router.get("/matches")
def list_matches(
    min_score: int | None = None,
    include_rejected: bool = False,
    limit: int = Query(default=20, ge=1, le=50),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    result = assistant_tools.list_top_matches(db, current_user.id, limit=limit)
    matches = result.get("matches") or []
    if min_score is not None:
        matches = [m for m in matches if m["overall_score"] >= min_score]
    return {
        "ok": True,
        "matches": matches,
        "search_run_id": result.get("search_run_id"),
        "stale": bool(result.get("stale")),
        "message": result.get("message"),
    }
