"""
Bootloader — must be the FIRST import in every entry point.

Mirrors enoract's bootloader, minus the parts that solve problems this project does
not have (free-threading detection, the httptools shim, the ujson block). What is kept
is the logger stack and its ordering constraint, because that constraint is not
optional and is invisible if you get it wrong.

Order matters:

- `structlog` is imported BEFORE `logxide`, so structlog's `_FixedFindCallerLogger`
  binds against the stdlib `logging.Logger`. logxide then swaps
  `sys.modules["logging"]` for its Rust dispatch, and the already-bound subclass keeps
  working. Import them the other way round and the swap happens under structlog.
- `_LogxideLoggerFactory` exists because `structlog.stdlib.LoggerFactory` captured
  `logging.getLogger` at import time — before the swap — so it would keep handing back
  stdlib loggers no matter what logxide did afterwards.
- Under pytest logxide skips the swap, so third-party loggers stay on stdlib and
  `caplog` still captures them.

Log lines are key-value: `logger.info("corpus_fetched", held=660, pending=51)` renders
as `corpus_fetched held=660 pending=51`. Grepping one field out of a run is the point.
"""

# ruff: noqa: I001  - structlog MUST precede logxide; see the module docstring above.
import logging
import os
from typing import TYPE_CHECKING, Any

import structlog
import logxide
from dotenv import load_dotenv

if TYPE_CHECKING:
    from collections.abc import MutableMapping

load_dotenv()

SERVICE_NAME = "policydesk"

_LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
_LEVEL_INT = level if isinstance(level := logging.getLevelName(_LOG_LEVEL), int) else logging.INFO

logging.basicConfig(
    level=_LEVEL_INT,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


class _LogxideLoggerFactory:
    """Return a logxide logger directly, bypassing structlog's captured getLogger."""

    def __call__(self, *args: str) -> Any:
        return logxide.getLogger(args[0] if args else SERVICE_NAME)


def _render_event_kv(_logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]) -> str:
    """Render an event dict as `<event> k1=v1 k2=v2`, with the event itself unquoted."""
    event = event_dict.pop("event", "")
    if not event_dict:
        return str(event)
    rest = " ".join(f"{k}={v}" for k, v in event_dict.items())
    return f"{event} {rest}" if event else rest


structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.format_exc_info,
        _render_event_kv,
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=_LogxideLoggerFactory(),
    cache_logger_on_first_use=True,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger(SERVICE_NAME)
