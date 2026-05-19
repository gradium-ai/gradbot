"""Cron entry-point for scheduled searches.

Run manually with:
    python -m demos.paris_rental_agent.scheduled_search
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from src.jobs import run_saved_searches  # noqa: E402

logger = logging.getLogger(__name__)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    run_saved_searches.main()


if __name__ == "__main__":
    main()
