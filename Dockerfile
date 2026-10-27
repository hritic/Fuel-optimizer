# --- Production-Ready Base Stage ---
# Standard python-slim comes pre-installed with debian-slim, bash, and ca-certificates
FROM python:3.12-slim AS base
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# --- Builder Stage ---
FROM base AS builder

# Securely pull the official uv binary directly from its verified engine image layer
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Copy the dependency matrix blueprint
COPY pyproject.toml /app/

RUN uv sync --no-install-project --no-dev

# --- Final Deployment Target Container ---
FROM base
# Copy the virtual environment compiled safely by uv in the isolated layer
COPY --from=builder /app/.venv /app/.venv

# Copy your source code configuration files
COPY . /app/
RUN chmod +x /app/setup.sh

EXPOSE 8000
ENTRYPOINT ["/app/setup.sh"]
