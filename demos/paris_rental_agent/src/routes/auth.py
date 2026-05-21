"""Auth routes for anonymous browser sessions."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Body, Cookie, Depends, Query, Response
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..dependencies import get_current_user, get_optional_user
from ..models import RenterProfile, SearchProfile, User
from ..schemas import GuestSessionOut, UserOut
from ..security import create_access_token, decode_access_token

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _set_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    secure = settings.app_env == "production"
    response.set_cookie(
        key=settings.cookie_name,
        value=token,
        max_age=settings.cookie_max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def _ensure_initial_records(db: Session, user: User) -> None:
    """Create the renter profile and a draft search profile for a new session."""
    if not user.renter_profile:
        db.add(RenterProfile(user_id=user.id))
    has_search_profile = (
        db.query(SearchProfile).filter(SearchProfile.user_id == user.id).first() is not None
    )
    if not has_search_profile:
        db.add(SearchProfile(user_id=user.id))


def _user_from_token(db: Session, token: Optional[str]) -> Optional[User]:
    if not token:
        return None
    user_id = decode_access_token(token)
    if not user_id:
        return None
    return db.query(User).filter(User.id == user_id).first()


_SESSION_LABEL_DOMAIN = "@session.paris-rental.local"


@router.post("/guest", response_model=GuestSessionOut)
def guest_session(
    response: Response,
    persist: bool = Query(default=True),
    db: Session = Depends(get_db),
    paris_rental_session: Optional[str] = Cookie(default=None),
) -> GuestSessionOut:
    """Create or refresh an anonymous browser session after cookie consent."""
    user = _user_from_token(db, paris_rental_session)
    if user:
        _ensure_initial_records(db, user)
        db.commit()
        db.refresh(user)
    else:
        user = User(
            session_label=f"session-{uuid.uuid4().hex}{_SESSION_LABEL_DOMAIN}",
            session_secret=uuid.uuid4().hex,
        )
        db.add(user)
        db.flush()
        _ensure_initial_records(db, user)
        db.commit()
        db.refresh(user)

    token = create_access_token(
        user.id,
        expires_delta=None if persist else timedelta(hours=8),
    )
    if persist:
        _set_cookie(response, token)
    return GuestSessionOut(
        **UserOut.model_validate(user).model_dump(),
        token=None if persist else token,
        persisted=persist,
    )


@router.post("/logout")
def logout(
    response: Response,
    payload: Optional[dict] = Body(default=None),
    current_user: Optional[User] = Depends(get_optional_user),
    db: Session = Depends(get_db),
) -> dict:
    settings = get_settings()
    response.delete_cookie(settings.cookie_name, path="/")
    forget = bool((payload or {}).get("forget"))
    if forget and current_user:
        db.delete(current_user)
        db.commit()
    return {"ok": True}


@router.get("/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.model_validate(current_user)
