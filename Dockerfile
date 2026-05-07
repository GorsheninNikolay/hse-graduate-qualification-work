# syntax=docker/dockerfile:1.7

# ---- Stage 1: builder ----
FROM python:3.14-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    POETRY_VERSION=2.1.1 \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    POETRY_VIRTUALENVS_CREATE=true \
    POETRY_NO_INTERACTION=1

WORKDIR /app

RUN pip install --no-cache-dir "poetry==${POETRY_VERSION}"

# Copy ONLY the dependency manifest first to maximize Docker layer caching.
COPY pyproject.toml poetry.lock poetry.toml ./

# Install runtime deps only (no dev deps). --no-root because pyproject has package-mode = false.
RUN poetry install --no-root --without dev

# ---- Stage 2: runtime ----
FROM python:3.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Copy the materialized venv from the builder stage.
COPY --from=builder /app/.venv /app/.venv

# Copy the application package.
COPY framework /app/framework

EXPOSE 4000

# Use the venv's python directly via PATH.
CMD ["python", "-m", "framework.cli"]
