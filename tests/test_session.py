"""
Eviction has two failure modes that only show up under a race, so they get tests.

Both were designed against, not discovered: a split session would let the back-office
pane and the customer pane tell different stories, and a late disconnect handler would
silently delete its own replacement.
"""

import pytest

from policydesk.web.session import EVICTION_NOTICE, Registry


class FakeSocket:
    """Records what a socket was told and whether it was closed."""

    def __init__(self, *, dead: bool = False) -> None:
        self.messages: list[str] = []
        self.closed = False
        self.dead = dead

    async def send(self, text: str) -> None:
        if self.dead:
            msg = "socket already gone"
            raise ConnectionError(msg)
        self.messages.append(text)

    async def close(self) -> None:
        if self.dead:
            msg = "socket already gone"
            raise ConnectionError(msg)
        self.closed = True


async def test_claim_unused_name_leaves_registry_with_one_session():
    registry = Registry()
    sock = FakeSocket()
    await registry.claim("王小明", sock.send, sock.close)

    assert len(registry) == 1
    assert "王小明" in registry
    assert sock.messages == []


async def test_claim_taken_name_evicts_previous_with_notice():
    registry = Registry()
    first, second = FakeSocket(), FakeSocket()
    await registry.claim("王小明", first.send, first.close)
    await registry.claim("王小明", second.send, second.close)

    assert first.messages == [EVICTION_NOTICE]
    assert first.closed
    assert second.messages == []
    assert len(registry) == 1


async def test_claim_when_previous_socket_is_dead_still_succeeds():
    """A browser that crashed cannot be told anything; the new claim must not block."""
    registry = Registry()
    dead, live = FakeSocket(dead=True), FakeSocket()
    await registry.claim("王小明", dead.send, dead.close)
    await registry.claim("王小明", live.send, live.close)

    assert len(registry) == 1
    assert "王小明" in registry


async def test_release_by_evicted_session_keeps_replacement_registered():
    """
    The evicted socket's disconnect handler fires after its replacement registered.

    Releasing on name alone would delete the live session and make the desk unreachable.
    """
    registry = Registry()
    first, second = FakeSocket(), FakeSocket()
    old = await registry.claim("王小明", first.send, first.close)
    await registry.claim("王小明", second.send, second.close)

    registry.release("王小明", old)

    assert "王小明" in registry, "the replacement must survive the evicted session's cleanup"


async def test_release_by_current_session_frees_the_name():
    registry = Registry()
    sock = FakeSocket()
    session = await registry.claim("王小明", sock.send, sock.close)

    registry.release("王小明", session)

    assert "王小明" not in registry
    assert len(registry) == 0


async def test_names_lists_sessions_oldest_first():
    registry = Registry()
    for name in ("陳大文", "林美華", "張志豪"):
        sock = FakeSocket()
        await registry.claim(name, sock.send, sock.close)

    assert registry.names == ["陳大文", "林美華", "張志豪"]


@pytest.mark.parametrize("name", ["王小明", "  ", "a" * 200])
async def test_claim_accepts_any_name_shape(name: str):
    """Name validation belongs at the edge, not here; the registry stays a plain map."""
    registry = Registry()
    sock = FakeSocket()
    await registry.claim(name, sock.send, sock.close)
    assert name in registry
