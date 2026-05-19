"""Profile intake routes (voice + text onboarding)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..dependencies import get_current_user
from ..models import User
from ..schemas import TextUpdateIn, TranscriptIn
from ..services import assistant_tools

router = APIRouter(prefix="/api/intake", tags=["intake"])


@router.post("/start")
def start(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return assistant_tools.start_profile_intake(db, current_user.id)


@router.get("/current")
def current(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return assistant_tools.get_profile_draft(db, current_user.id)


@router.post("/transcript")
def transcript(
    payload: TranscriptIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not payload.transcript or not payload.transcript.strip():
        raise HTTPException(status_code=400, detail="transcript is empty")
    return assistant_tools.extract_requirements_from_transcript(
        db, current_user.id, payload.transcript, source="voice"
    )


@router.post("/text-update")
def text_update(
    payload: TextUpdateIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not isinstance(payload.patch, dict):
        raise HTTPException(status_code=400, detail="patch must be an object")
    return assistant_tools.update_profile_draft(
        db, current_user.id, payload.patch, source="text"
    )


@router.post("/confirm")
def confirm(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return assistant_tools.confirm_profile(db, current_user.id)
