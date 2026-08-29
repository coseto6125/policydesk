"""
The contract every scenario module signs, checked on all of them at once.

A scenario written in its own module is dispatched by name and gated by derivation, so
nothing about it is declared in one place a reviewer can read. These tests are that
place. They walk `CATALOGUE`, take every scenario carrying a `tools_module`, and hold it
to the shape `executor._gather` assumes — because the executor assuming a shape and the
module not having it is a failure that shows up as a customer's private data in a reply,
not as an exception.

The gate test is the one that matters. It replaces every `@requires_identity` tool with a
function that raises, then calls `gather` with those names withheld. A module that honours
`allowed` returns; one that does not raises, and the raise is the proof that the query
would have run.
"""

import re
from datetime import UTC, datetime
from importlib import import_module
from typing import Any

import pytest

from policydesk.agent import tools
from policydesk.agent.scenario import CATALOGUE, Emit
from policydesk.core.db import Database

pytestmark = pytest.mark.asyncio

MODULE_SCENARIOS = [s for s in CATALOGUE if s.tools_module]


@pytest.fixture(scope="module")
async def db():
    pool = Database()
    try:
        await pool.fetch_val("SELECT 1")
    except Exception:
        pytest.skip("policydesk-pg is not up")
    return pool


@pytest.fixture(scope="module")
async def member_id(db):
    found = await db.fetch_val("SELECT member_id FROM policy GROUP BY member_id ORDER BY count(*) DESC LIMIT 1")
    if found is None:
        pytest.skip("no member holds a policy")
    return int(found)


def test_every_module_scenario_is_registered_once():
    names = [s.name for s in CATALOGUE]
    assert len(names) == len(set(names))
    assert MODULE_SCENARIOS, "at least soothe should be here; an empty list makes every test below vacuous"


@pytest.mark.parametrize("scenario", MODULE_SCENARIOS, ids=lambda s: s.name)
def test_a_module_scenario_exports_what_the_executor_imports(scenario):
    owner = import_module(scenario.tools_module)
    assert hasattr(owner, "TOOLS"), f"{scenario.name} has no TOOLS, so its gate resolves nothing"
    assert hasattr(owner, "gather"), f"{scenario.name} has no gather, so the executor has nothing to call"
    # The names the router advertises and the names the module can run are the same set.
    # A tool in one and not the other is either a gate that guards nothing or a tool the
    # scenario can never call.
    assert set(scenario.tools) == set(owner.TOOLS), (
        f"{scenario.name}: Scenario.tools {sorted(scenario.tools)} != TOOLS {sorted(owner.TOOLS)}"
    )


@pytest.mark.parametrize("scenario", MODULE_SCENARIOS, ids=lambda s: s.name)
def test_gather_takes_the_shared_signature(scenario):
    # Read off the code object rather than `inspect.signature`, which evaluates the
    # annotations — `Database` is imported under TYPE_CHECKING, so evaluating them raises
    # NameError and the test then fails for a reason that is not the contract.
    code = import_module(scenario.tools_module).gather.__code__
    for name in ("allowed", "retriever"):
        assert name in code.co_varnames, f"{scenario.name}: gather does not accept {name}"
    assert code.co_flags & 0x08, f"{scenario.name}: gather has no **kwargs, so member_id and today would fail"


@pytest.mark.parametrize("scenario", MODULE_SCENARIOS, ids=lambda s: s.name)
async def test_a_withheld_tool_is_never_called(scenario, db, member_id, monkeypatch):
    """
    The gate withholds the query, not the query's output.

    Every gated tool is replaced by one that raises. `gather` is then called with only the
    public names allowed. A module that checks `allowed` before dispatching returns; one
    that dispatches first and filters afterwards raises — and the raise says the member's
    row was read before they proved who they are.
    """
    owner = import_module(scenario.tools_module)
    allowed = tools.permitted(scenario.tools, owner=owner, confirmed=False)
    withheld = set(scenario.tools) - allowed
    if not withheld:
        pytest.skip(f"{scenario.name} reads no member record")

    def boom(*_args: Any, **_kwargs: Any):
        raise AssertionError(f"{scenario.name} called a tool it was not allowed to run")

    for name in withheld:
        fn = owner.TOOLS[name]
        # Patch where the function is defined, not only in TOOLS: a module calls its own
        # tool by its global name, and one borrowed from `agent.tools` by that module's
        # attribute. Patching the definition covers both spellings.
        monkeypatch.setattr(import_module(fn.__module__), fn.__name__, boom)
        monkeypatch.setitem(owner.TOOLS, name, boom)

    params = {p.name: p.example or "測試" for p in scenario.params}
    facts = await owner.gather(
        db, params, member_id=member_id, today=datetime.now(UTC).date(), retriever=None, allowed=allowed
    )
    assert isinstance(facts, dict)
    assert not (set(facts) & withheld), f"{scenario.name} returned material from a withheld tool"


@pytest.mark.parametrize("scenario", MODULE_SCENARIOS, ids=lambda s: s.name)
async def test_the_public_half_still_answers_when_everything_else_is_withheld(scenario, db, member_id, monkeypatch):
    # The reason the gate is per tool. A customer who has not verified gets what the
    # corpus says about everyone, not a bare refusal — so a scenario with any public tool
    # must return something under the same withholding as the test above.
    owner = import_module(scenario.tools_module)
    allowed = tools.permitted(scenario.tools, owner=owner, confirmed=False)
    if not allowed:
        pytest.skip(f"{scenario.name} has no public tool")

    def boom(*_args: Any, **_kwargs: Any):
        raise AssertionError("withheld")

    for name in set(scenario.tools) - allowed:
        fn = owner.TOOLS[name]
        monkeypatch.setattr(import_module(fn.__module__), fn.__name__, boom)
        monkeypatch.setitem(owner.TOOLS, name, boom)

    params = {p.name: p.example or "測試" for p in scenario.params}
    facts = await owner.gather(
        db, params, member_id=member_id, today=datetime.now(UTC).date(), retriever=None, allowed=allowed
    )
    assert set(facts) & allowed, f"{scenario.name} returned nothing an unverified customer could read"


@pytest.mark.parametrize("scenario", MODULE_SCENARIOS, ids=lambda s: s.name)
async def test_a_confirmed_turn_runs_every_tool(scenario, db, member_id):
    # The other direction. `allowed=None` is what a direct caller passes and what a
    # confirmed turn amounts to, and every tool the scenario advertises should produce a
    # key — otherwise the router offers the model a tool that returns nothing.
    owner = import_module(scenario.tools_module)
    params = {p.name: p.example or "測試" for p in scenario.params}
    facts = await owner.gather(
        db, params, member_id=member_id, today=datetime.now(UTC).date(), retriever=None, allowed=None
    )
    assert isinstance(facts, dict)
    assert set(facts) - {"_allowed_clauses", "_identity_required"}, f"{scenario.name} returned no material at all"


EXPLAINS = re.compile(r"是空的時候|沒有回傳任何項目時|查無|沒有符合")


@pytest.mark.parametrize(
    "scenario",
    [s for s in CATALOGUE if s.emit is not Emit.TEMPLATE and s.tools],
    ids=lambda s: s.name,
)
def test_a_scenario_says_what_an_empty_tool_result_means(scenario):
    """
    An empty result the model is not told about becomes 系統尚未回傳.

    That sentence reached a customer twice today — once when the table behind a tool held
    four rows, once when a lookup was correctly empty because the product has no surgery
    schedule. The model does not distinguish "nothing to find" from "could not look", and
    it will not invent the reassuring reading: 您名下沒有停效的保單 is good news, and a
    desk that reports it as a system failure has made a good day sound like a broken one.

    A template scenario is exempt: `_render` decides what an empty list looks like, and
    there is no model between the rows and the customer.
    """
    assert EXPLAINS.search(scenario.injection), (
        f"{scenario.name} runs {sorted(scenario.tools)} and never says what an empty one means"
    )
