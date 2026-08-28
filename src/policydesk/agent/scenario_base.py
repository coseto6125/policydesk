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

from enum import StrEnum

from msgspec import Struct


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


class Scenario(Struct, frozen=True):
    """One thing the desk knows how to do."""

    name: str
    display_name: str
    description: str
    """Read by the router. Written for the model, not for an operator."""
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
        p.name: {"type": "string", "description": f"{p.description}{f'，例如 {p.example}' if p.example else ''}"}
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
            # model is free to skip.
            "required": [p.name for p in scenario.params],
            "additionalProperties": False,
        },
    }
