"""Database outages fail integration tests, and retry logs protect customer data."""

from contextlib import contextmanager
from importlib import import_module, reload
from traceback import format_exception
from unittest.mock import AsyncMock, Mock

import pytest
import stamina
from psqlpy.exceptions import ConnectionExecuteError
from stamina.instrumentation import RetryDetails, get_on_retry_hooks, set_on_retry_hooks
from structlog.testing import capture_logs

import conftest
from policydesk.core import db as database_module


@pytest.mark.parametrize("body_fails", [False, True])
async def test_mock_database_transaction_shares_queries_and_propagates_errors(body_fails):
    db = conftest.mock_database(fetch_val="issued", fetch=[{"document_id": 17}])

    async def use_session():
        async with db.transaction() as session:
            assert await session.fetch_val("stage") == "issued"
            assert await session.fetch("documents") == [{"document_id": 17}]
            await session.execute_many("roles", [[17, "mock"]])
            if body_fails:
                raise RuntimeError("transaction body failed")

    if body_fails:
        with pytest.raises(RuntimeError, match="transaction body failed"):
            await use_session()
    else:
        await use_session()
    db.transaction.assert_called_once_with()
    db.transaction.return_value.__aenter__.assert_awaited_once()
    db.transaction.return_value.__aexit__.assert_awaited_once()
    db.fetch_val.assert_awaited_once_with("stage")
    db.fetch.assert_awaited_once_with("documents")
    db.execute_many.assert_awaited_once_with("roles", [[17, "mock"]])


async def test_connected_database_connection_failure_fails_and_closes(monkeypatch):
    # The fail branch is the subject, so this test says which branch it is in rather
    # than reading whatever the runner set. CI declares no database and gets a skip.
    monkeypatch.delenv("POLICYDESK_TEST_NO_DB", raising=False)
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
    monkeypatch.delenv("POLICYDESK_TEST_NO_DB", raising=False)
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


@pytest.fixture
def retry_database(monkeypatch):
    pool = Mock()
    connection = AsyncMock()
    acquire = AsyncMock()
    acquire.__aenter__.return_value = connection
    pool.acquire.return_value = acquire
    transaction = AsyncMock()
    connection.transaction = Mock(return_value=transaction)
    transaction.__aenter__.return_value = transaction
    monkeypatch.setattr(database_module, "ConnectionPool", Mock(return_value=pool))
    db = database_module.Database(dsn="postgres://DSN_SECRET@invalid/database")
    return db, connection, transaction


@pytest.mark.parametrize("operation", ["fetch", "fetch_val", "execute", "execute_many"])
@pytest.mark.parametrize("outcome", ["recovered", "exhausted", "query_error"])
@pytest.mark.parametrize("keyword", [False, True])
async def test_database_retry_logs_omit_values_and_preserve_attempts(retry_database, operation, outcome, keyword):
    db, connection, transaction = retry_database
    target = getattr(transaction if operation == "execute_many" else connection, operation)
    error_type = RuntimeError if outcome == "query_error" else ConnectionExecuteError
    error = error_type("EXCEPTION_SECRET postgres://DSN_SECRET@invalid/database")
    error.__cause__ = ValueError("CAUSE_SECRET")
    error.__context__ = ValueError("CONTEXT_SECRET")
    result = Mock()
    result.result.return_value = [{"value": 1}]
    target.side_effect = [error, result] if outcome == "recovered" else error
    values = [["BOUND_SECRET"]] if operation == "execute_many" else ["BOUND_SECRET"]
    sql = "SELECT 'SQL_SECRET', $1"

    async def invoke():
        method = getattr(db, operation)
        if keyword:
            return await method(sql=sql, **{"rows" if operation == "execute_many" else "params": values})
        return await method(sql, values)

    with capture_logs() as events, stamina.set_testing(True, attempts=3, cap=True):
        if outcome == "recovered":
            await invoke()
        else:
            with pytest.raises(error_type) as caught:
                await invoke()
            assert caught.value is error

    attempts = {"recovered": 2, "exhausted": 3, "query_error": 1}[outcome]
    assert target.await_count == attempts
    assert error.__cause__.args == ("CAUSE_SECRET",)
    assert error.__context__.args == ("CONTEXT_SECRET",)
    scheduled = [event for event in events if event["event"] == "stamina.retry_scheduled"]
    assert len(scheduled) == attempts - 1
    rendered = repr(events)
    assert all(secret not in rendered for secret in (
        "SQL_SECRET", "BOUND_SECRET", "DSN_SECRET", "EXCEPTION_SECRET", "CAUSE_SECRET", "CONTEXT_SECRET",
    ))
    for retry_number, event in enumerate(scheduled, 1):
        assert event["callable"] == f"{database_module.__name__}.Database.{operation}"
        assert event["retry_num"] == retry_number
        assert event["caused_by"] == "ConnectionExecuteError()"


def test_database_retry_hook_preserves_unrelated_details_lifecycle_and_imports():
    original_hooks = get_on_retry_hooks()
    received = []
    lifecycle = []

    @contextmanager
    def lifecycle_context():
        lifecycle.append("enter")
        yield
        lifecycle.append("exit")

    def hook(details):
        received.append(details)
        return lifecycle_context()

    observer = Mock(return_value=None)
    try:
        set_on_retry_hooks([hook, observer])
        database_module._protect_retry_logs()
        installed = get_on_retry_hooks()
        database_module._protect_retry_logs()
        assert get_on_retry_hooks() == installed
        assert import_module(database_module.__name__) is database_module
        reload(database_module)
        assert get_on_retry_hooks() == installed
        error = ConnectionExecuteError("EXCEPTION_SECRET")
        error.__cause__ = ValueError("CAUSE_SECRET")
        error.__context__ = ValueError("CONTEXT_SECRET")
        error.private_payload = "ATTRIBUTE_SECRET"
        original = RetryDetails(
            name=f"{database_module.__name__}.Database.execute", args=("SQL_SECRET",),
            kwargs={"params": ["BOUND_SECRET"]}, retry_num=2, wait_for=0.5,
            waited_so_far=1.0, caused_by=error,
        )
        with installed[0](original):
            assert lifecycle == ["enter"]
        assert installed[1](original) is None
        assert lifecycle == ["enter", "exit"]
        safe = received[0]
        assert safe.args == ()
        assert safe.kwargs == {}
        assert type(safe.caused_by) is type(error)
        assert safe.caused_by.args == ()
        assert safe.caused_by.__cause__ is None
        assert safe.caused_by.__context__ is None
        assert safe.caused_by.__traceback__ is None
        assert vars(safe.caused_by) == {}
        assert observer.call_args.args[0].args == ()
        assert observer.call_args.args[0].kwargs == {}
        assert (safe.name, safe.retry_num, safe.wait_for, safe.waited_so_far) == (
            original.name, original.retry_num, original.wait_for, original.waited_so_far,
        )
        assert original.args == ("SQL_SECRET",)
        assert original.kwargs == {"params": ["BOUND_SECRET"]}
        assert original.caused_by is error
        for name in ("unrelated.call", f"{database_module.__name__}.Database.execute_extra"):
            unrelated = RetryDetails(name, (), {}, 1, 0, 0, error)
            with installed[0](unrelated):
                pass
            assert received[-1] is unrelated
            assert installed[1](unrelated) is None
            assert observer.call_args.args[0] is unrelated
    finally:
        set_on_retry_hooks(original_hooks)


async def test_a_declared_absence_skips_where_a_broken_database_fails(monkeypatch):
    """
    Two ways to have no database, and they must not read alike.

    A developer whose database is down needs the message telling them to start it. CI
    runs without one on purpose and needs the suite to go on. A green run with half the
    suite quietly missing is what the fail branch exists to prevent, so the skip branch
    is taken only where the absence is declared.
    """
    monkeypatch.setenv("POLICYDESK_TEST_NO_DB", "1")
    monkeypatch.setattr(conftest, "Database", Mock(side_effect=ValueError("private-secret")))

    with pytest.raises(pytest.skip.Exception) as caught:
        async with conftest.connected_database():
            pytest.fail("a pool was yielded with no database")

    assert "POLICYDESK_TEST_NO_DB" in str(caught.value)
    assert "private-secret" not in "".join(format_exception(caught.value))
