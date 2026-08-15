# Core-only runtime image: API, worker, and migrations from one build.
#
# The same image serves three commands, so the three processes can never drift
# onto different code or dependency sets:
#   API:        python -m akunaki.api      (set AKUNAKI_API_HOST=0.0.0.0)
#   worker:     python -m akunaki.worker
#   migrations: alembic upgrade head       (init container)
#
# Python is pinned to exactly 3.13.14 (3.14 + sqlalchemy-libsql segfaulted;
# see backend/README.md), dependencies come frozen from uv.lock with no dev
# group, and no model SDK is present — the boot-boundary CI job is the spec
# this image implements.

FROM python:3.13.14-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /bin/uv

ENV UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Dependency layer first so a source-only change does not re-resolve anything.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# The project itself, installed editable against /app/src — the same layout
# CI and local dev run, so alembic.ini's script_location works unchanged.
# scripts/ rides along for operator acts run inside the container, chiefly
# service-token minting (scripts/service_token.py).
COPY alembic.ini ./
COPY src ./src
COPY scripts ./scripts
RUN uv sync --frozen --no-dev


FROM python:3.13.14-slim

# Non-root with a fixed uid so a volume's ownership survives image rebuilds.
RUN groupadd --gid 10001 akunaki && \
    useradd --uid 10001 --gid akunaki --home-dir /app --no-create-home akunaki

COPY --from=build --chown=akunaki:akunaki /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app
USER akunaki

# Development-oriented default; the deployment overrides the command per role.
CMD ["python", "-m", "akunaki.api"]
