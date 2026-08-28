"""
What the desk says when nothing qualifies.

A customer told 沒有符合的商品 learns nothing and has nowhere to go. These guard the
two halves of the answer that replaces it: which condition is binding, and what a
single change would reach.
"""

from pathlib import Path

TOOLS = Path("src/policydesk/agent/tools.py").read_text()
EXECUTOR = Path("src/policydesk/agent/executor.py").read_text()


def test_each_probe_drops_exactly_one_predicate():
    """
    Attribution is the point: relaxing the age band alone is what the age band cost.

    A probe that drops two predicates at once reports an opening nobody can act on,
    because it does not say which of the two changes did the work.
    """
    body = TOOLS[TOOLS.index("async def alternatives"):TOOLS.index("async def required_documents")]
    for name, flags in (("放寬職業等級", "True, False, True"), ("放寬投保年齡", "False, True, True"), ("提高預算", "True, True, False")):
        line = body[body.index(name):]
        assert flags in line[:400], f"{name} does not drop exactly one predicate"


def test_the_probes_run_together():
    """Six sequential round trips is six times the wait, on a turn that ends in a refusal."""
    body = TOOLS[TOOLS.index("async def alternatives"):TOOLS.index("async def required_documents")]
    assert "asyncio.gather" in body


def test_the_probe_only_runs_when_nothing_qualified():
    """It is the fallback, not a second opinion on a list that already has products."""
    body = EXECUTOR[EXECUTOR.index('if "suitable_products" in scenario.tools'):EXECUTOR.index('facts["_criteria"]')]
    assert 'if not facts["suitable_products"]:' in body


def test_the_scenario_tells_the_model_what_to_do_with_them():
    """A tool result nothing in the prompt mentions is a tool result the model skips."""
    from policydesk.agent.scenario import RECOMMEND

    assert "alternatives" in RECOMMEND.injection
    assert "binding" in RECOMMEND.injection
    assert "openings" in RECOMMEND.injection


def test_an_empty_openings_list_is_not_filled_in_by_the_model():
    from policydesk.agent.scenario import RECOMMEND

    assert "不要自己想辦法" in RECOMMEND.injection


def test_tool_results_reach_the_model_as_toon():
    """
    Tool results are tabular, so TOON states each row's field names once, not per row.

    Measured on a product list: 41% fewer characters than JSON for the same rows, on the
    prompt the customer is waiting on.
    """
    assert "etoon.dumps({k: _short(v) for k, v in facts.items()})" in EXECUTOR
    assert "json.encode({k: _short(v)" not in EXECUTOR


def test_toon_encodes_the_shapes_the_tools_return():
    """Rows, an empty list and a nested dict all have to survive the encoder."""
    import etoon

    out = etoon.dumps({
        "suitable_products": [{"name": "新iLife一年期定期壽險", "unit_premium": 7430}],
        "list_policies": [],
        "_criteria": {"insurance_age": 56, "line": "life", "budget": 20000},
    })
    assert "新iLife一年期定期壽險" in out
    assert "7430" in out
    assert "20000" in out


def test_short_renders_the_types_the_encoder_refuses():
    """
    Etoon serialises through stdlib json, which raises on a date.

    A tool result carrying a policy's effective date is the common case: `list_policies`
    is gathered on every turn. The first live run after the swap died on exactly that,
    and the socket closed under the customer.
    """
    import datetime
    from decimal import Decimal

    import etoon

    from policydesk.agent.executor import _short

    rows = [{"policy_number": "CL1-2", "effective_at": datetime.date(2024, 3, 1), "premium": Decimal("7430.00")}]
    encoded = etoon.dumps({"list_policies": _short(rows)})
    assert "2024-03-01" in encoded
    assert "7430" in encoded


def test_short_still_clips_long_text_inside_rows():
    from policydesk.agent.executor import _short

    row = _short([{"verbatim": "條" * 900}])[0]
    assert len(row["verbatim"]) == 400
