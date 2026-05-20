"""FastAPI dependencies: current user resolver."""

from __future__ import annotations

from typing import Optional

from fastapi import Cookie, Depends, Header, HTTPException, Query, status
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .models import User
from .security import decode_access_token


def _resolve_user(db: Session, token: Optional[str]) -> Optional[User]:
    if not token:
        return None
    user_id = decode_access_token(token)
    if not user_id:
        return None
    return db.query(User).filter(User.id == user_id).first()


def _bearer_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def get_current_user(
    db: Session = Depends(get_db),
    paris_rental_session: Optional[str] = Cookie(default=None),
    authorization: Optional[str] = Header(default=None),
) -> User:
    user = _resolve_user(db, paris_rental_session) or _resolve_user(
        db, _bearer_token(authorization)
    )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return user


def get_optional_user(
    db: Session = Depends(get_db),
    paris_rental_session: Optional[str] = Cookie(default=None),
    authorization: Optional[str] = Header(default=None),
) -> Optional[User]:
    return _resolve_user(db, paris_rental_session) or _resolve_user(
        db, _bearer_token(authorization)
    )


def get_user_from_token_or_cookie(
    db: Session = Depends(get_db),
    paris_rental_session: Optional[str] = Cookie(default=None),
    token: Optional[str] = Query(default=None),
    authorization: Optional[str] = Header(default=None),
) -> User:
    """For WebSocket auth where cookies may not be available — accept ?token=... fallback."""
    user = (
        _resolve_user(db, paris_rental_session)
        or _resolve_user(db, token)
        or _resolve_user(db, _bearer_token(authorization))
    )
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return user
