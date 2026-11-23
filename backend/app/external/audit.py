"""Local rotating JSON-lines audit. Payloads, file paths and secrets are excluded."""

import json
import logging
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler

import structlog

from app.core.config import user_data_dir

logger = logging.getLogger("synex.external.audit")
logger.propagate = False


def record(operation, *, plugin=None, version=None, server=None, outcome, call_id=None):
    try:
        if not logger.handlers:
            folder = user_data_dir() / "logs"
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            logger.addHandler(
                RotatingFileHandler(
                    folder / "external-plugins.jsonl",
                    maxBytes=1024 * 1024,
                    backupCount=3,
                    encoding="utf-8",
                )
            )
            logger.setLevel(logging.INFO)
        logger.info(
            json.dumps(
                dict(
                    time=datetime.now(UTC).isoformat(),
                    operation=operation,
                    plugin=plugin,
                    version=version,
                    server=server,
                    outcome=outcome,
                    call_id=call_id,
                ),
                ensure_ascii=True,
            )
        )
    except OSError:
        structlog.get_logger().warning("External plugin audit unavailable")
