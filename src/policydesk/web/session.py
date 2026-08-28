"""
Who is at the desk right now.

Identity is a display name and nothing else. That is a demo decision, not a security
one, and the code says so rather than implying a login system exists: there is no
password, no token, no account record. A name claims the desk; the previous holder of
that name is disconnected and told why.

The eviction is the interesting half. Two browsers claiming 王小明 must not both drive
the same conversation, because the back-office pane on the left mirrors the customer
pane on the right and a split session would show a caseworker one story while the
customer sees another. So the second claim wins and the first is closed with a reason
it can display, rather than both being left connected to diverge quietly.
"""

import asyncio
from collections.abc import Awaitable, Callable  # noqa: TC003  - msgspec evaluates these at runtime
from time import time

from msgspec import Struct

from policydesk.bootloader import logger

EVICTION_NOTICE = "此名稱可能有人正在使用，您已被登出。請改用不易重名的名稱。"


class Session(Struct):
    """One person at the desk."""

    name: str
    opened_at: float
    send: Callable[[str], Awaitable[None]]
    """Delivers a message to this session's socket."""
    close: Callable[[], Awaitable[None]]
    """Closes this session's socket."""


class Registry:
    """
    The set of live sessions, keyed by name.

    Not thread-safe by design: Sanic runs the websocket handlers on one event loop per
    worker, and a lock here would only hide the fact that two workers would need a
    shared store anyway. The demo runs one worker.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def __contains__(self, name: str) -> bool:
        return name in self._sessions

    def __len__(self) -> int:
        return len(self._sessions)

    @property
    def names(self) -> list[str]:
        """Who is currently connected, oldest first."""
        return sorted(self._sessions, key=lambda n: self._sessions[n].opened_at)

    async def claim(
        self,
        name: str,
        send: Callable[[str], Awaitable[None]],
        close: Callable[[], Awaitable[None]],
    ) -> Session:
        """
        Take the desk under a name, evicting whoever held it.

        Args:
            name: The display name being claimed.
            send: Delivers a message to the claiming socket.
            close: Closes the claiming socket.

        Returns:
            The new session.

        """
        if (previous := self._sessions.get(name)) is not None:
            logger.info("session_evicted", name=name)
            await self._evict(previous)

        session = Session(name=name, opened_at=time(), send=send, close=close)
        self._sessions[name] = session
        return session

    async def _evict(self, session: Session) -> None:
        """
        Tell a session it has been displaced, then close it.

        Args:
            session: The session losing the name.

        The notice is best-effort: a socket that has already gone away cannot be told
        anything, and failing to close it must not block the incoming claim.

        """
        try:
            await session.send(EVICTION_NOTICE)
            await session.close()
        except (ConnectionError, asyncio.CancelledError, RuntimeError) as exc:
            logger.debug("eviction_moot", name=session.name, error=str(exc))

    def release(self, name: str, session: Session) -> None:
        """
        Drop a session when its socket closes.

        Args:
            name: The name it held.
            session: The session that is going away.

        A session only releases the name if it still holds it. Without that check, an
        evicted socket's own disconnect handler deletes the entry that its replacement
        just installed, and the new holder becomes invisible to the registry.

        """
        if self._sessions.get(name) is session:
            del self._sessions[name]
