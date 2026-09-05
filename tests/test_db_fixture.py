"""Database outages fail integration tests without exposing connection secrets."""

from traceback import format_exception
from unittest.mock import AsyncMock, Mock

import pytest

import conftest


async def test_connected_database_connection_failure_fails_and_closes(monkeypatch):
    pool = AsyncMock()
    pool.fetch_val.side_effect = ConnectionError("postgres://private-user:private-secret@unit.invalid/database")
    monkeypatch.setattr(conftest, "Database", Mock(return_value=pool))
    with pytest.raises((pytest.fail.Exception, pytest.skip.Exception)) as caught:
        async with conftest.connected_database():
            pytest.fail("a disconnected pool was yielded")
    assert isinstance(caught.value, pytest.fail.Exception)
    message = str(caught.value)
    assert "DATABASE_URL" in message
    assert "podman start policydesk-pg" in message
    assert "systemctl --user start policydesk-pg" in message
    assert "private-user" not in message
    assert "private-secret" not in message
    assert caught.value.__context__ is None
    assert "private-secret" not in "".join(format_exception(caught.value))
    pool.close.assert_awaited_once()


async def test_connected_database_constructor_failure_fails_without_dsn(monkeypatch):
    monkeypatch.setattr(conftest, "Database", Mock(side_effect=ValueError("private-secret")))
    with pytest.raises((pytest.fail.Exception, pytest.skip.Exception)) as caught:
        async with conftest.connected_database():
            pytest.fail("an invalid pool was yielded")
    assert isinstance(caught.value, pytest.fail.Exception)
    assert "private-secret" not in str(caught.value)
    assert caught.value.__context__ is None
    assert "private-secret" not in "".join(format_exception(caught.value))


@pytest.mark.parametrize("body_fails", [False, True])
async def test_connected_database_success_closes_after_context(monkeypatch, body_fails):
    pool = AsyncMock()
    pool.fetch_val.return_value = 1
    monkeypatch.setattr(conftest, "Database", Mock(return_value=pool))

    async def use_pool():
        async with conftest.connected_database() as actual:
            assert actual is pool
            pool.fetch_val.assert_awaited_once_with("SELECT 1")
            pool.close.assert_not_awaited()
            if body_fails:
                raise AssertionError("test body failed")

    if body_fails:
        with pytest.raises(AssertionError, match="test body failed"):
            await use_pool()
    else:
        await use_pool()
    pool.close.assert_awaited_once()
