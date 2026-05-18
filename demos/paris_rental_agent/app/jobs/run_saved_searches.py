"""Run searches for users with daily AlertPreference."""

from __future__ import annotations

import asyncio
import logging
from typing import Iterable

from sqlalchemy.orm import Session

from ..db import SessionLocal, init_db
from ..models import AlertPreference
from ..services.search_pipeline import run_search_for_user

logger = logging.getLogger(__name__)


async def _run_for_user(db: Session, user_id: str) -> dict:
    return await run_search_for_user(db, user_id, max_results=20)


async def run() -> dict:
    """Run scheduled searches for every user with an enabled, daily alert."""
    init_db()
    db = SessionLocal()
    summary = {"users_processed": 0, "results": []}
    try:
        prefs = (
            db.query(AlertPreference)
            .filter(
                AlertPreference.enabled.is_(True),
                AlertPreference.frequency == "daily",
            )
            .all()
        )
        seen_users: set[str] = set()
        for pref in prefs:
            if pref.user_id in seen_users:
                continue
            seen_users.add(pref.user_id)
            try:
                res = await _run_for_user(db, pref.user_id)
                summary["users_processed"] += 1
                summary["results"].append({"user_id": pref.user_id, "result": res})
                logger.info("Scheduled search for user=%s ok=%s", pref.user_id, res.get("ok"))
            except Exception:
                logger.exception("Scheduled search failed for user=%s", pref.user_id)
    finally:
        db.close()
    return summary


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    summary = asyncio.run(run())
    logger.info("Scheduled run complete: %s", summary)


if __name__ == "__main__":
    main()
