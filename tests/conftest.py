"""Shared database lifecycle for integration tests."""

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from policydesk.core.db import Database

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def mock_database(**attrs: object) -> AsyncMock:
    """
    Return an async database mock with a synchronous transaction context factory.

    Args:
        **attrs: Return values to set on the mock, e.g. `fetch_val="inquiry"`.

    Returns:
        The mock. Its `transaction()` yields a session that shares the same
        `fetch`, `fetch_one`, `fetch_val` and `execute` mocks as the database
        itself, so a test that stubs a query gets the same answer either way.

    Transaction entry is synchronous, while queries and context entry are async.
    This fake checks that protocol, not commit/rollback or lock correctness;
    those require the live database tests. Tool mocks must separately preserve
    their actual public/identity declarations to pass the runtime access gate.
    """
    db = AsyncMock()
    for name, value in attrs.items():
        getattr(db, name).return_value = value
    session = AsyncMock()
    for name in ("fetch", "fetch_one", "fetch_val", "execute", "execute_many"):
        setattr(session, name, getattr(db, name))
    # A plain MagicMock, because every attribute of an AsyncMock is itself async and
    # `transaction()` would return a coroutine again. `Database.transaction` is a
    # synchronous call returning a context manager, and this matches that shape.
    db.transaction = MagicMock()
    db.transaction.return_value.__aenter__ = AsyncMock(return_value=session)
    db.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
    return db


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
