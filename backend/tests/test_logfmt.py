"""The JSON formatter must carry ``extra`` fields and stay valid JSON."""

from __future__ import annotations

import json
import logging

from akunaki.logfmt import JsonFormatter


def _record(msg: str, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("akunaki.test", logging.WARNING, __file__, 1, msg, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_extra_fields_are_rendered() -> None:
    line = JsonFormatter().format(_record("fetch rejected", stream="steps", status=400))
    parsed = json.loads(line)
    assert parsed["msg"] == "fetch rejected"
    assert parsed["level"] == "WARNING"
    assert parsed["logger"] == "akunaki.test"
    assert parsed["stream"] == "steps"
    assert parsed["status"] == 400
    assert "ts" in parsed


def test_standard_attributes_are_not_duplicated() -> None:
    parsed = json.loads(JsonFormatter().format(_record("hello")))
    assert set(parsed) == {"ts", "level", "logger", "msg"}


def test_quotes_and_non_json_values_stay_valid() -> None:
    line = JsonFormatter().format(_record('he said "no"', failure=object()))
    parsed = json.loads(line)
    assert parsed["msg"] == 'he said "no"'
    assert isinstance(parsed["failure"], str)
