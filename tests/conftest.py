"""Shared database lifecycle for integration tests."""

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import pytest

from policydesk.core.db import Database

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@asynccontextmanager
async def connected_database() -> AsyncIterator[Database]:
    """Check connectivity and always close the pool without exposing its DSN."""
    pool = None
    try:
        try:
            pool = Database()
            await pool.fetch_val("SELECT 1")
            connected = True
        except Exception:
            connected = False
        if not connected:
            pytest.fail(
                "policydesk-pg is not reachable. Check DATABASE_URL and start the existing service with "
                "`podman start policydesk-pg` or `systemctl --user start policydesk-pg`.",
                pytrace=False,
            )
        yield pool
    finally:
        if pool is not None:
            await pool.close()


@pytest.fixture(scope="module")
async def db() -> AsyncIterator[Database]:
    """Share one checked pool per test module."""
    async with connected_database() as pool:
        yield pool
