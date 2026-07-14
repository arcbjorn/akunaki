"""Entrypoint: python -m akunaki.api"""

from __future__ import annotations

import uvicorn

from akunaki.api.app import create_app
from akunaki.config import get_settings


def main() -> None:
    """Run the API with uvicorn (development-oriented defaults)."""
    settings = get_settings()
    app = create_app(settings)
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
