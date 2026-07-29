"""RQ worker entry point: ``catchment-worker`` (or ``python -m catchment.worker``).

Configures redacting logging *before* consuming anything, so no job can log a
message body during startup.
"""

from __future__ import annotations

import sys

from redis import Redis
from rq import Worker

from catchment.config import get_settings
from catchment.jobs.queue import QUEUE_NAME
from catchment.logging_config import configure_logging, get_logger, log_context
from catchment.storage.db import dispose_engine

logger = get_logger(__name__)


def main() -> int:
    """Run a worker against the pipeline queue until interrupted."""
    settings = get_settings()
    configure_logging(settings)

    connection = Redis.from_url(str(settings.redis_url))
    logger.info(
        "worker starting",
        extra=log_context(queue=QUEUE_NAME, environment=settings.env),
    )

    worker = Worker([QUEUE_NAME], connection=connection)
    try:
        worker.work(with_scheduler=False)
    except KeyboardInterrupt:  # pragma: no cover - operator action
        logger.info("worker stopped by operator")
    finally:
        # Same reasoning as the API lifespan: a container restart must not
        # strand the connection pool this process opened.
        dispose_engine()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
