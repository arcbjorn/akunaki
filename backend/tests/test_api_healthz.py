"""API core-only boot and /healthz."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from akunaki.api.app import create_app
from akunaki.config import Settings


def test_core_only_boot_without_model_config(settings: Settings) -> None:
    """API factory succeeds with only core settings (no MODEL_*)."""
    app = create_app(settings)
    assert app.title == "Akunaki API"
    assert app.state.settings.database_url.startswith("sqlite+libsql:")
    assert not hasattr(settings, "model_provider")


def test_healthz_ok_when_database_ready(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "status": "ok",
        "service": "akunaki-api",
        "database_ready": True,
        "models_required": False,
    }
    # Must not fabricate product health fields.
    assert "recovery" not in body
    assert "scores" not in body
    assert "connectors" not in body


def test_healthz_degraded_when_probe_fails(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(settings)

    def _fail() -> bool:
        return False

    monkeypatch.setattr(app.state, "probe_database_ready", _fail)
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["database_ready"] is False
    assert body["models_required"] is False
