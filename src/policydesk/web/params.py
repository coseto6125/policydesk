"""
Reading a query parameter, once.

Three copies of `int(request.args.get("member", ""))` wrapped in the same try/except grew
across two modules — twice in `console` (the second already generalised, which is the
module noticing the duplication and half-fixing it) and once in `server`, on the
customer-facing contract routes. They agree today. What splits them is the first change to
what counts as valid: someone tightening the console's rule has no reason to know the
contract viewer parses the same parameter its own way.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sanic import Request


def int_arg(request: Request, name: str = "member") -> int | None:
    """
    Read an optional integer query parameter.

    Args:
        request: The incoming request.
        name: Which parameter. Defaults to the one nearly every caller wants.

    Returns:
        The value, or None when it is absent or not a number. None is not an error: it
        reaches a query as a NULL against an `IS NULL OR` guard, which is how one statement
        serves the scoped and unscoped reads without a value ever being formatted into SQL.

    """
    try:
        return int(request.args.get(name, ""))
    except (TypeError, ValueError):
        return None
