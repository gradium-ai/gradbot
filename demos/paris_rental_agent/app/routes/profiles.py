"""Renter and search profile routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..dependencies import get_current_user
from ..models import RenterProfile, SearchProfile, User
from ..schemas import (
    RenterProfileIn,
    RenterProfileOut,
    SearchProfileBase,
    SearchProfileOut,
)
from ..services import assistant_tools

router = APIRouter(prefix="/api", tags=["profiles"])


@router.get("/renter-profile", response_model=RenterProfileOut)
def get_renter_profile(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> RenterProfileOut:
    rp = (
        db.query(RenterProfile).filter(RenterProfile.user_id == current_user.id).first()
    )
    if not rp:
        rp = RenterProfile(user_id=current_user.id)
        db.add(rp)
        db.commit()
        db.refresh(rp)
    return RenterProfileOut.model_validate(rp)


@router.patch("/renter-profile", response_model=RenterProfileOut)
def patch_renter_profile(
    patch: RenterProfileIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RenterProfileOut:
    payload = patch.model_dump(exclude_unset=True)
    assistant_tools.update_renter_profile(db, current_user.id, payload)
    rp = (
        db.query(RenterProfile).filter(RenterProfile.user_id == current_user.id).first()
    )
    return RenterProfileOut.model_validate(rp)


@router.get("/search-profile", response_model=SearchProfileOut)
def get_search_profile(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> SearchProfileOut:
    sp = assistant_tools.get_active_search_profile(db, current_user.id)
    if not sp:
        sp = SearchProfile(user_id=current_user.id)
        db.add(sp)
        db.commit()
        db.refresh(sp)
    elif assistant_tools.repair_search_profile(sp):
        db.commit()
        db.refresh(sp)
    return SearchProfileOut.model_validate(sp)


@router.patch("/search-profile", response_model=SearchProfileOut)
def patch_search_profile(
    patch: SearchProfileBase,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> SearchProfileOut:
    payload = patch.model_dump(exclude_unset=True)
    if not payload:
        raise HTTPException(status_code=400, detail="empty patch")
    assistant_tools.update_search_profile(db, current_user.id, payload)
    sp = assistant_tools.get_active_search_profile(db, current_user.id)
    if sp is not None and assistant_tools.repair_search_profile(sp):
        db.commit()
        db.refresh(sp)
    return SearchProfileOut.model_validate(sp)
