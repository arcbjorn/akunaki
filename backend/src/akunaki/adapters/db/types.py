"""Portable column types for the local libSQL driver.

``libsql_experimental`` implements BLOB storage correctly but does not expose
the optional DBAPI ``Binary`` constructor, so SQLAlchemy's stock
:class:`~sqlalchemy.LargeBinary` raises ``AttributeError`` in its bind
processor before a statement is ever executed. The driver accepts and returns
plain ``bytes`` natively, so :class:`Blob` binds them through unchanged.

Documented local driver limitation, not a schema relaxation: the emitted DDL
is still ``BLOB``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import LargeBinary
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator


class Blob(TypeDecorator[bytes]):
    """BLOB column that binds ``bytes`` directly (no DBAPI ``Binary`` needed)."""

    impl = LargeBinary
    cache_ok = True

    def bind_processor(self, dialect: Dialect) -> Any:
        """Bypass ``LargeBinary``'s ``dialect.dbapi.Binary`` lookup.

        Returning ``None`` tells SQLAlchemy to pass the Python value straight
        through to the driver, which is exactly what this driver wants.
        """
        return None

    def result_processor(self, dialect: Dialect, coltype: object) -> Any:
        """Return stored values unchanged; the driver already yields ``bytes``."""
        return None
