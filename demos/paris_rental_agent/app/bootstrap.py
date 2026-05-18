"""Local bootstrap helpers for the Paris rental demo."""

from __future__ import annotations

import logging

from .db import SessionLocal
from .models import RenterProfile, SearchProfile, User
from .security import hash_password

logger = logging.getLogger(__name__)

DEMO_EMAIL = "demo@example.com"
DEMO_PASSWORD = "demopass123"


def seed_demo_user() -> bool:
    """Idempotently create the demo account used by local setup and startup."""
    db = SessionLocal()
    try:
        existing = db.query(User).filter(User.email == DEMO_EMAIL).first()
        if existing:
            return False
        user = User(
            email=DEMO_EMAIL,
            password_hash=hash_password(DEMO_PASSWORD),
            full_name="Demo User",
        )
        db.add(user)
        db.flush()
        db.add(RenterProfile(user_id=user.id, display_name="Demo User"))
        db.add(SearchProfile(user_id=user.id))
        db.commit()
        logger.info("Seeded demo user %s", DEMO_EMAIL)
        return True
    finally:
        db.close()
