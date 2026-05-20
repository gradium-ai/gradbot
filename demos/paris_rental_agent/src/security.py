"""JWT utilities for browser sessions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt

from .config import get_settings

JWT_ALGORITHM = "HS256"


def create_access_token(user_id: str, *, expires_delta: Optional[timedelta] = None) -> str:
    settings = get_settings()
    if expires_delta is None:
        expires_delta = timedelta(seconds=settings.cookie_max_age_seconds)
    expire = datetime.now(timezone.utc) + expires_delta
    payload: dict[str, Any] = {"sub": user_id, "exp": expire}
    return jwt.encode(payload, settings.effective_secret_key, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> Optional[str]:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.effective_secret_key, algorithms=[JWT_ALGORITHM])
        sub = payload.get("sub")
        if isinstance(sub, str):
            return sub
        return None
    except jwt.PyJWTError:
        return None
