"""Alembic migration scripts, shipped inside the package.

They live here rather than beside ``alembic.ini`` so an installed wheel
carries them: the readiness endpoint reports whether the database is at the
code's migration head, and it can only answer that if the scripts it compares
against are actually present. Locating this package by import also removes the
path arithmetic that broke as soon as the layout was not a source checkout.
"""

from __future__ import annotations

from pathlib import Path


def script_location() -> Path:
    """Absolute path to this migrations directory (the alembic script location)."""
    return Path(__file__).resolve().parent
