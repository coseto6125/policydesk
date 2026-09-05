"""
The scenario type, and the tool schema it turns into.

Separate from `scenario` for one reason: a scenario written in its own module needs this
type, and `scenario` needs that module to put it in the catalogue. Both imports cannot be
at the top of the same pair of files — whichever runs second sees a half-built module and
raises ImportError, and *which* one runs second depends on what the process imported
first, so the failure appears and disappears with the entry point.

The type has no dependency on the catalogue, so it moves here and the cycle is gone
rather than ordered around.
"""

import asyncio
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from msgspec import Struct

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping


class Emit(StrEnum):
    """Where a scenario's answer comes from."""

    TEMPLATE = "template"
    """Rendered from data, verbatim. No model call."""
    MODEL = "model"
    """The model writes it, from material the tools returned."""


class Param(Struct, frozen=True):
    """
    One thing that must be known before a scenario can run.

    Every param is injected into the scenario tool's schema as both a property and a
    required field. A param that is only a property is a param the model may omit.
    """

    name: str
    description: str
    example: str = ""
    when_unsaid: str = ""
    """What to fill when the customer has not said this, where a scenario has an answer
    of its own. Empty takes `UNSAID`.

    A sentinel and a guess are different things, and only the parameter knows which it
    has. 全部 tells `find_clause` not to filter and 一般說明 tells a scenario to answer
    the general case; `health` would be one of five real lines and an answer to a
    question nobody asked. This field exists so the schema states one rule per parameter
    — with both sentences appended, the model read the global one last and filled an
    empty string where 全部 was meant, and `find_clause` then ran `ILIKE '%%'`."""


class Scenario(Struct, frozen=True):
    """One thing the desk knows how to do."""

    name: str
    display_name: str
    description: str
    """Read by the router. Written for the model, not for an operator."""
    summary: str = ""
    """One line naming what this scenario does, for an operator reading the console.

    Separate from `description` rather than sliced out of it. That one is routing
    material — a list of example questions and the cases to route elsewhere — and its
    first clause reads as a rule about when to pick the scenario, not as a statement of
    what the scenario is. A console cell built from it says 保戶問繳費相關的事情時使用,
    which tells an operator nothing they did not already read in the name.
    """
    injection: str = ""
    """Added to the model's instructions once this scenario is entered."""
    tools: tuple[str, ...] = ()
    params: tuple[Param, ...] = ()
    emit: Emit = Emit.MODEL
    template: str = ""
    """Used when emit is TEMPLATE. Formatted against the tool results."""
    transitions: tuple[str, ...] = ()
    """Scenarios reachable from this one."""
    requires_stage: str | None = None
    """The case stage this scenario needs. Refused with an explanation otherwise."""
    tools_module: str = ""
    """The dotted path of the module whose `TOOLS` and `gather` serve this scenario.

    Empty means `agent.tools`, where the desk's own tools live. A scenario written in its
    own module names that module here, and the executor then dispatches to it and derives
    its gate from it — rather than resolving the tool names against `agent.tools`, where
    they are not defined and would read as needing no 資料核對.
    """
    quick_replies: tuple[str, ...] = ()
    """Offered to the customer as one-tap follow-ups after this scenario answers.

    Every one is a question. None of them commits the customer to anything — a tap is
    one pixel away from a mis-tap, and a mis-tap that reads as 我要買這張 is an
    expression of intent the customer never made. Buying is typed, never tapped.
    """
    coverage_verdict: bool = False
    """Whether a reader can take this scenario's reply as whether something is covered.

    Set it and the waiting, exclusion and carve-back clauses of the customer's contracts
    are gathered whatever tools the scenario declares. It is not a prompt asking the
    model to consider them; it is the rows being present.

    The case that named it: 保單生效第 20 天生病住院，賠嗎？ routed to `claim_checklist`,
    whose tools read documents and multipliers and no clause bearing on cover. Its
    material held art.3, art.14 and art.20 and not art.2 — and art.3 grants cover for
    第二條約定之疾病, while art.2 defines 疾病 as 自本附約生效日起持續有效第三十一日起
    所發生的疾病. So the reply quoted a grant clause that defers to a definition nobody
    had read, and answered 符合本附約的保險範圍 about a contract with a 31-day wait.

    The citation check passed, and it was right to: it tests whether what was cited
    exists, not whether what mattered was cited. Those are different guarantees and only
    the first is built. The same question on the same product had been answered 不賠
    through `explain_cover`, which retrieves those clauses — so the desk gave two opposite
    answers depending on which scenario the router picked.

    A material gap is not a prompt problem. The model fills what the rows leave out.
    """
    dates: bool = False
    """Whether the answering call is offered the date tool.

    Off by default, for the calculator's reason. A scenario that states a deadline sets
    it, and its injection says which date the expression starts from: the day after
    delivery for a rescission, the lapse date for a reinstatement. Elsewhere the schema
    pins `date_calculations` empty, so a model that works out a date in prose on a
    scenario that offered no tool is withheld, not trusted.
    """
    calculator: bool = False
    """Whether the answering call is offered the calculator tool.

    Off by default. A calculator in every scenario let the router's free answer work out
    1+1 for a customer who had wandered off topic, and no scenario's injection asks the
    model to compute: the figures a customer reads come from tool rows, and `quote` runs
    `calculate` itself, deterministically, before the model sees the material.
    """


UNSAID = "Leave this an empty string when the customer did not say it. Fill nothing in from an example, a common case or a guess."
"""Said on every parameter, because `required` makes the model produce *something*.

保戶 asked 我想問住院理賠 — five characters, no duration — and the router filled
`event` with 住院四天, which is the parameter's own example. Nothing downstream could
catch it: the value is well formed, the scenario is right, and `faults` stays empty.
The pressure comes from the schema, so the release belongs there too. `budget` never
invented a figure in the same replay because 只填阿拉伯數字 makes a made-up value
obviously wrong; this line does that job for the parameters whose value is prose.
"""


def tool_schema(scenario: Scenario) -> dict:
    """
    Turn a scenario into a tool the router can call.

    Args:
        scenario: The scenario to expose.

    Returns:
        A function-tool schema whose properties and required list both carry every
        parameter.

    """
    properties = {
        p.name: {
            "type": "string",
            "description": (
                f"{p.description}{f'，例如 {p.example}' if p.example else ''}。"
                f"{p.when_unsaid or UNSAID}"
            ),
        }
        for p in scenario.params
    }
    return {
        "type": "function",
        "name": scenario.name,
        "description": scenario.description,
        "parameters": {
            "type": "object",
            "properties": properties,
            # Both lists, always. A property that is not required is a question the
            # model is free to skip — and `_gather` reads a missing key and an empty one
            # the same way, so requiring it costs nothing and keeps the router honest
            # about what it did and did not hear.
            "required": [p.name for p in scenario.params],
            "additionalProperties": False,
        },
    }


async def gather_tools(
    factories: Mapping[str, Callable[[], Awaitable[Any]]], *, allowed: frozenset[str] | None
) -> dict[str, Any]:
    """
    Run the tools a scenario is permitted this turn, together.

    Args:
        factories: Tool name to a zero-argument callable returning its coroutine. A
            callable rather than a coroutine, because a coroutine built for a tool the
            gate withholds is never awaited and warns about it — and building it at all
            reads as though the query might run.
        allowed: The names the executor's identity gate permits. None permits all.

    Returns:
        Tool name to result, for the tools that ran. A withheld tool has no key, which is
        what tells the executor's `_identity_required` flag apart from a tool that
        genuinely returned nothing.

    Two things at once, and they belong together. The gate is written once instead of
    once per scenario module — six copies of `allowed is None or name in allowed` is six
    places a fix has to land. And the permitted tools go out concurrently rather than one
    at a time: they read disjoint tables and none feeds another, so awaiting them in
    sequence pays a round trip per tool on the path a customer is waiting on. Measured on
    a local Postgres it is 5% to 22% of the scenario's own time; over a networked one it
    is a full round trip each.

    A tool that needs another's output cannot go in the same call. Ask twice — the second
    map is built from the first's results, and that dependency is then visible in the
    code rather than implied by the order of two awaits.

    """
    wanted = {name: make for name, make in factories.items() if allowed is None or name in allowed}
    if not wanted:
        return {}
    results = await asyncio.gather(*(make() for make in wanted.values()))
    return dict(zip(wanted, results, strict=True))
