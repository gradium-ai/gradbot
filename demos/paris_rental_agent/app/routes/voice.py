"""Voice WebSocket route — authenticates and hands off to gradbot_session."""

from __future__ import annotations

import logging
from typing import Optional

import gradbot
from fastapi import APIRouter, Cookie, Query, WebSocket, status

from ..config import get_settings
from ..db import SessionLocal
from ..models import User
from ..security import decode_access_token
from ..voice.gradbot_session import handle_voice_session

logger = logging.getLogger(__name__)

router = APIRouter()

# Module-level config so we don't reload yaml on every websocket
_VOICE_CONFIG: Optional[gradbot.config.Config] = None


def _get_voice_config() -> gradbot.config.Config:
    global _VOICE_CONFIG
    if _VOICE_CONFIG is None:
        from pathlib import Path

        # paris_rental_agent has its own config.yaml plus inherits from demos/config.yaml
        _VOICE_CONFIG = gradbot.config.load(Path(__file__).resolve().parents[2])
    return _VOICE_CONFIG


def _resolve_user(token: Optional[str]) -> Optional[User]:
    if not token:
        return None
    user_id = decode_access_token(token)
    if not user_id:
        return None
    db = SessionLocal()
    try:
        return db.query(User).filter(User.id == user_id).first()
    finally:
        db.close()


@router.websocket("/ws/voice")
async def ws_voice(
    websocket: WebSocket,
    paris_rental_session: Optional[str] = Cookie(default=None),
    token: Optional[str] = Query(default=None),
):
    user = _resolve_user(paris_rental_session) or _resolve_user(token)
    if not user:
        # Per the WS spec, we accept first then close so the client gets a code
        await websocket.accept()
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="not authenticated")
        return

    cfg = _get_voice_config()
    await handle_voice_session(websocket, user, config=cfg)
