"""Listing, save/reject, and viewing-request routes."""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..dependencies import get_current_user
from ..models import Listing, User
from ..schemas import DraftRequestIn, SavedListingPatch
from ..services import assistant_tools

router = APIRouter(prefix="/api", tags=["listings"])


@router.get("/listings/{listing_id}")
def get_listing(
    listing_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    listing = db.query(Listing).filter(Listing.id == listing_id).first()
    if not listing:
        raise HTTPException(status_code=404, detail="listing_not_found")
    return assistant_tools._serialize_listing(listing)


@router.post("/listings/{listing_id}/save")
def save_listing(
    listing_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    res = assistant_tools.save_listing(db, current_user.id, listing_id)
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res)
    return res


@router.post("/listings/{listing_id}/reject")
def reject_listing(
    listing_id: str,
    body: dict = Body(default_factory=dict),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    reason = body.get("reason") if isinstance(body, dict) else None
    res = assistant_tools.reject_listing(db, current_user.id, listing_id, reason)
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res)
    return res


@router.post("/listings/{listing_id}/explain")
def explain_listing(
    listing_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    res = assistant_tools.explain_listing(db, current_user.id, listing_id)
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res)
    return res


@router.post("/listings/{listing_id}/draft-viewing-request")
def draft_viewing_request(
    listing_id: str,
    payload: DraftRequestIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    res = assistant_tools.draft_viewing_request(
        db, current_user.id, listing_id, payload.language
    )
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res)
    return res


@router.get("/saved-listings")
def list_saved(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return assistant_tools.list_saved_listings(db, current_user.id)


@router.patch("/saved-listings/{saved_listing_id}")
def update_saved(
    saved_listing_id: str,
    patch: SavedListingPatch,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    payload = patch.model_dump(exclude_unset=True)
    res = assistant_tools.update_saved_listing(
        db, current_user.id, saved_listing_id, payload
    )
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res)
    return res


@router.get("/viewing-drafts")
def list_drafts(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return assistant_tools.list_viewing_drafts(db, current_user.id)
