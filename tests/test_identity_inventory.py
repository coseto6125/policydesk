"""
Every tool a scenario can run carries an explicit decision about 資料核對.

The runtime gate admits only explicit public declarations or identity-required tools
after confirmation. Missing declarations remain excluded, whether the tool resolves
in the shared module or a scenario-local registry.

These tests make the decision explicit and check it against what the tool actually reads.
"""

import importlib
import inspect
from types import SimpleNamespace

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


@pytest.mark.parametrize("local", [False, True], ids=["global", "scenario-owner"])
@pytest.mark.parametrize(("declaration", "reads", "unconfirmed", "confirmed"), [
    (False, False, True, True),
    (True, True, False, True),
    ("missing", True, False, False),
    (None, True, False, False),
    (0, True, False, False),
    (1, True, False, False),
    ("false", True, False, False),
])
def test_identity_gate_only_explicit_boolean_declarations_authorize_tools(
    monkeypatch, local, declaration, reads, unconfirmed, confirmed,
):
    async def probe():
        raise AssertionError("declaration inspection must not execute the tool")

    if declaration != "missing":
        probe.requires_identity = declaration
    owner = SimpleNamespace(TOOLS={"declaration_probe": probe}) if local else None
    if not local:
        monkeypatch.setattr(tools, "declaration_probe", probe, raising=False)
    assert tools.reads_identity(["declaration_probe"], owner=owner) is reads
    for verified, allowed in [(False, unconfirmed), (True, confirmed)]:
        expected = frozenset({"declaration_probe"}) if allowed else frozenset()
        assert tools.permitted(["declaration_probe"], owner=owner, confirmed=verified) == expected


def test_identity_gate_unmarked_owner_override_cannot_inherit_global_public_access(monkeypatch):
    @tools.public
    async def published():
        return "public catalogue"

    async def unreviewed():
        return "private record"

    monkeypatch.setattr(tools, "shadow_probe", published, raising=False)
    owner = SimpleNamespace(TOOLS={"shadow_probe": unreviewed})
    assert tools.reads_identity(["shadow_probe"], owner=owner) is True
    assert tools.permitted(["shadow_probe"], owner=owner, confirmed=False) == frozenset()
    assert tools.permitted(["shadow_probe"], owner=owner, confirmed=True) == frozenset()


@pytest.mark.parametrize("confirmed", [False, True])
async def test_gather_unmarked_member_reader_is_not_called_even_after_confirmation(monkeypatch, confirmed):
    from datetime import date
    from unittest.mock import AsyncMock

    from msgspec import structs

    from policydesk.agent.executor import Turn, _gather
    from policydesk.agent.scenario import BY_NAME

    async def unreviewed(*args, **kwargs):
        raise AssertionError("unmarked member reader executed")

    monkeypatch.setattr(tools, "list_policies", unreviewed)
    # A real Scenario, not a stand-in. `_gather` reads fields off it, and a namespace
    # carrying only the three this test cared about breaks the moment one is added.
    scenario = structs.replace(BY_NAME["policy_overview"], tools=("list_policies",), tools_module="", params=())
    await _gather(AsyncMock(), scenario, Turn(case_id=1, member_id=1),
                  today=date(2026, 9, 6), params={}, confirmed=confirmed)


def test_alternatives_declares_public_catalogue_access_explicitly():
    assert vars(tools.alternatives).get("requires_identity") is False
