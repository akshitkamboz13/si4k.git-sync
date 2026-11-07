"""
Structured logging system for si4k.git-sync.
Provides standard console output and machine-parseable JSON logs.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "operation_id") and record.operation_id:
            log_obj["operation_id"] = record.operation_id
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_obj)


def get_logger(name: str = "si4k.git-sync") -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        handler = logging.StreamHandler(sys.stdout)
        use_json = os.environ.get("SI4K_LOG_FORMAT", "").lower() == "json"
        if use_json:
            handler.setFormatter(JSONFormatter())
        else:
            fmt = logging.Formatter(
                fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            handler.setFormatter(fmt)
        logger.addHandler(handler)
        logger.propagate = False
    return logger
