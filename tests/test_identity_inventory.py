"""
Every tool a scenario can run carries an explicit decision about 資料核對.

The runtime gate already fails closed: `reads_identity` treats a name it cannot resolve as
gated, so a forgotten decorator costs an unnecessary ID request rather than a leak. That
protects the data and hides the omission. Absence of a flag currently means two different
things — someone decided this tool is public, and nobody thought about it — and the desk
cannot tell them apart.

These tests make the decision explicit and check it against what the tool actually reads.
"""

import importlib
import inspect

import pytest

from policydesk.agent import tools
from policydesk.agent.scenario import CATALOGUE

PERSONAL_TABLES = (
    "FROM member", "JOIN member", "FROM policy", "JOIN policy", "premium_payment",
    "member_fact", "FROM claim", "JOIN claim", "FROM beneficiary", "JOIN beneficiary",
    "conversation_message",
)
"""
Tables holding one named person's record. A tool touching these reads 個人資料.

A name that is also an ordinary word carries its FROM/JOIN, because the bare word matches
prose as readily as SQL. `beneficiary` appears in the docstrings of the §110-112 statute
tools, which read the law and no record at all.
"""


def registry() -> dict[str, object]:
    """
    Resolve every tool any scenario can run, the way the gate resolves it.

    Returns:
        Tool name to function, for every name in every scenario's `tools`.

    """
    found: dict[str, object] = {}
    for scenario in CATALOGUE:
        owner = importlib.import_module(scenario.tools_module) if scenario.tools_module else None
        catalogue = dict(getattr(owner, "TOOLS", {}) or {})
        for name in scenario.tools:
            resolved = catalogue.get(name, getattr(tools, name, None))
            if resolved is not None:
                found[name] = resolved
    return found


def test_every_scenario_tool_resolves():
    """A name the gate cannot resolve is gated by fallback, and nobody is told."""
    unresolved = []
    for scenario in CATALOGUE:
        owner = importlib.import_module(scenario.tools_module) if scenario.tools_module else None
        catalogue = dict(getattr(owner, "TOOLS", {}) or {})
        unresolved += [
            f"{scenario.name}.{name}"
            for name in scenario.tools
            if catalogue.get(name, getattr(tools, name, None)) is None
        ]
    assert not unresolved, f"tools no module defines: {unresolved}"


@pytest.mark.parametrize("name", sorted(registry()))
def test_every_tool_declares_whether_it_reads_the_customers_own_record(name):
    """
    One of `@requires_identity` or `@public`, set on the function itself.

    `getattr(fn, "requires_identity", False)` cannot tell a decision from an omission, so
    this reads the function's own `__dict__`: a flag inherited or defaulted does not count.
    """
    fn = registry()[name]
    assert "requires_identity" in vars(fn), (
        f"`{name}` declares nothing. Mark it `@requires_identity` when it reads one named "
        f"customer's record, or `@public` when everyone gets the same answer."
    )


@pytest.mark.parametrize("name", sorted(registry()))
def test_a_tool_declared_public_does_not_read_a_personal_table(name):
    """The declaration has to match the SQL, or it is a comment rather than a gate."""
    fn = registry()[name]
    if vars(fn).get("requires_identity", True):
        return
    try:
        source = inspect.getsource(fn)
    except OSError:
        pytest.skip(f"no source for {name}")
    touched = [table for table in PERSONAL_TABLES if table in source]
    assert not touched, (
        f"`{name}` is declared public and queries {touched}. Either it needs "
        f"`@requires_identity`, or the query has to stop reading that table."
    )


def test_the_gate_treats_an_unknown_name_as_gated():
    """
    The floor of the whole gate, pinned.

    `reads_identity` returning False for a name it cannot resolve would let a scenario
    read member data before the customer proves who they are, silently. This is the one
    line that must not move, so it is asserted rather than left to review.
    """
    assert tools.reads_identity(["a_tool_no_module_defines"]) is True
