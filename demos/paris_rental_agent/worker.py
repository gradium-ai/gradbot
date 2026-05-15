"""Background worker entry-point.

For the MVP this is a polling stub: it wakes up periodically and runs any
scheduled searches that are due. In production this would be replaced by a
queue-driven worker (RQ, Arq, Celery, etc.).

Run manually with:
    python -m demos.paris_rental_agent.worker
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from contextlib import suppress
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from app.db import init_db  # noqa: E402
from app.jobs import run_saved_searches  # noqa: E402

logger = logging.getLogger(__name__)


_shutdown = asyncio.Event()


def _install_signal_handlers(loop: asyncio.AbstractEventLoop) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, _shutdown.set)


async def _main() -> None:
    init_db()
    interval_s = int(os.environ.get("WORKER_INTERVAL_S", "3600"))
    logger.info("Paris rental worker started (interval=%ss). Waiting for tasks…", interval_s)

    while not _shutdown.is_set():
        try:
            summary = await run_saved_searches.run()
            logger.info("Worker tick: %s", summary)
        except Exception:
            logger.exception("Worker tick failed")

        try:
            await asyncio.wait_for(_shutdown.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            continue

    logger.info("Worker shutting down…")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    loop = asyncio.new_event_loop()
    try:
        _install_signal_handlers(loop)
        loop.run_until_complete(_main())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
