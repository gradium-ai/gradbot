"""Auth routes: signup, login, logout, me."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..dependencies import get_current_user
from ..models import RenterProfile, SearchProfile, User
from ..schemas import LoginRequest, SignupRequest, UserOut
from ..security import create_access_token, hash_password, verify_password

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
    """Create the renter profile and a draft search profile on signup."""
    if not user.renter_profile:
        db.add(RenterProfile(user_id=user.id))
    has_search_profile = (
        db.query(SearchProfile).filter(SearchProfile.user_id == user.id).first() is not None
    )
    if not has_search_profile:
        db.add(SearchProfile(user_id=user.id))


@router.post("/signup", response_model=UserOut)
def signup(payload: SignupRequest, response: Response, db: Session = Depends(get_db)) -> UserOut:
    existing = db.query(User).filter(User.email == payload.email.lower()).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )
    user = User(
        email=payload.email.lower(),
        password_hash=hash_password(payload.password),
        full_name=payload.full_name,
    )
    db.add(user)
    db.flush()
    _ensure_initial_records(db, user)
    db.commit()
    db.refresh(user)
    token = create_access_token(user.id)
    _set_cookie(response, token)
    return UserOut.model_validate(user)


@router.post("/login", response_model=UserOut)
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)) -> UserOut:
    user = db.query(User).filter(User.email == payload.email.lower()).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )
    user.last_login_at = datetime.now(timezone.utc)
    _ensure_initial_records(db, user)
    db.commit()
    db.refresh(user)
    token = create_access_token(user.id)
    _set_cookie(response, token)
    return UserOut.model_validate(user)


@router.post("/logout")
def logout(response: Response) -> dict:
    settings = get_settings()
    response.delete_cookie(settings.cookie_name, path="/")
    return {"ok": True}


@router.get("/me", response_model=UserOut)
def me(current_user: User = Depends(get_current_user)) -> UserOut:
    return UserOut.model_validate(current_user)


@router.post("/voice-token")
def voice_token(current_user: User = Depends(get_current_user)) -> dict:
    """Return a short-lived token for WebSocket connections that can't read cookies."""
    token = create_access_token(current_user.id, expires_delta=timedelta(minutes=5))
    return {"token": token}
