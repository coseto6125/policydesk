# syntax=docker/dockerfile:1

# Two stages, because one dependency has to be compiled.
#
# etoon publishes no wheel for Python 3.14, so uv builds it from source with maturin,
# which needs a Rust toolchain. A single-stage build on python:3.14-slim fails at that
# step; putting Rust in the runtime image instead would carry a compiler into
# production for a dependency that is compiled once.
#
# The builder produces .venv and the runtime copies it. Nothing else crosses.

FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Rust for etoon and logxide; build-essential for anything else that needs a linker.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential curl \
 && curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain stable \
 && rm -rf /var/lib/apt/lists/*
ENV PATH="/root/.cargo/bin:${PATH}"

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Dependencies resolve from the lock, so the image gets the versions the tests ran
# against rather than whatever resolves at build time.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev


FROM python:3.14-slim AS runtime

WORKDIR /app
ENV PATH="/app/.venv/bin:${PATH}" PYTHONUNBUFFERED=1

COPY --from=builder /app/.venv /app/.venv
COPY src ./src
COPY pyproject.toml README.md ./

# Runs as a non-root user: the desk holds members' national IDs and addresses, and a
# container that reads them does not also need to own its own filesystem.
RUN useradd --create-home --uid 10001 desk && chown -R desk:desk /app
USER desk

EXPOSE 8100
CMD ["python", "-m", "policydesk.web.server"]
