# syntax=docker/dockerfile:1
FROM python:3.14-slim AS base

# uv resolves and installs from the lock, so the image gets the same versions the
# tests ran against rather than whatever resolves at build time.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

EXPOSE 8100
CMD ["uv", "run", "--no-dev", "policydesk"]
