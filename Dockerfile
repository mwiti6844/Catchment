# syntax=docker/dockerfile:1

# One image serves both roles — the API and the RQ worker — differing only in
# the command. Two Dockerfiles at this scale would just be two things to keep
# in sync.

# --- build stage ------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

# uv is copied in rather than used as the base image, so the builder and the
# runtime share an identical Debian/Python base. The virtualenv is copied
# between stages and its interpreter symlinks must resolve on the other side.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies in their own layer, resolved from pyproject alone. This layer is
# invalidated only when pyproject.toml changes, so editing application code
# does not reinstall SQLAlchemy and FastAPI every build.
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /app/.venv && \
    uv pip install --python /app/.venv/bin/python -r pyproject.toml

# Then the source, and the package itself with dependency resolution skipped —
# they were installed above.
COPY catchment ./catchment
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv/bin/python --no-deps .

# --- runtime stage ----------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

RUN groupadd --system --gid 10001 catchment && \
    useradd --system --uid 10001 --gid catchment --no-create-home catchment

WORKDIR /app

COPY --from=builder --chown=catchment:catchment /app/.venv /app/.venv
COPY --chown=catchment:catchment catchment ./catchment
COPY --chown=catchment:catchment alembic.ini ./

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# No secrets are baked in. Every credential arrives at run time through the
# environment and is read via catchment/config.py — see CLAUDE.md.
USER catchment
EXPOSE 8000

# Uses the stdlib rather than adding curl to the image for one health probe.
# The worker has no HTTP surface and disables this in compose.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"]

CMD ["uvicorn", "catchment.api:app", "--host", "0.0.0.0", "--port", "8000"]
