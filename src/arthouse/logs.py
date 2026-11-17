"""One logging policy for the web app and every CLI entry point.

`ARTHOUSE_DEBUG=1` raises the root logger to DEBUG; the third-party loggers
that would otherwise drown the useful lines are pinned in one place here.
"""

import logging

from arthouse.config import get_settings

# httpx logs one line per request at INFO and the whole wire at DEBUG; our
# client already reports the path, params, status and retry itself.
NOISY_LOGGERS = ("httpx", "httpcore")


def configure_logging() -> None:
    """Set the root level and quiet the transport loggers; safe to call twice."""
    settings = get_settings()
    level = logging.DEBUG if settings.debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # `basicConfig` does nothing when the root already has handlers (pytest,
    # uvicorn), so set the level explicitly to keep the policy in force.
    logging.getLogger().setLevel(level)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
