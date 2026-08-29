"""
What a call cost, from the rate the API key is billed at.

`llm_usage.cost_usd` existed from the first migration and nothing ever wrote it, so
every figure the console showed under 成本 was blank — 454 rows, none priced. The token
counts were there all along; the rate was the missing half, and a token count without a
rate is not a cost.

## The file, not the code, holds the rates

`model_pricing.json` sits next to this module and maps a model to its three rates in
USD per 1,000 tokens: `input` for uncached prompt tokens, `cached` for the cached-read
slice of them, `output` for completion tokens. A rate changes when a provider changes
it, which has nothing to do with a release of this code — so it is data, and
`POLICYDESK_LLM_PRICING_JSON` moves the file for a deployment that prices differently.

The format is deliberately the one `enoract.shared.client.llm.pricing` reads, so one
table serves both and neither has to be kept in step with the other by hand.

## A model with no entry costs NULL, never zero

Zero is a claim: it says the call was free. NULL says nobody priced it, which is the
true statement about a model absent from the table, and it is the one the console can
render as 未定價 rather than as a number an operator would add up.
"""

import os
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Final

from msgspec import DecodeError, json

from policydesk.bootloader import logger

if TYPE_CHECKING:
    from policydesk.llm.provider import Completion

PER = 1000.0
"""Tokens one rate covers. The published prices are per million; the file holds per
thousand, matching enoract's table so the two files are the same object."""

_PATH: Final[Path] = (
    Path(p) if (p := os.environ.get("POLICYDESK_LLM_PRICING_JSON")) else Path(__file__).with_name("model_pricing.json")
)

_RATES: dict[str, dict[str, float]] | None = None
_WARNED: set[str] = set()


def rates() -> dict[str, dict[str, float]]:
    """
    Read the rate table, once.

    Returns:
        Model name to its rates. Empty when the file is missing or will not parse,
        which makes every cost NULL rather than stopping the desk — a deployment
        without a price list still has to answer customers.

    """
    global _RATES
    if _RATES is not None:
        return _RATES
    try:
        _RATES = json.decode(_PATH.read_bytes(), type=dict[str, dict[str, float]])
    except (OSError, DecodeError) as exc:
        logger.warning("pricing_unavailable", path=str(_PATH), error=str(exc))
        _RATES = {}
    return _RATES


def price(model: str, *, prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0) -> float | None:
    """
    Cost one call in USD.

    Args:
        model: The model the provider billed under.
        prompt_tokens: Prompt tokens, cached ones included.
        completion_tokens: Completion tokens.
        cached_tokens: The cached-read slice of `prompt_tokens`.

    Returns:
        The cost, or None when the model has no entry — warned once per model, so a
        missing rate is visible in the log without one line per call.

    `cached_tokens` is a slice of `prompt_tokens`, not a figure beside it: the provider
    reports how many of the prompt tokens were served from cache, and billing them at
    both rates would double-count the same tokens. An entry with no `cached` rate bills
    them at `input`, which is the provider's own behaviour when it offers no discount —
    silently halving the bill instead would be the worse guess.

    """
    if (rate := rates().get(model)) is None:
        if model not in _WARNED:
            _WARNED.add(model)
            logger.warning("model_not_priced", model=model, path=str(_PATH))
        return None
    inp = rate.get("input", 0.0)
    cached = min(max(cached_tokens, 0), max(prompt_tokens, 0))
    return (
        (max(prompt_tokens, 0) - cached) / PER * inp
        + cached / PER * rate.get("cached", inp)
        + max(completion_tokens, 0) / PER * rate.get("output", 0.0)
    )


def cost(completion: Completion) -> Decimal | None:
    """
    Price a completion, in the type the column binds from.

    Args:
        completion: What came back, carrying the model and the token counts.

    Returns:
        The cost in USD, or None when the model has no rate.

    Decimal, not float: `cost_usd` is `numeric`, and psqlpy binds a numeric column from
    Decimal only — an int, a float or a str all fail with `insufficient data left in
    message`, a wire-protocol error naming neither the column nor the type. Both
    recorders write this column, so the conversion lives here rather than once beside
    each of them.
    """
    usd = price(
        completion.model,
        prompt_tokens=completion.prompt_tokens,
        completion_tokens=completion.completion_tokens,
        cached_tokens=completion.cached_tokens,
    )
    return None if usd is None else Decimal(f"{usd:.6f}")
