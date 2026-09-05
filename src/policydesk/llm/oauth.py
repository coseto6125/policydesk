"""
Read-only consumer of Claude Code's subscription OAuth credentials.

Claude Code stores its subscription OAuth token in `~/.claude/.credentials.json`
(`claudeAiOauth.accessToken`, an `sk-ant-oat01…` token). Sent as
`Authorization: Bearer` plus the `anthropic-beta: oauth-2025-04-20` header, it bills
`/v1/messages` against the subscription quota rather than Console pay-as-you-go
credit — an `ant auth login` Console profile 400s with "credit balance too low" on the
same request, and this token returns 200.

This module is **strictly read-only**. It never calls the OAuth refresh endpoint and
never writes the credential file. Anthropic's refresh tokens are single-use rotating,
so refreshing here without writing the rotated token back would invalidate Claude
Code's own credentials and force a `/login`. Freshness is delegated to Claude Code:
when the cached token nears expiry this re-reads the file, which Claude Code refreshes
in the background.

Ported from `enoract.shared.client.anthropic_oauth`, which has been running against
this API in production. Ported rather than imported: the two repositories deploy
separately, and a cross-repo import would tie policydesk's container to enoract's.
"""

import os
import time
from pathlib import Path

import msgspec

from policydesk.bootloader import logger

OAUTH_BETA_HEADER = "oauth-2025-04-20"
"""The beta header that makes the API accept a subscription OAuth Bearer token."""

_REFRESH_SKEW_S = 300.0
"""Re-read the file this many seconds before the cached token's stated expiry, so a
long-lived client picks up Claude Code's background refresh in time."""


def _default_creds() -> Path:
    """
    Resolve the credential file's path.

    Returns:
        `ANTHROPIC_OAUTH_CREDS_PATH` when it is set, and Claude Code's own location
        otherwise.

    Read per call rather than frozen at import, so a deployment can point at a token
    file synced from a machine where Claude Code is signed in — the default path
    exists only where Claude Code itself runs, which is not the case on a cloud host.

    """
    return Path(path) if (path := os.environ.get("ANTHROPIC_OAUTH_CREDS_PATH")) else Path.home() / ".claude" / ".credentials.json"


class _Cred(msgspec.Struct):
    accessToken: str  # noqa: N815 - matches Claude Code's JSON shape
    expiresAt: int  # noqa: N815 - epoch milliseconds


class _CredFile(msgspec.Struct):
    claudeAiOauth: _Cred  # noqa: N815 - matches Claude Code's JSON shape


class SubscriptionOAuthToken:
    """
    Cached read-only accessor for Claude Code's subscription OAuth token.

    `token()` returns the cached access token, re-reading the credential file when the
    cached one is within `_REFRESH_SKEW_S` of expiry. Refresh is Claude Code's job;
    this class only reads.
    """

    __slots__ = ("_expires_at", "_path", "_token")

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else _default_creds()
        self._token: str | None = None
        self._expires_at = 0.0

    @classmethod
    def available(cls, path: Path | None = None) -> bool:
        """
        Say whether a credential file exists to read.

        Args:
            path: Where to look, defaulting to the resolved path.

        Returns:
            True when the file is there. `build_provider` reads this to choose a
            provider without raising on a machine that has no credentials.

        """
        return (path if path is not None else _default_creds()).is_file()

    def token(self) -> str:
        """
        Return a subscription access token, re-reading the file near expiry.

        Returns:
            The access token.

        Raises:
            OAuthCredentialError: The file is missing or malformed.

        A token still past its expiry after the re-read means Claude Code has not
        refreshed yet. It is returned anyway so the API answers 401 and the caller
        surfaces a clear "sign in again" signal, rather than this blocking on a
        refresh it is forbidden to perform.

        """
        if self._token is None or time.time() >= self._expires_at - _REFRESH_SKEW_S:
            self._load()
            if time.time() >= self._expires_at:
                logger.warning("oauth_token_past_expiry", path=str(self._path))
        return self._token

    def _load(self) -> None:
        """
        Read the credential file into the cache.

        Raises:
            OAuthCredentialError: The file could not be read or did not parse.

        """
        try:
            raw = self._path.read_bytes()
        except OSError as exc:
            raise OAuthCredentialError(f"cannot read {self._path} (is Claude Code signed in?)") from exc
        try:
            cred = msgspec.json.decode(raw, type=_CredFile).claudeAiOauth
        except msgspec.DecodeError as exc:
            raise OAuthCredentialError(f"malformed credential file {self._path}") from exc
        self._token = cred.accessToken
        self._expires_at = cred.expiresAt / 1000.0


class OAuthCredentialError(RuntimeError):
    """The subscription credential file is missing or unreadable."""
