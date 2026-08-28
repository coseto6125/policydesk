"""
The connection pool, and the handful of helpers everything queries through.

Follows enoract's `BaseDBPool`: one psqlpy `ConnectionPool`, retries on the exceptions
that mean "the connection went away" rather than "the query was wrong", and helpers
that hand back plain dicts so callers do not thread a driver type through the codebase.

Parameters are `$1`-style and always passed separately. There is no string
interpolation of a value anywhere in this project, including in a query nobody outside
would ever reach — a codebase where one query builds SQL by formatting teaches the next
one to.
"""

import os
from typing import TYPE_CHECKING, Any

import stamina
from psqlpy import ConnectionPool
from psqlpy.exceptions import BaseConnectionError, BaseConnectionPoolError

from policydesk.bootloader import logger

if TYPE_CHECKING:
    from collections.abc import Sequence

DEFAULT_DSN = os.environ.get("DATABASE_URL", "postgres://policydesk:policydesk@localhost:5434/policydesk")


def _is_transport_failure(exc: Exception) -> bool:
    """
    Say whether an exception means the connection failed rather than the query.

    Args:
        exc: What was raised.

    Returns:
        True when retrying could plausibly work. A malformed query retried three times
        is three identical errors and a slower failure.

    """
    return isinstance(exc, BaseConnectionError | BaseConnectionPoolError)


def _no_rows(exc: Exception) -> bool:
    """
    Say whether an exception means the query matched nothing.

    Args:
        exc: What psqlpy raised.

    Returns:
        True for the "unexpected number of rows" error, which psqlpy raises from
        fetch_val when a query returns none. It is not a transport failure, and it was
        being retried three times before surfacing — three identical round trips and a
        slower answer to "there is no such row".

    """
    return "unexpected number of rows" in str(exc)


class Database:
    """One pool, with the fetch helpers the rest of the code uses."""

    def __init__(self, dsn: str = DEFAULT_DSN, max_pool_size: int = 10) -> None:
        self._dsn = dsn
        self._pool = ConnectionPool(dsn=dsn, max_db_pool_size=max_pool_size)
        logger.info("db_pool_open", size=max_pool_size)

    async def close(self) -> None:
        """Close the pool."""
        self._pool.close()

    @stamina.retry(on=_is_transport_failure, attempts=3, timeout=20)
    async def fetch(self, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        """
        Run a query and return its rows.

        Args:
            sql: SQL with `$1`-style placeholders.
            params: Values for the placeholders.

        Returns:
            Rows as dicts.

        """
        async with self._pool.acquire() as conn:
            return (await conn.fetch(sql, params or [])).result()

    async def fetch_one(self, sql: str, params: Sequence[Any] | None = None) -> dict[str, Any] | None:
        """
        Run a query expected to match at most one row.

        Args:
            sql: SQL with `$1`-style placeholders.
            params: Values for the placeholders.

        Returns:
            The row, or None.

        """
        rows = await self.fetch(sql, params)
        return rows[0] if rows else None

    @stamina.retry(on=_is_transport_failure, attempts=3, timeout=20)
    async def fetch_val(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        """
        Run a query and return its first column of its first row.

        Args:
            sql: SQL with `$1`-style placeholders.
            params: Values for the placeholders.

        Returns:
            The scalar, or None when the query matched no row.

        psqlpy raises on an empty result here rather than returning None, so a caller
        looking up a row that may not exist would otherwise get an exception where it
        expected a null — and stamina would retry it twice first, because the message
        looks like a failure rather than an answer.

        """
        async with self._pool.acquire() as conn:
            try:
                return await conn.fetch_val(sql, params or [])
            except Exception as exc:
                if _no_rows(exc):
                    return None
                raise

    @stamina.retry(on=_is_transport_failure, attempts=3, timeout=20)
    async def execute(self, sql: str, params: Sequence[Any] | None = None) -> None:
        """
        Run a statement for its effect.

        Args:
            sql: SQL with `$1`-style placeholders.
            params: Values for the placeholders.

        """
        async with self._pool.acquire() as conn:
            await conn.execute(sql, params or [])

    @stamina.retry(on=_is_transport_failure, attempts=3, timeout=60)
    async def execute_many(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        """
        Run one statement over many parameter sets, in a single transaction.

        Args:
            sql: SQL with `$1`-style placeholders.
            rows: One parameter sequence per execution.

        All rows land or none do. A corpus import that half-succeeds leaves a database
        nobody can reason about, and re-running it would double what did land.

        """
        if not rows:
            return

        # Postgres binds at most 65535 parameters per statement. Past that the server
        # rejects the whole batch with "insufficient data left in message" — a protocol
        # complaint that names neither a row nor a column, and reads like corrupt data
        # rather than a size limit. 11,741 clauses at 7 parameters each is 82,187, so
        # the corpus import hits it and a hand-written test never does.
        per_row = max(1, len(rows[0]))
        chunk = max(1, 60_000 // per_row)

        async with self._pool.acquire() as conn, conn.transaction() as transaction:
            for start in range(0, len(rows), chunk):
                batch = [list(params) for params in rows[start : start + chunk]]
                await transaction.execute_many(sql, batch)
