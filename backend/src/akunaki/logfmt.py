"""Structured log formatting shared by the api and worker entrypoints.

One JSON object per line, carrying every field a call site attached through
``extra``. The previous format string could only render the message, so a
line like ``"google_health fetch rejected"`` reached the log without the
``status`` and ``failure`` it was logged with — the one thing an operator
needed three weeks later. A message containing a quote also broke the JSON.
"""

from __future__ import annotations

import json
import logging
from typing import Any

# Attributes every LogRecord carries; anything else on the record was attached
# by the caller through ``extra`` (or a filter) and belongs in the line.
_STANDARD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | frozenset(
    {"message", "asctime", "taskName"}
)


class JsonFormatter(logging.Formatter):
    """Render a record as one JSON line including its ``extra`` fields."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialize the record; values that are not JSON go through ``str``."""
        payload: dict[str, Any] = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)
