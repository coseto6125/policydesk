"""
The de-escalation scenario, and the two ways it can hurt a customer.

The first is the obvious one: a statute citation the model invented. It reads as law,
carries the authority of law, and points at a sentence that does not exist.

The checker for it lives in `agent/statute.py` and is exercised in `test_statute.py`,
because where it lives turned out to matter more than whether it worked. It began in this
module, correct and tested and never called: the executor read `art.NN` out of replies and
compared it to the member's contracts, and a statute citation matched neither pattern nor
corpus, so an invented 第999條 shipped verbatim past a check this file's docstring claimed
was running. A checker a scenario owns is a checker the executor cannot reach.

What is left here is the scenario's own behaviour, and the tests that assert an invented
provision is withheld now run a turn through the executor rather than calling the checker
directly — the earlier version would have passed unchanged on the day nothing called it.

The second is quieter and worse: the desk quotes the provision that justifies the company
and stops there. 保險法 §64 II is the insurer's right to rescind; §64 III is the two-year
limit that takes it away. Both are true, one is an answer and the other is a threat. The
tests below assert that the retrieval reaches the customer's half, and that the scenario
runs without demanding ID from someone mid-complaint.
"""

import inspect

import pytest

from policydesk.agent import statute, tools
from policydesk.agent.scenarios.soothe import (
    SOOTHE,
    complaint_channel,
    gather,
    statute_reference,
)


@pytest.fixture(scope="module")
async def db(db):
    if not await db.fetch_val("SELECT count(*) FROM statute_article"):
        await statute.ingest(db)
    return db


def test_cited_reads_article_paragraph_and_item():
    assert statute.cited("依〔保險法 第64條第2項〕，…") == [("保險法", "art.64.2")]
    assert statute.cited("〔保險法 第65條第1項第2款〕") == [("保險法", "art.65.1.2")]
    assert statute.cited("〔保險法 第8-1條〕") == [("保險法", "art.8-1")]


def test_cited_deduplicates_a_provision_quoted_twice():
    assert statute.cited("〔保險法 第64條〕…〔保險法 第64條〕") == [("保險法", "art.64")]


def test_statute_citation_does_not_collide_with_the_clause_syntax():
    # The executor pulls `art.NN` out of a reply and voids any the customer's contracts
    # do not contain. A statute written as art.64.2 would be read as a clause, found in
    # no policy, and take the whole reply down with it.
    import re

    from policydesk.agent.executor import _CITATION

    assert not _CITATION.findall("依〔保險法 第64條第2項〕，保險人得解除契約。")
    assert re.search(r"art\.", "art.12")  # the clause syntax itself still matches


async def test_recheck_passes_a_real_provision(db):
    assert await statute.unresolved(db, "依〔保險法 第64條第2項〕，…") == []


async def test_recheck_catches_an_article_that_does_not_exist(db):
    # 保險法 stops well short of 第999條. The pair comes back as written, not as the
    # article it would belong to, so the caller can point at the exact citation to strike.
    assert await statute.unresolved(db, "〔保險法 第999條第1項〕") == [("保險法", "art.999.1")]


async def test_recheck_catches_a_provision_attributed_to_the_wrong_statute(db):
    # 保險法 §64 exists and 保險法施行細則 §64 does not. Same number, different Act — the
    # misattribution hardest for a reader to catch.
    assert await statute.unresolved(db, "〔保險法施行細則 第64條第2項〕") == [("保險法施行細則", "art.64.2")]


async def test_recheck_catches_a_paragraph_beyond_the_article(db):
    # 保險法 §64 has three 項. A fourth is a sentence that sounds like law and is not.
    assert await statute.unresolved(db, "〔保險法 第64條第9項〕") == [("保險法", "art.64.9")]


async def test_statute_reference_finds_the_provision_behind_a_complaint(db):
    rows = await statute_reference(db, "解除契約", limit=6)
    assert rows
    assert any(r["doc_id"] == "art.64.3" for r in rows), "the two-year limit must be reachable"


async def test_statute_reference_citations_are_written_the_way_the_checker_reads_them(db):
    rows = await statute_reference(db, "解除契約", limit=6)
    for row in rows:
        assert statute.cited(row["citation"]) == [(row["statute"], row["doc_id"])]


async def test_statute_reference_citations_all_survive_the_recheck(db):
    # The model is told to copy `citation` verbatim, so a citation the tool itself
    # formats wrongly is a reply withheld for a provision that was really there.
    rows = await statute_reference(db, "申訴", limit=6)
    assert not await statute.unresolved(db, " ".join(r["citation"] for r in rows))


async def test_complaint_channel_states_the_statutory_deadline(db):
    route = await complaint_channel(db)
    assert route["ombudsman_deadline_days"] == "60"
    assert route["basis"], "the escalation route must name the provision it rests on"


async def test_complaint_channel_matches_what_the_act_actually_says(db):
    # The numbers are asserted against the statute text rather than against themselves,
    # because a service promise drifting from the Act is exactly the error the corpus is
    # here to prevent.
    route = await complaint_channel(db)
    rows = await statute.find_articles(db, ["art.13.2"], ["financial_consumer_protection_act"])
    text = rows[0]["verbatim"]
    assert "三十日" in text
    assert route["internal_days"] == "30"
    assert "六十日" in text
    assert route["ombudsman_deadline_days"] == "60"


async def test_gather_returns_both_halves(db):
    facts = await gather(db, {"concern": "你們憑什麼解除我的契約"})
    assert facts["statute_reference"], "provisions without the route reads as being read the law"
    assert facts["complaint_channel"], "the route without provisions reads as being shown the door"


def test_soothe_tools_read_no_member_record():
    # The property that matters, asserted on the function objects themselves: neither tool
    # is marked, so neither may read one named customer's record. A person angry enough to
    # be shouting is the worst audience for 請提供身分證字號.
    from policydesk.agent.scenarios.soothe import TOOLS

    assert not [name for name, fn in TOOLS.items() if getattr(fn, "requires_identity", False)]


def test_the_gate_treats_a_name_it_cannot_resolve_as_protected():
    # The direction this fails in is the whole point. `reads_identity` resolves names
    # through `tools`, so a tool written in a scenario package resolves to nothing — and
    # reading nothing as 「不需核對」 would wave a future @requires_identity tool through
    # the gate silently. Unknown therefore means gated.
    assert tools.reads_identity(("no_such_tool_at_all",))
    assert set(SOOTHE.tools).isdisjoint(dir(tools)), "if these ever land in tools.py, this note is stale"


def test_soothe_names_the_module_its_gate_is_derived_from():
    # Unknown names are gated, so a scenario whose tools live in its own module has to
    # say where they live. Given the module the derivation runs on the real functions and
    # answers no; given nothing it answers yes, and an angry customer asking about public
    # law is met with 請提供身分證字號.
    from importlib import import_module

    assert SOOTHE.tools_module, "without this the gate resolves nothing and refuses everything"
    owner = import_module(SOOTHE.tools_module)
    assert not tools.reads_identity(SOOTHE.tools, owner=owner)
    assert tools.reads_identity(SOOTHE.tools), "and gated when the module is not named"


def test_the_scenario_module_contract_is_what_the_executor_calls():
    # One signature, so the executor's dispatch is one line for every scenario module.
    # A module reading member data needs member_id and today; soothe reads none and takes
    # them anyway, because a contract each caller special-cases is not one.
    from policydesk.agent.scenarios import soothe

    # Read off the code object rather than `inspect.signature`, which evaluates the
    # annotations — and `Database` is only imported under TYPE_CHECKING, so evaluating
    # them raises NameError and the test fails for a reason that is not the contract.
    code = soothe.gather.__code__
    assert "retriever" in code.co_varnames
    assert code.co_flags & 0x08, "member_id and today are passed to every scenario module"


def test_soothe_forbids_admitting_fault_and_promising_payment():
    assert "不可以承認公司有錯" in SOOTHE.injection
    assert "不可以承諾會賠" in SOOTHE.injection


def test_soothe_requires_the_favourable_half_of_a_provision():
    assert "對保戶有利的部分要一起講" in SOOTHE.injection


def test_soothe_forbids_uncited_statute():
    assert "不可以引用工具沒有回傳的條文" in SOOTHE.injection


def test_soothe_collects_the_complaint_in_the_customers_own_words():
    concern = next(p for p in SOOTHE.params if p.name == "concern")
    assert "不要改寫成公司用語" in concern.description


def test_soothe_hands_off_to_a_scenario_that_can_read_the_policy():
    # It cannot answer 這條怎麼適用在我身上 itself — that needs his contract, which needs
    # verification. The transition is what makes the refusal a next step rather than a wall.
    assert "explain_cover" in SOOTHE.transitions


async def test_every_provision_in_the_corpus_round_trips_through_the_citation_format(db):
    # The property the whole check rests on: a citation the tool formats must be one the
    # reader reads back as the same provision. Asserted over all 1,212 rather than on
    # examples, because that is how 第149-10條 was found — a single-digit branch pattern
    # read it as no citation at all, and an unreadable citation is not one the checker
    # rejects, it is one the checker never sees.
    rows = await db.fetch(
        """SELECT a.statute_id, s.name AS statute_name, a.doc_id, a.article, a.branch,
                  a.paragraph, a.subparagraph
           FROM statute_article a JOIN statute s USING (statute_id)"""
    )
    bad = [r["doc_id"] for r in rows if statute.cited(statute.citation(r)) != [(r["statute_name"], r["doc_id"])]]
    assert not bad, bad[:10]


async def test_a_two_digit_branch_article_is_readable_and_checkable(db):
    assert statute.cited("〔保險法 第149-10條第3項〕") == [("保險法", "art.149-10.3")]
    assert await statute.unresolved(db, "〔保險法 第149-10條第3項〕") == []
    assert await statute.unresolved(db, "〔保險法 第149-99條〕") == [("保險法", "art.149-99")]


def test_the_statutes_own_branch_notation_is_accepted():
    # 之十 is how the Act writes it in its own cross-references, and a model copying the
    # corpus will sometimes copy that.
    assert statute.cited("〔保險法 第149之10條第3項〕") == [("保險法", "art.149-10.3")]


def test_a_citation_written_in_prose_is_still_read():
    # The model is told to copy the bracketed form, and mostly does. What matters is what
    # happens when it does not: a citation the pattern misses is not one the checker
    # rejects, it is one the checker never sees, so the reply ships uncheckable.
    assert statute.cited("依保險法第64條第2項，保險人得解除契約") == [("保險法", "art.64.2")]
    assert statute.cited("（保險法 第64條第2項）") == [("保險法", "art.64.2")]
    assert statute.cited("〔保險法 第 64 條 第 2 項〕") == [("保險法", "art.64.2")]


def test_a_citation_in_chinese_numerals_is_read():
    # The statute cross-references itself this way — 第六十四條第三項 appears verbatim
    # inside 第68條 — so a model quoting the corpus reproduces it.
    assert statute.cited("根據保險法第六十四條第二項") == [("保險法", "art.64.2")]
    assert statute.cited("保險法第一百四十九條之十第三項") == [("保險法", "art.149-10.3")]


def test_both_notations_for_a_branch_article_agree():
    # Digits put 之N before 條 and words put it after. Both are in the corpus.
    assert statute.cited("〔保險法 第8-1條第1項〕") == statute.cited("〔保險法 第八條之一第1項〕") == [("保險法", "art.8-1.1")]


def test_a_leading_particle_does_not_become_part_of_the_statutes_name():
    # The name is half the key the recheck looks up. 依保險法 matches no statute, so a real
    # citation would be reported as invented and the reply withheld for nothing.
    assert statute.cited("依保險法第64條") == statute.cited("參照保險法第64條") == [("保險法", "art.64")]


def test_a_contract_article_is_not_read_as_a_statute():
    # 本契約第3條 is the customer's own policy. Without the 法/細則/辦法 anchor the checker
    # would look it up as a statute, find nothing, and void a reply nobody miscited in.
    assert statute.cited("本契約第3條約定的等待期") == []
    assert statute.cited("依第64條規定") == []


async def test_every_provision_resolves_from_the_prose_form_too(db):
    rows = await db.fetch(
        """SELECT a.statute_id, s.name AS statute_name, a.doc_id, a.article, a.branch,
                  a.paragraph, a.subparagraph
           FROM statute_article a JOIN statute s USING (statute_id)"""
    )
    bad = []
    for row in rows:
        prose = "依" + row["statute_name"] + statute.citation(row).strip("〔〕").split(" ", 1)[1]
        if statute.cited(prose) != [(row["statute_name"], row["doc_id"])]:
            bad.append(row["doc_id"])
    assert not bad, bad[:10]


async def test_the_looser_pattern_still_catches_an_invented_provision(db):
    # Reading more forms must not mean rejecting fewer. Both directions asserted.
    assert await statute.unresolved(db, "依保險法第999條第1項") == [("保險法", "art.999.1")]
    assert await statute.unresolved(db, "依保險法施行細則第64條第2項") == [("保險法施行細則", "art.64.2")]
    assert await statute.unresolved(db, "依保險法第64條第2項") == []


def test_a_scenario_module_imports_cleanly_from_any_entry_point():
    # The cycle this guards against was entry-point dependent: importing through
    # `scenario` worked and importing `scenarios.soothe` directly raised ImportError on a
    # name that was plainly there. A test inside an already-imported process cannot see
    # that, so each entry point gets a cold interpreter.
    import subprocess
    import sys

    for entry in (
        "policydesk.agent.scenarios.soothe",
        "policydesk.agent.scenario",
        "policydesk.agent.scenario_base",
        "policydesk.agent.executor",
    ):
        done = subprocess.run(  # noqa: S603  (argv is built here, from a literal tuple)
            [sys.executable, "-c", f"import {entry}"], capture_output=True, text=True, check=False
        )
        assert done.returncode == 0, f"{entry}: {done.stderr.strip().splitlines()[-1:]}"


def test_the_scenarios_package_pulls_in_nothing():
    # `scenario_base` broke the cycle, so this package needs no import to order it. Adding
    # one would rebuild the cycle in a quieter place: every scenario module would drag in
    # the catalogue, and a scenario wanting only `Scenario` would load all the others.
    # Read as source, not as attributes: importing a submodule binds its name on the
    # package, so `vars()` reports `soothe` whether or not this file imported anything.

    from policydesk.agent import scenarios as package

    body = [
        line
        for line in inspect.getsource(package).splitlines()
        if line.startswith(("import ", "from ")) and "__future__" not in line
    ]
    assert not body, body


@pytest.fixture
async def case(db):
    """Open a case to run a real turn against."""
    member_id = await db.fetch_val("SELECT member_id FROM member ORDER BY member_id LIMIT 1")
    if member_id is None:
        pytest.skip("no member to run a turn against")
    return await db.fetch_val(
        'INSERT INTO "case" (member_id, kind, stage) VALUES ($1::bigint, $2, $3) RETURNING case_id',
        [member_id, "service", "inquiry"],
    ), member_id


class _Says:
    """
    A provider that routes to `soothe`, then writes one fixed reply as its answer.

    The router calls a tool on every turn now. A stub that only returns text lands on
    `out_of_scope` and never reaches this route's statute check, which is what these
    tests are for.
    """

    name = "stub"

    def __init__(self, text: str) -> None:
        self.text = text

    async def complete(self, **kwargs):
        from msgspec import json

        from policydesk.llm.provider import Completion

        if kwargs.get("tools"):
            return Completion(text="", model="stub", provider="stub",
                              tool_calls=[{"name": "soothe", "arguments": "{}"}])
        return Completion(
            text=json.encode({"reply": self.text, "citations": [], "calculations": []}).decode(),
            model="stub", provider="stub",
        )


async def test_an_invented_provision_is_withheld_on_the_soothe_route(db, case):
    # The general case is covered elsewhere; this one pins the route that emits statute
    # citations *by design*. The checker began in this module, correct and tested and
    # never called — so what is asserted here is that the reply is withheld, not that the
    # checker returns the right list.
    from policydesk.agent.executor import WITHHELD, run_turn

    case_id, member_id = case
    invented = "我聽到您的不滿。依〔保險法 第999條第2項〕，保險人不得解除契約。"
    turn = await run_turn(
        _Says(invented), db, case_id=case_id, member_id=member_id,
        text="你們憑什麼說要解除我的契約", confirmed=False,
    )
    assert turn.reply == WITHHELD
    assert "第999條" not in turn.reply


async def test_a_real_provision_survives_the_soothe_route(db, case):
    # The other direction, and the one that fails if the checker is too strict: a
    # withholding gate that also swallows correct citations answers nobody.
    from policydesk.agent.executor import WITHHELD, run_turn

    case_id, member_id = case
    real = "我聽到您的不滿。依〔保險法 第64條第3項〕，解除權經過二年即不得行使。"
    turn = await run_turn(
        _Says(real), db, case_id=case_id, member_id=member_id,
        text="你們憑什麼說要解除我的契約", confirmed=False,
    )
    assert turn.reply != WITHHELD
    assert "第64條第3項" in turn.reply


@pytest.mark.asyncio
async def test_a_real_provision_survives_however_the_model_words_the_sentence(db):
    """
    The checker voided ten of fourteen realistic phrasings of a correct citation.

    `_STATUTE_NAME` has no left boundary, so any CJK character abutting the name is captured
    with it and 另依保險法 matches no row. Before the executor ran this on every reply it
    only saw soothe's own hardcoded string, where the name always stood alone — so widening
    where the check runs is what turned a latent weakness into replies being withheld.
    """
    from policydesk.agent import statute

    real = [
        "依保險法第64條第2項辦理。", "另依保險法第64條第2項辦理。", "並依保險法第64條第2項辦理。",
        "這在保險法第64條第2項有規定。", "法源是保險法第64條第2項。", "請參考保險法第64條第2項。",
        "本公司依保險法第64條第2項處理。", "台灣的保險法第64條第2項。", "適用保險法第64條第2項。",
        "〔保險法 第64條第2項〕", "改依保險法施行細則第4條。", "另依金融消費者保護法第13條第2項。",
    ]
    voided = [t for t in real if await statute.unresolved(db, t)]
    assert not voided, f"a correct citation was reported as invented: {voided}"

    # And the two the check exists for still fail: an article nobody wrote, and the right
    # article number attributed to the wrong statute.
    assert await statute.unresolved(db, "依保險法第999條第1項")
    assert await statute.unresolved(db, "依保險法施行細則第64條第2項")
