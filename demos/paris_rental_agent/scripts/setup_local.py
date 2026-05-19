"""Create local database tables and optionally seed the demo account.

Run from the repo root:
    python demos/paris_rental_agent/scripts/setup_local.py

Or from this demo folder:
    python scripts/setup_local.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from src.bootstrap import DEMO_EMAIL, DEMO_PASSWORD, seed_demo_user  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.db import init_db  # noqa: E402


def main() -> None:
    settings = get_settings()
    init_db()
    print(f"Database ready: {settings.database_url}")
    if not settings.enable_demo_account:
        print("Demo account disabled. Set ENABLE_DEMO_ACCOUNT=true to seed it.")
        return

    created = seed_demo_user()
    status = "created" if created else "already exists"
    print(f"Demo account {status}: {DEMO_EMAIL} / {DEMO_PASSWORD}")


if __name__ == "__main__":
    main()
