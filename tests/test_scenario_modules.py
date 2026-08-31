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


def test_every_scenario_carries_an_operator_summary():
    """
    A scenario with no summary is a blank cell in the console.

    `description` cannot stand in for it. That field is routing material written for the
    model — a list of example questions followed by the cases to route elsewhere — and
    its opening clause reads as a rule about when to pick the scenario rather than as a
    statement of what the scenario does. The console showed the English key alone before
    this field existed, which an operator has to already know to read.
    """
    missing = [s.name for s in CATALOGUE if not s.summary.strip()]
    assert not missing, f"no summary, so the console shows these as a bare key: {missing}"


def test_a_summary_is_one_short_line():
    # Long enough to say what the scenario does, short enough for a table cell. The
    # failure it guards is someone pasting the routing description in.
    too_long = [(s.name, len(s.summary)) for s in CATALOGUE if len(s.summary) > 40]
    assert not too_long, f"summary is a table cell, not a paragraph: {too_long}"


def test_every_parameter_says_what_to_do_when_the_customer_has_not_said_it():
    """
    `required` makes the model produce something; this is what it produces instead.

    保戶 asked 我想問住院理賠 — no duration in it anywhere — and the router filled
    `event` with 住院四天, which is that parameter's own example. Nothing downstream can
    catch that: the value is well formed, the scenario is right, `faults` comes back
    empty, and the reply then answers a question about a four-day stay nobody mentioned.

    Asserted on the schema the router actually receives rather than on `Param`, because
    the fix belongs where the pressure is — one rule per parameter in `tool_schema`
    covers the fifteen spread across ten modules, and the sixteenth gets it for free.
    """
    from policydesk.agent.scenario_base import tool_schema

    without = [
        f"{s.name}.{name}"
        for s in CATALOGUE
        if s.params
        for name, spec in tool_schema(s)["parameters"]["properties"].items()
        if "填空字串" not in spec["description"]
    ]
    assert not without, f"these parameters invite the example when the customer said nothing: {without}"


def test_a_parameter_states_one_rule_for_an_unsaid_value_and_not_two():
    """
    Two rules on one parameter, and the model reads the last one.

    `UNSAID` used to be appended to every description, including the four that already
    said what to fill. An `explain_cover` call with no topic then filled `""` where 全部
    was meant, and `find_clause`'s SQL fallback ran `ILIKE '%%'` — which is not the whole
    contract the description promises, it is whatever the kind ordering and the limit
    happen to leave.

    A parameter carrying its own answer states that one; the rest take the global rule.
    Never both.
    """
    from policydesk.agent.scenario_base import UNSAID, tool_schema

    for scenario in (s for s in CATALOGUE if s.params):
        schema = tool_schema(scenario)
        for param in scenario.params:
            text = schema["parameters"]["properties"][param.name]["description"]
            if param.when_unsaid:
                where = f"{scenario.name}.{param.name}"
                assert param.when_unsaid in text, f"{where}'s own rule never reaches the schema"
                assert UNSAID not in text, f"{where} carries its own rule and the global one"
            else:
                assert UNSAID in text, f"{scenario.name}.{param.name} states no rule at all"


def test_a_sentinel_means_do_not_filter_and_never_names_one_option():
    """
    A fallback that means "all of it" is not a guess; one that names a real value is.

    `browse_products.line` and `quote.line` both said 保戶沒指明就填 health. 你們有賣什麼
    and 我想問保險 arrived naming no line and were both shown 醫療 — one of five, and the
    answer to neither. 全部 and 一般說明 stay: 一般說明 is what 我最近換工作，想問下跟以前
    的保險會有關係嗎 routes with, and it produced §59's four paragraphs and every policy's
    occupation ceiling.
    """
    lines = {"health", "life", "accident", "annuity", "investment"}
    guessing = [
        f"{s.name}.{p.name}"
        for s in CATALOGUE
        for p in s.params
        if any(
            re.search(rf"沒[^。]{{0,10}}填\s*{value}\b", f"{p.description}{p.when_unsaid}")
            for value in lines
        )
    ]
    assert not guessing, f"these parameters answer a question the customer did not ask: {guessing}"


def test_a_parameter_is_still_required():
    """
    The empty string is the answer, not a missing key.

    Making the property optional would let the router drop a parameter it did hear, and
    `_gather` cannot tell that from one the customer never gave.
    """
    from policydesk.agent.scenario_base import tool_schema

    for scenario in (s for s in CATALOGUE if s.params):
        schema = tool_schema(scenario)
        assert set(schema["parameters"]["required"]) == {p.name for p in scenario.params}, scenario.name
