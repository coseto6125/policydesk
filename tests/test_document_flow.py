"""
Document lifecycle: state tests prove workflow correctness, not truthful model prose.

Assert state transitions and persisted data changes, never reply keyword matches.
Evaluate whether replies faithfully explain those states separately by meaning.
SHA values here identify versions, not verified bytes.
"""

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from msgspec import json

from conftest import connected_database
from policydesk.agent import tools
from policydesk.core import commands
from policydesk.core import db as database_module
from policydesk.core.models import Stage
from policydesk.synthetic.person import generate
from policydesk.synthetic.portfolio import enrol
from policydesk.web import server
from policydesk.web.session import Registry

if TYPE_CHECKING:
    from policydesk.core.db import Database


def test_commands_document_kind_keeps_its_public_import():
    assert commands.DocumentKind.APPLICATION.value == "要保書"


async def _corpus_counts(db: Database, names: list[str], product_id: str) -> dict:
    """
    Count what the document flow must leave alone: these members' policies and this product's clauses.

    Scoped to the fixture's own members and product, not the whole tables. Test modules
    run in parallel, and other modules enrol members and load scratch products of their
    own; a global count moves under this fixture without the document flow having touched
    anything.
    """
    return await db.fetch_one(
        """SELECT (SELECT count(*) FROM policy p JOIN member m USING (member_id)
                   WHERE m.display_name = ANY($1::text[])) AS policies,
                  (SELECT count(*) FROM clause WHERE product_id = $2::text) AS clauses,
                  (SELECT count(*) FROM contract_clause WHERE product_id = $2::text) AS contract_clauses""",
        [names, product_id],
    )


@pytest.fixture
async def document_cases():
    names = [f"doc-{uuid4().hex}" for _ in range(2)]
    async with connected_database() as db:
        product_id = await db.fetch_val(
            "SELECT product_id FROM product WHERE document_kind = 'contract' ORDER BY product_id LIMIT 1"
        )
        if product_id is None:
            # The catalogue is built from the insurer's contract PDFs, which do not
            # travel with the repository. With no product there is no document
            # lifecycle to exercise, and a fixture that fails here reports a broken
            # build where what it found was an empty database.
            pytest.skip("no contract product to run a document lifecycle against")
        before = await _corpus_counts(db, names, product_id)
        cases = []
        try:
            for name in names:
                person = generate(name, int(uuid4().hex, 16) % 10_000_000)
                assert not await db.fetch_val(
                    "SELECT EXISTS(SELECT 1 FROM member WHERE national_id = $1::text)", [person.national_id]
                ), "fixture identity must be unique"
                member_id, written = await enrol(person, db, preset="none")
                assert written == 0
                case = await commands.open_case(db, member_id)
                proposal = await commands.propose(
                    db, case.case_id, product_ids=[product_id], adviser="fixture adviser", licence="fixture licence",
                )
                assert isinstance(proposal, commands.Applied)
                issued = await commands.issue_documents(db, case.case_id)
                assert isinstance(issued, commands.Applied)
                assert issued.stage is Stage.ISSUED
                documents = await db.fetch(
                    "SELECT document_id, kind, sha FROM case_document WHERE case_id = $1::bigint ORDER BY document_id",
                    [case.case_id],
                )
                assert len(documents) == len(commands.ENROLMENT_DOCUMENTS) > 0
                assert {row["kind"] for row in documents} == {kind.value for kind in commands.ENROLMENT_DOCUMENTS}
                assert all(row["sha"] for row in documents)
                cases.append({"case_id": case.case_id, "member_id": member_id, "documents": documents})
            yield db, cases
        finally:
            member_ids = [row["member_id"] for row in await db.fetch(
                "SELECT member_id FROM member WHERE display_name = ANY($1::text[])", [names],
            )]
            case_ids = [row["case_id"] for row in await db.fetch(
                'SELECT case_id FROM "case" WHERE member_id = ANY($1::bigint[])', [member_ids],
            )]
            await db.execute("DELETE FROM llm_usage WHERE case_id = ANY($1::bigint[])", [case_ids])
            await db.execute("DELETE FROM member WHERE member_id = ANY($1::bigint[])", [member_ids])
            remaining = await db.fetch_one(
                """SELECT (SELECT count(*) FROM member WHERE display_name = ANY($1::text[])) AS members,
                          (SELECT count(*) FROM "case" WHERE case_id = ANY($2::bigint[])) AS cases,
                          (SELECT count(*) FROM case_document WHERE case_id = ANY($2::bigint[])) AS documents,
                          (SELECT count(*) FROM authorization_grant WHERE case_id = ANY($2::bigint[])) AS grants,
                          (SELECT count(*) FROM identity_check WHERE case_id = ANY($2::bigint[])) AS identity_checks,
                          (SELECT count(*) FROM audit_event WHERE case_id = ANY($2::bigint[])) AS audit_events,
                          (SELECT count(*) FROM llm_usage WHERE case_id = ANY($2::bigint[])) AS usage""",
                [names, case_ids],
            )
            assert remaining == dict.fromkeys(remaining, 0), f"document fixtures survived cleanup: {remaining}"
            assert await _corpus_counts(db, names, product_id) == before, "document tests must not change policy or clause counts"


async def _state(db: Database, case_id: int) -> dict:
    return {
        "case": await db.fetch_one(
            'SELECT stage, case_version, adviser_name, adviser_licence, decided_by, decision_reason FROM "case" WHERE case_id = $1::bigint',
            [case_id],
        ),
        "documents": await db.fetch(
            "SELECT document_id, sha, signed_at, uploaded_name FROM case_document WHERE case_id = $1::bigint ORDER BY document_id",
            [case_id],
        ),
        "grants": await db.fetch(
            "SELECT grant_id, scope, document_sha FROM authorization_grant WHERE case_id = $1::bigint ORDER BY grant_id",
            [case_id],
        ),
        "audit_events": await db.fetch_val("SELECT count(*) FROM audit_event WHERE case_id = $1::bigint", [case_id]),
        "identity_checks": await db.fetch(
            "SELECT verified, reason FROM identity_check WHERE case_id = $1::bigint ORDER BY check_id", [case_id],
        ),
    }


async def test_demonstrate_documents_preserves_progress_and_only_completes_own_case(document_cases):
    db, (case, other) = document_cases
    other_before = await _state(db, other["case_id"])
    first = case["documents"][0]
    await commands.upload_document(db, case["case_id"], document_id=first["document_id"], sample="matching")
    existing = (await _state(db, case["case_id"]))["documents"][0]

    missing = await commands.demonstrate_documents(db, case["case_id"], mode="missing")
    assert isinstance(missing, commands.Refusal)
    assert len(missing.missing) == 1
    partial = await _state(db, case["case_id"])
    assert partial["case"]["stage"] == Stage.ISSUED.value
    assert partial["documents"][0] == existing
    assert sum(row["signed_at"] is None for row in partial["documents"]) == 1
    assert sum(row["signed_at"] is not None for row in partial["documents"]) > 0

    repeated = await commands.demonstrate_documents(db, case["case_id"], mode="missing")
    assert isinstance(repeated, commands.Refusal)
    assert repeated.missing == missing.missing
    wrong = await commands.demonstrate_documents(db, case["case_id"], mode="wrong")
    assert isinstance(wrong, commands.Refusal)
    assert not wrong.missing
    assert await _state(db, case["case_id"]) == partial

    complete = await commands.demonstrate_documents(db, case["case_id"], mode="complete")
    assert isinstance(complete, commands.Applied)
    assert complete.stage is Stage.REVIEW
    final = await _state(db, case["case_id"])
    assert final["case"]["case_version"] == partial["case"]["case_version"] + 3
    assert all(row["signed_at"] is not None for row in final["documents"])
    assert len(final["identity_checks"]) == 1
    assert final["identity_checks"][0]["verified"] is True
    assert final["case"]["decided_by"] is None
    assert final["case"]["decision_reason"] == partial["case"]["decision_reason"]
    assert await db.fetch_val(
        "SELECT bool_and(provider = 'mock') FROM authorization_grant WHERE case_id = $1::bigint AND stage = 'signed'",
        [case["case_id"]],
    )
    assert await _state(db, other["case_id"]) == other_before


@pytest.mark.parametrize("mode", [None, "", "unknown", [], {}, True, 1])
async def test_demonstrate_documents_invalid_mode_writes_nothing(document_cases, mode):
    db, (case, _) = document_cases
    before = await _state(db, case["case_id"])
    assert isinstance(await commands.demonstrate_documents(db, case["case_id"], mode=mode), commands.Refusal)
    assert await _state(db, case["case_id"]) == before


@pytest.mark.parametrize(("mode", "stage"), [
    (mode, stage) for mode in ("missing", "wrong", "complete") for stage in Stage
    if stage is not Stage.ISSUED
    and not (mode == "complete" and stage in (Stage.SIGNED, Stage.VERIFIED, Stage.REVIEW))
])
async def test_demonstrate_documents_illegal_stage_writes_nothing(document_cases, mode, stage):
    db, (case, _) = document_cases
    await db.execute('UPDATE "case" SET stage = $2::text WHERE case_id = $1::bigint', [case["case_id"], stage.value])
    before = await _state(db, case["case_id"])
    assert isinstance(await commands.demonstrate_documents(db, case["case_id"], mode=mode), commands.Refusal)
    assert await _state(db, case["case_id"]) == before


@pytest.mark.parametrize("defect", ["unissued", "missing_version"])
async def test_demonstrate_documents_incomplete_issue_refuses_before_any_write(document_cases, defect):
    db, (case, _) = document_cases
    last = case["documents"][-1]
    if defect == "unissued":
        await db.execute("DELETE FROM case_document WHERE document_id = $1::bigint", [last["document_id"]])
    else:
        await db.execute("UPDATE case_document SET sha = '' WHERE document_id = $1::bigint", [last["document_id"]])
    before = await _state(db, case["case_id"])
    result = await commands.demonstrate_documents(db, case["case_id"], mode="complete")
    assert isinstance(result, commands.Refusal)
    assert not result.missing
    assert await _state(db, case["case_id"]) == before


async def test_demonstrate_documents_concurrent_complete_records_one_set(document_cases):
    db, (case, _) = document_cases
    before = await _state(db, case["case_id"])
    outcomes = await asyncio.gather(*(
        commands.demonstrate_documents(db, case["case_id"], mode="complete") for _ in range(2)
    ))
    assert all(isinstance(outcome, commands.Applied) and outcome.stage is Stage.REVIEW for outcome in outcomes)
    final = await _state(db, case["case_id"])
    assert final["case"]["stage"] == Stage.REVIEW.value
    assert final["case"]["case_version"] == before["case"]["case_version"] + 3
    assert final["audit_events"] == before["audit_events"] + 3
    assert len(final["identity_checks"]) == 1
    assert len(final["grants"]) == len(case["documents"]) * len(commands.SIGNING_PARTIES)


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("mode", ["missing", "complete"])
async def test_demonstrate_documents_later_document_failure_rolls_back_whole_batch(document_cases, monkeypatch, mode, cancel):
    db, (case, _) = document_cases
    before = await _state(db, case["case_id"])
    original = commands._record_signatures
    calls = 0

    async def fail_after_second_document(*args, **kwargs):
        nonlocal calls
        result = await original(*args, **kwargs)
        calls += 1
        if calls == 2:
            if cancel:
                raise asyncio.CancelledError
            raise RuntimeError("injected after second document")
        return result

    monkeypatch.setattr(commands, "_record_signatures", fail_after_second_document)
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await commands.demonstrate_documents(db, case["case_id"], mode=mode)
    assert calls == 2
    assert await _state(db, case["case_id"]) == before


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("failed_stage", [Stage.VERIFIED, Stage.REVIEW])
@pytest.mark.parametrize("single_file", [False, True])
async def test_document_demo_later_step_failure_rolls_back_all_records(document_cases, monkeypatch, cancel, failed_stage, single_file):
    db, (case, _) = document_cases
    if single_file:
        for document in case["documents"][:-1]:
            await commands.upload_document(db, case["case_id"], document_id=document["document_id"], sample="matching")
    before = await _state(db, case["case_id"])
    original = commands._bump

    async def fail_after_step(session, case_id, stage, *args):
        result = await original(session, case_id, stage, *args)
        if stage is failed_stage:
            if cancel:
                raise asyncio.CancelledError
            raise RuntimeError("injected after demo step")
        return result

    monkeypatch.setattr(commands, "_bump", fail_after_step)
    operation = (
        commands.upload_document(
            db, case["case_id"], document_id=case["documents"][-1]["document_id"], sample="matching", advance_demo=True,
        ) if single_file else commands.demonstrate_documents(db, case["case_id"], mode="complete")
    )
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await operation
    assert await _state(db, case["case_id"]) == before


@pytest.mark.parametrize("failed_step", ["verification", "submission"])
async def test_document_demo_refused_step_preserves_actual_progress_and_retries(document_cases, monkeypatch, failed_step):
    db, (case, _) = document_cases
    if failed_step == "verification":
        monkeypatch.setattr(commands, "verify_demo_identity", lambda national_id: SimpleNamespace(verified=False, reason="fixture rejection"))
    else:
        await db.execute('UPDATE "case" SET adviser_licence = $2::text WHERE case_id = $1::bigint', [case["case_id"], ""])
    refused = await commands.demonstrate_documents(db, case["case_id"], mode="complete")
    assert isinstance(refused, commands.Refusal)
    partial = await _state(db, case["case_id"])
    assert partial["case"]["stage"] == (Stage.SIGNED.value if failed_step == "verification" else Stage.VERIFIED.value)
    assert all(document["signed_at"] is not None for document in partial["documents"])
    assert len(partial["identity_checks"]) == 1
    assert partial["identity_checks"][0]["verified"] is (failed_step == "submission")
    if failed_step == "verification":
        monkeypatch.undo()
    else:
        await db.execute('UPDATE "case" SET adviser_licence = $2::text WHERE case_id = $1::bigint', [case["case_id"], "fixture licence"])
    completed = await commands.demonstrate_documents(db, case["case_id"], mode="complete")
    assert isinstance(completed, commands.Applied)
    assert completed.stage is Stage.REVIEW
    final = await _state(db, case["case_id"])
    assert final["documents"] == partial["documents"]
    assert final["grants"] == partial["grants"]
    assert len(final["identity_checks"]) == (2 if failed_step == "verification" else 1)
    repeated = await commands.demonstrate_documents(db, case["case_id"], mode="complete")
    assert repeated == completed
    assert await _state(db, case["case_id"]) == final


async def test_upload_document_final_sample_auto_advance_reaches_review(document_cases):
    db, (case, other) = document_cases
    other_before = await _state(db, other["case_id"])
    for index, document in enumerate(case["documents"], 1):
        outcome = await commands.upload_document(
            db, case["case_id"], document_id=document["document_id"], sample="matching", advance_demo=True,
        )
        if index < len(case["documents"]):
            assert isinstance(outcome, commands.Refusal)
            assert len(outcome.missing) == len(case["documents"]) - index
            assert (await _state(db, case["case_id"]))["identity_checks"] == []
    assert isinstance(outcome, commands.Applied)
    assert outcome.stage is Stage.REVIEW
    assert await _state(db, other["case_id"]) == other_before


async def _sign_all(db: Database, case: dict) -> commands.Applied:
    for document in case["documents"]:
        for party in commands.SIGNING_PARTIES:
            result = await commands.record_signature(
                db, case["case_id"], document_id=document["document_id"], party=party, document_sha=document["sha"],
            )
    assert isinstance(result, commands.Applied)
    assert result.stage is Stage.SIGNED
    return result


async def _verify(db: Database, case: dict) -> commands.Applied:
    national_id = await db.fetch_val("SELECT national_id FROM member WHERE member_id = $1::bigint", [case["member_id"]])
    result = await commands.verify_identity(
        db, case["case_id"], national_id=national_id, verified=True, reason="fixture provider", latency_ms=0,
    )
    assert isinstance(result, commands.Applied)
    assert result.stage is Stage.VERIFIED
    return result


async def test_document_commands_matching_versions_require_both_parties_and_allow_review(document_cases):
    db, (case, _) = document_cases
    for document in case["documents"]:
        result = await commands.record_signature(
            db, case["case_id"], document_id=document["document_id"],
            party=commands.SIGNING_PARTIES[0], document_sha=document["sha"],
        )
        assert isinstance(result, commands.Refusal)
        assert result.missing
    partial = await _state(db, case["case_id"])
    assert partial["case"]["stage"] == Stage.ISSUED.value
    assert len(partial["grants"]) == len(case["documents"])
    assert all(document["signed_at"] is None for document in partial["documents"])
    for position, document in enumerate(case["documents"]):
        result = await commands.record_signature(
            db, case["case_id"], document_id=document["document_id"],
            party=commands.SIGNING_PARTIES[1], document_sha=document["sha"],
        )
        if position < len(case["documents"]) - 1:
            assert isinstance(result, commands.Refusal)
            assert result.missing
        else:
            assert isinstance(result, commands.Applied)
            assert result.stage is Stage.SIGNED
    signed = await _state(db, case["case_id"])
    assert len(signed["grants"]) == len(case["documents"]) * len(commands.SIGNING_PARTIES)
    assert all(document["signed_at"] is not None for document in signed["documents"])
    snapshot = await commands.snapshot(db, case["case_id"])
    assert snapshot["document_status"] == {
        "signed": len(case["documents"]), "total": len(case["documents"]), "pending": 0,
        "unissued": (), "missing": (),
    }
    await _verify(db, case)
    submitted = await commands.submit_for_review(db, case["case_id"])
    assert isinstance(submitted, commands.Applied)
    assert submitted.stage is Stage.REVIEW


async def test_record_signature_wrong_version_refuses_without_mutating_records(document_cases):
    db, (case, _) = document_cases
    document = case["documents"][0]
    before = await _state(db, case["case_id"])
    result = await commands.record_signature(
        db, case["case_id"], document_id=document["document_id"],
        party=commands.SIGNING_PARTIES[0], document_sha=f"{document['sha']}-wrong-version",
    )
    assert isinstance(result, commands.Refusal)
    assert await _state(db, case["case_id"]) == before


async def test_signing_documents_mock_marker_tracks_current_document_and_scope(document_cases):
    db, (case, other) = document_cases
    document = case["documents"][0]

    async def current_document():
        rows = await commands.signing_documents(db, case["case_id"])
        return next(row for row in rows if row["document_id"] == document["document_id"])

    assert (await current_document())["signature_simulated"] is False
    await commands.record_signature(
        db, case["case_id"], document_id=document["document_id"], party="要保人", document_sha=document["sha"],
    )
    partial = await current_document()
    assert partial["signature_simulated"] is True
    assert partial["signed_at"] is None
    await db.execute(
        "UPDATE case_document SET sha = $2::text WHERE document_id = $1::bigint",
        [document["document_id"], f"{document['sha']}-new"],
    )
    assert (await current_document())["signature_simulated"] is False
    await db.execute_many(
        """INSERT INTO authorization_grant (case_id, stage, scope, document_sha, provider)
           VALUES ($1::bigint, 'signed', $2::text, $3::text, 'mock')""",
        [
            (other["case_id"], f"要保人 簽署文件 {document['document_id']}", f"{document['sha']}-new"),
            (case["case_id"], "unrelated permission", f"{document['sha']}-new"),
        ],
    )
    assert (await current_document())["signature_simulated"] is False
    await commands.upload_document(db, case["case_id"], document_id=document["document_id"], filename="demo.pdf")
    complete = await current_document()
    assert complete["signature_simulated"] is True
    assert complete["signed_at"] is not None
    providers = await db.fetch(
        """SELECT provider FROM authorization_grant
           WHERE case_id = $1::bigint AND document_sha = $2::text AND scope <> 'unrelated permission'""",
        [case["case_id"], f"{document['sha']}-new"],
    )
    assert providers == [{"provider": "mock"}, {"provider": "mock"}]
    await db.execute(
        "UPDATE authorization_grant SET provider = 'external-test' WHERE case_id = $1::bigint", [case["case_id"]],
    )
    assert (await current_document())["signature_simulated"] is False


async def test_record_signature_split_versions_do_not_complete_a_document(document_cases):
    db, (case, _) = document_cases
    document = case["documents"][0]
    first = await commands.record_signature(
        db, case["case_id"], document_id=document["document_id"],
        party=commands.SIGNING_PARTIES[0], document_sha=document["sha"],
    )
    assert isinstance(first, commands.Refusal)
    assert first.missing
    current_sha = f"{document['sha']}-new-version"
    await db.execute("UPDATE case_document SET sha = $2::text WHERE document_id = $1::bigint", [document["document_id"], current_sha])
    second = await commands.record_signature(
        db, case["case_id"], document_id=document["document_id"],
        party=commands.SIGNING_PARTIES[1], document_sha=current_sha,
    )
    assert isinstance(second, commands.Refusal)
    assert second.missing
    signed_at = await db.fetch_val("SELECT signed_at FROM case_document WHERE document_id = $1::bigint", [document["document_id"]])
    assert signed_at is None, "different document versions must not combine into two signatures"


async def test_submit_for_review_changed_version_refuses_despite_signed_timestamp(document_cases):
    db, (case, _) = document_cases
    await _sign_all(db, case)
    await _verify(db, case)
    document = case["documents"][0]
    await db.execute(
        "UPDATE case_document SET sha = $2::text WHERE document_id = $1::bigint",
        [document["document_id"], f"{document['sha']}-amended"],
    )
    before = await _state(db, case["case_id"])
    assert all(row["signed_at"] is not None for row in before["documents"])
    result = await commands.submit_for_review(db, case["case_id"])
    assert isinstance(result, commands.Refusal), f"stale signatures advanced the case: {result}"
    assert await _state(db, case["case_id"]) == before
    snapshot = await commands.snapshot(db, case["case_id"])
    current = next(row for row in snapshot["documents"] if row["document_id"] == document["document_id"])
    assert current["signed_at"] is None
    assert current["signed_parties"] == 0
    assert snapshot["document_status"]["pending"] == 1
    assert snapshot["document_status"]["signed"] == len(case["documents"]) - 1
    pending = await tools.pending_signatures(db, case["case_id"])
    assert pending["count"] == 1
    assert pending["names"].strip() == document["kind"]


@pytest.mark.parametrize("remove_all", [False, True])
async def test_submit_for_review_missing_required_document_refuses_without_mutation(document_cases, remove_all):
    db, (case, _) = document_cases
    await _sign_all(db, case)
    await _verify(db, case)
    removed = case["documents"] if remove_all else case["documents"][:1]
    await db.execute("DELETE FROM case_document WHERE document_id = ANY($1::bigint[])",
                     [[row["document_id"] for row in removed]])
    before = await _state(db, case["case_id"])
    result = await commands.submit_for_review(db, case["case_id"])
    assert isinstance(result, commands.Refusal)
    assert set(result.missing) == {row["kind"] for row in removed}
    assert await _state(db, case["case_id"]) == before
    snapshot = await commands.snapshot(db, case["case_id"])
    status = snapshot["document_status"]
    assert status["total"] == len(case["documents"])
    assert status["signed"] == len(case["documents"]) - len(removed)
    assert status["pending"] == len(removed)
    assert set(status["unissued"]) == set(result.missing)
    assert set(status["missing"]) == set(result.missing)
    pending = await tools.pending_signatures(db, case["case_id"])
    assert pending["count"] == len(removed)


@pytest.mark.parametrize("defect", ["duplicate_role", "wrong_role", "wrong_stage", "wrong_case", "wrong_document", "empty_sha", "null_sha"])
async def test_snapshot_invalid_grants_never_make_a_timestamp_current(document_cases, defect):
    db, (case, other) = document_cases
    document = case["documents"][0]
    for index, party in enumerate(commands.SIGNING_PARTIES):
        grant_case, grant_stage, grant_document = case["case_id"], "signed", document["document_id"]
        if index == 1:
            match defect:
                case "duplicate_role":
                    party = commands.SIGNING_PARTIES[0]
                case "wrong_role":
                    party = "業務員"
                case "wrong_stage":
                    grant_stage = "issued"
                case "wrong_case":
                    grant_case = other["case_id"]
                case "wrong_document":
                    grant_document = other["documents"][0]["document_id"]
        await db.execute(
            """INSERT INTO authorization_grant (case_id, stage, scope, document_sha)
               VALUES ($1::bigint, $2::text, $3::text, $4::text)""",
            [grant_case, grant_stage, f"{party} 簽署文件 {grant_document}", "" if defect == "empty_sha" else document["sha"]],
        )
    current_sha = None if defect == "null_sha" else "" if defect == "empty_sha" else document["sha"]
    await db.execute(
        "UPDATE case_document SET signed_at = now(), sha = $2::text WHERE document_id = $1::bigint",
        [document["document_id"], current_sha],
    )
    before = await _state(db, case["case_id"])
    snapshot = await commands.snapshot(db, case["case_id"])
    current = next(row for row in snapshot["documents"] if row["document_id"] == document["document_id"])
    assert current["signed_at"] is None
    assert snapshot["document_status"]["signed"] == 0
    assert snapshot["document_status"]["pending"] == len(case["documents"])
    assert await _state(db, case["case_id"]) == before


@pytest.mark.parametrize("stage", [Stage.PROPOSED, Stage.VERIFIED])
async def test_record_signature_illegal_stage_refuses_without_mutating_records(document_cases, stage):
    db, (case, _) = document_cases
    document = case["documents"][0]
    await db.execute('UPDATE "case" SET stage = $2::text WHERE case_id = $1::bigint', [case["case_id"], stage.value])
    before = await _state(db, case["case_id"])
    result = await commands.record_signature(
        db, case["case_id"], document_id=document["document_id"],
        party=commands.SIGNING_PARTIES[0], document_sha=document["sha"],
    )
    assert isinstance(result, commands.Refusal)
    assert await _state(db, case["case_id"]) == before


async def test_record_signature_other_cases_document_refuses_without_mutating_either_case(document_cases):
    db, (owner, other) = document_cases
    document = owner["documents"][0]
    before = [await _state(db, case["case_id"]) for case in (owner, other)]
    result = await commands.record_signature(
        db, other["case_id"], document_id=document["document_id"],
        party=commands.SIGNING_PARTIES[0], document_sha=document["sha"],
    )
    assert isinstance(result, commands.Refusal)
    assert [await _state(db, case["case_id"]) for case in (owner, other)] == before


@pytest.mark.parametrize("stage", [Stage.PROPOSED, Stage.VERIFIED])
async def test_customer_socket_upload_illegal_stage_reports_refusal_without_writes(document_cases, stage, monkeypatch):
    db, (case, _) = document_cases
    await db.execute('UPDATE "case" SET stage = $2::text WHERE case_id = $1::bigint', [case["case_id"], stage.value])
    member = await db.fetch_one("SELECT display_name, national_id FROM member WHERE member_id = $1::bigint", [case["member_id"]])
    frames, before = [], {}
    guidance = AsyncMock()
    monkeypatch.setattr(server, "_answer", guidance)

    class Socket:
        async def __aiter__(self):
            yield json.encode({"type": "hello", "name": member["display_name"]}).decode()
            yield json.encode({"type": "say", "text": member["national_id"]}).decode()
            before.update(await _state(db, case["case_id"]))
            yield json.encode({"type": "upload", "document_id": case["documents"][0]["document_id"], "filename": "signed.pdf"}).decode()

        async def send(self, message):
            frames.append(json.decode(message))

        async def close(self):
            pass

    request = SimpleNamespace(app=SimpleNamespace(ctx=SimpleNamespace(db=db, registry=Registry(), desk_sockets=set())))
    await server.customer_socket(request, Socket())
    assert any(frame["type"] == "confirmed" for frame in frames)
    assert before, "the upload must follow actual identity confirmation"
    assert await _state(db, case["case_id"]) == before, "a refused upload changed document metadata or signatures"
    assert any(frame["type"] == "notice" and frame["level"] == "warn" and frame["text"] for frame in frames)
    assert guidance.await_count == 2
    assert guidance.call_args.kwargs["document_event"] is True
    assert guidance.call_args.kwargs["case_id"] == case["case_id"]
    assert guidance.call_args.kwargs["text"]


async def test_pending_signatures_progress_uses_actual_current_documents(document_cases):
    db, (case, _) = document_cases
    initial = await tools.pending_signatures(db, case["case_id"])
    assert initial["stage"] == "issued"
    assert initial["simulation_only"] is True
    assert initial["signing_parties"] == commands.SIGNING_PARTIES
    assert initial["identity_verified"] is False
    assert initial["submitted_for_review"] is False
    assert initial["signed"] == 0
    assert initial["count"] == len(case["documents"])
    assert {doc["title"] for doc in initial["documents"]} == set(initial["missing"])
    for index, document in enumerate(case["documents"], 1):
        await commands.upload_document(db, case["case_id"], document_id=document["document_id"], sample="matching")
        current = await tools.pending_signatures(db, case["case_id"])
        assert current["stage"] == ("signed" if index == len(case["documents"]) else "issued")
        assert current["signed"] == index
        assert current["count"] == len(case["documents"]) - index
        assert document["kind"] not in current["missing"]
        assert sum(doc["signature_simulated"] for doc in current["documents"]) == index
    assert current["stage"] == "signed"
    assert current["missing"] == ()


async def test_document_commands_require_each_guard_when_auto_advance_is_not_requested(document_cases):
    """Progress queries cannot advance a case; commands must satisfy each state gate."""
    db, (case, other) = document_cases
    other_before = await _state(db, other["case_id"])

    async def read_only_progress(stage):
        before = await _state(db, case["case_id"])
        progress = await tools.pending_signatures(db, case["case_id"])
        assert progress["stage"] == stage
        assert progress["identity_verified"] is (stage in ("verified", "review"))
        assert progress["submitted_for_review"] is (stage == "review")
        assert await _state(db, case["case_id"]) == before
        return progress

    await read_only_progress("issued")
    initial = await _state(db, case["case_id"])
    refused = await commands.submit_for_review(db, case["case_id"])
    assert isinstance(refused, commands.Refusal)
    assert await _state(db, case["case_id"]) == initial
    for document in case["documents"]:
        await commands.upload_document(
            db, case["case_id"], document_id=document["document_id"], sample="matching",
        )
    complete = await read_only_progress("signed")
    assert complete["signed"] == complete["total"] == len(case["documents"])
    assert not complete["missing"]
    signed = await _state(db, case["case_id"])
    assert not signed["identity_checks"]
    assert len(signed["grants"]) == len(case["documents"]) * len(commands.SIGNING_PARTIES)
    refused = await commands.submit_for_review(db, case["case_id"])
    assert isinstance(refused, commands.Refusal)
    assert await _state(db, case["case_id"]) == signed

    await _verify(db, case)
    await read_only_progress("verified")
    verified = await _state(db, case["case_id"])
    assert verified["identity_checks"] == [{"verified": True, "reason": "fixture provider"}]
    assert verified["documents"] == signed["documents"]
    assert verified["grants"] == signed["grants"]
    submitted = await commands.submit_for_review(db, case["case_id"])
    assert isinstance(submitted, commands.Applied)
    await read_only_progress("review")
    reviewed = await _state(db, case["case_id"])
    assert reviewed["case"]["case_version"] == verified["case"]["case_version"] + 1
    assert reviewed["audit_events"] == verified["audit_events"] + 1
    assert reviewed["documents"] == verified["documents"]
    assert reviewed["grants"] == verified["grants"]
    assert await _state(db, other["case_id"]) == other_before


@pytest.mark.parametrize("sample", ["mismatched", "unknown", True, []])
async def test_upload_document_wrong_or_invalid_sample_keeps_all_records(document_cases, sample):
    db, (case, _) = document_cases
    before = await _state(db, case["case_id"])
    outcome = await commands.upload_document(
        db, case["case_id"], document_id=case["documents"][0]["document_id"], sample=sample,
    )
    assert isinstance(outcome, commands.Refusal)
    assert not outcome.missing  # A rejection, not persisted partial progress.
    assert await _state(db, case["case_id"]) == before


async def test_upload_document_matching_sample_records_only_owned_mock_document(document_cases):
    db, (case, other) = document_cases
    before = await _state(db, other["case_id"])
    document = case["documents"][0]
    outcome = await commands.upload_document(db, case["case_id"], document_id=document["document_id"], sample="matching")
    assert isinstance(outcome, commands.Refusal)
    assert outcome.missing
    row = await db.fetch_one("SELECT uploaded_name FROM case_document WHERE document_id = $1::bigint", [document["document_id"]])
    assert row["uploaded_name"] == f"示範-{document['kind']}.pdf"
    progress = await tools.pending_signatures(db, case["case_id"])
    assert progress["signed"] == 1
    assert progress["documents"][0]["signature_simulated"] is True
    own_before = await _state(db, case["case_id"])
    wrong_after_correct = await commands.upload_document(db, case["case_id"], document_id=document["document_id"], sample="mismatched")
    assert isinstance(wrong_after_correct, commands.Refusal)
    assert not wrong_after_correct.missing
    assert await _state(db, case["case_id"]) == own_before
    assert document["kind"] not in (await tools.pending_signatures(db, case["case_id"]))["missing"]
    rejected = await commands.upload_document(db, case["case_id"], document_id=other["documents"][0]["document_id"], sample="matching")
    assert isinstance(rejected, commands.Refusal)
    assert not rejected.missing
    assert await _state(db, other["case_id"]) == before


async def test_upload_document_partial_set_commits_metadata_and_both_roles(document_cases):
    db, (case, _) = document_cases
    document = case["documents"][0]
    outcome = await commands.upload_document(
        db, case["case_id"], document_id=document["document_id"], filename="signed.pdf",
    )
    assert isinstance(outcome, commands.Refusal)
    assert outcome.missing
    state = await _state(db, case["case_id"])
    assert state["documents"][0]["uploaded_name"] == "signed.pdf"
    assert state["documents"][0]["signed_at"] is not None
    assert {row["scope"] for row in state["grants"]} == {
        f"{party} 簽署文件 {document['document_id']}" for party in commands.SIGNING_PARTIES
    }
    assert state["case"]["stage"] == Stage.ISSUED.value


@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("operation", ["record_signature", "upload_document"])
async def test_document_command_failure_after_stage_write_rolls_back_every_record(document_cases, monkeypatch, cancel, operation):
    db, (case, _) = document_cases
    last = case["documents"][-1]
    for document in case["documents"]:
        for party in commands.SIGNING_PARTIES:
            if document == last and party == commands.SIGNING_PARTIES[-1]:
                continue
            await commands.record_signature(
                db, case["case_id"], document_id=document["document_id"], party=party, document_sha=document["sha"],
            )
    before = await _state(db, case["case_id"])
    original = commands._bump

    async def fail_after_write(*args, **kwargs):
        await original(*args, **kwargs)
        if cancel:
            raise asyncio.CancelledError
        raise RuntimeError("injected after stage and audit writes")

    monkeypatch.setattr(commands, "_bump", fail_after_write)
    arguments = (
        {"party": commands.SIGNING_PARTIES[-1], "document_sha": last["sha"]}
        if operation == "record_signature" else {"filename": "signed.pdf"}
    )
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await getattr(commands, operation)(db, case["case_id"], document_id=last["document_id"], **arguments)
    assert await _state(db, case["case_id"]) == before


async def test_issue_documents_concurrent_requests_issue_only_one_set(document_cases):
    db, (case, _) = document_cases
    await db.execute("DELETE FROM case_document WHERE case_id = $1::bigint", [case["case_id"]])
    await db.execute('UPDATE "case" SET stage = $2::text WHERE case_id = $1::bigint', [case["case_id"], Stage.PROPOSED.value])
    outcomes = await asyncio.gather(*(commands.issue_documents(db, case["case_id"]) for _ in range(2)))
    assert sum(isinstance(outcome, commands.Applied) for outcome in outcomes) == 1
    state = await _state(db, case["case_id"])
    assert len(state["documents"]) == len(commands.ENROLMENT_DOCUMENTS)


@pytest.mark.parametrize("cancel", [False, True])
async def test_upload_document_second_role_failure_rolls_back_filename_and_first_role(document_cases, monkeypatch, cancel):
    db, (case, _) = document_cases
    before = await _state(db, case["case_id"])
    original = database_module.TransactionSession.execute_many

    async def fail_after_first_role(session, sql, rows):
        await original(session, sql, rows[:1])
        if cancel:
            raise asyncio.CancelledError
        raise RuntimeError("injected between roles")

    monkeypatch.setattr(database_module.TransactionSession, "execute_many", fail_after_first_role)
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await commands.upload_document(
            db, case["case_id"], document_id=case["documents"][0]["document_id"], filename="signed.pdf",
        )
    assert await _state(db, case["case_id"]) == before


async def test_upload_document_last_document_with_one_role_advances_only_once(document_cases):
    db, (case, _) = document_cases
    last = case["documents"][-1]
    for document in case["documents"]:
        for party in commands.SIGNING_PARTIES:
            if document == last and party == commands.SIGNING_PARTIES[0]:
                continue
            await commands.record_signature(
                db, case["case_id"], document_id=document["document_id"], party=party, document_sha=document["sha"],
            )
    before = await _state(db, case["case_id"])
    outcome = await commands.upload_document(db, case["case_id"], document_id=last["document_id"], filename="signed.pdf")
    assert isinstance(outcome, commands.Applied)
    assert outcome.stage is Stage.SIGNED
    after = await _state(db, case["case_id"])
    assert after["case"]["case_version"] == before["case"]["case_version"] + 1
    assert after["audit_events"] == before["audit_events"] + 1
    refused = await commands.upload_document(db, case["case_id"], document_id=last["document_id"], filename="replaced.pdf")
    assert isinstance(refused, commands.Refusal)
    assert not refused.missing
    assert await _state(db, case["case_id"]) == after


async def test_verify_identity_failed_check_commits_without_advancing(document_cases):
    db, (case, _) = document_cases
    await _sign_all(db, case)
    before = await _state(db, case["case_id"])
    result = await commands.verify_identity(
        db, case["case_id"], national_id="invalid", verified=False, reason="fixture refusal", latency_ms=0,
    )
    assert isinstance(result, commands.Refusal)
    assert await _state(db, case["case_id"]) == {
        **before, "identity_checks": [{"verified": False, "reason": "fixture refusal"}],
    }


async def _wait_for_blocked_connection(db, blocker_pid, task):
    """Observe a real PostgreSQL lock wait, not a scheduling delay or mocked query."""
    async with asyncio.timeout(5):
        while not await db.fetch_val(
            "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE $1::int = ANY(pg_blocking_pids(pid)))",
            [blocker_pid],
        ):
            assert not task.done(), "command completed without waiting for the case lock"
            await asyncio.sleep(0.01)


@pytest.mark.parametrize(("operation", "stage"), [
    ("propose", Stage.INQUIRY), ("issue_documents", Stage.PROPOSED),
    ("record_signature", Stage.ISSUED), ("upload_document", Stage.ISSUED),
    ("verify_identity", Stage.SIGNED), ("submit_for_review", Stage.VERIFIED),
    ("decide", Stage.REVIEW), ("snapshot", Stage.REVIEW),
])
async def test_case_commands_wait_for_other_connection_and_read_committed_stage(document_cases, operation, stage):
    db, (case, _) = document_cases
    await _sign_all(db, case)
    await _verify(db, case)
    await db.execute('UPDATE "case" SET stage = $2::text WHERE case_id = $1::bigint', [case["case_id"], stage.value])
    document = case["documents"][0]
    national_id = await db.fetch_val("SELECT national_id FROM member WHERE member_id = $1::bigint", [case["member_id"]])
    arguments = {
        "propose": {"product_ids": ["fixture"], "adviser": "fixture", "licence": "fixture"},
        "issue_documents": {},
        "record_signature": {"document_id": document["document_id"], "document_sha": document["sha"], "party": "要保人"},
        "upload_document": {"document_id": document["document_id"], "filename": "signed.pdf"},
        "verify_identity": {"national_id": national_id, "verified": True, "reason": "fixture", "latency_ms": 0},
        "submit_for_review": {}, "decide": {"approved": False, "reason": "fixture", "by": "fixture"}, "snapshot": {},
    }
    task = None
    async with connected_database() as other:
        try:
            async with db.transaction() as holder:
                await holder.fetch_one('SELECT case_id FROM "case" WHERE case_id = $1::bigint FOR UPDATE', [case["case_id"]])
                pid = await holder.fetch_val("SELECT pg_backend_pid()")
                task = asyncio.create_task(getattr(commands, operation)(other, case["case_id"], **arguments[operation]))
                await _wait_for_blocked_connection(db, pid, task)
                await holder.execute('UPDATE "case" SET stage = $2::text WHERE case_id = $1::bigint', [case["case_id"], Stage.APPROVED.value])
                expected = await _state(holder, case["case_id"])
            result = await asyncio.wait_for(task, timeout=5)
            if operation == "snapshot":
                assert result["stage"] == Stage.APPROVED.value
                assert result["case_version"] == expected["case"]["case_version"]
            else:
                assert isinstance(result, commands.Refusal)
            assert await _state(db, case["case_id"]) == expected
        finally:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def test_open_case_two_connections_resume_same_new_case(document_cases):
    db, (case, _) = document_cases
    tasks = []
    async with connected_database() as other:
        try:
            async with db.transaction() as holder:
                await holder.fetch_one("SELECT member_id FROM member WHERE member_id = $1::bigint FOR UPDATE", [case["member_id"]])
                pid = await holder.fetch_val("SELECT pg_backend_pid()")
                tasks = [asyncio.create_task(commands.open_case(pool, case["member_id"], "service")) for pool in (db, other)]
                await _wait_for_blocked_connection(db, pid, tasks[0])
            first, second = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
            assert first.case_id == second.case_id
            assert await db.fetch_val(
                'SELECT count(*) FROM "case" WHERE member_id = $1::bigint AND kind = $2::text', [case["member_id"], "service"],
            ) == 1
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("filename", ["", "  ", "x" * 256])
async def test_upload_document_invalid_filename_refuses_without_writes(document_cases, filename):
    db, (case, _) = document_cases
    before = await _state(db, case["case_id"])
    outcome = await commands.upload_document(db, case["case_id"], document_id=case["documents"][0]["document_id"], filename=filename)
    assert isinstance(outcome, commands.Refusal)
    assert not outcome.missing
    assert await _state(db, case["case_id"]) == before


async def test_upload_document_other_case_refuses_without_mutating_either_case(document_cases):
    db, (case, other) = document_cases
    before = [await _state(db, item["case_id"]) for item in (case, other)]
    result = await commands.upload_document(db, case["case_id"], document_id=other["documents"][0]["document_id"], filename="signed.pdf")
    assert isinstance(result, commands.Refusal)
    assert not result.missing
    assert [await _state(db, item["case_id"]) for item in (case, other)] == before


@pytest.mark.parametrize("phase", ["begin", "rollback", "commit"])
async def test_transaction_repeated_cancellation_settles_control_before_pool_reuse(document_cases, monkeypatch, phase):
    db, (case, _) = document_cases
    before = await _state(db, case["case_id"])
    reached = asyncio.Event()
    release = asyncio.Event()
    wrote = asyncio.Event()
    original = database_module._settle
    calls = 0
    pid = None

    async def controlled_settle(awaitable):
        nonlocal calls
        calls += 1
        pause = calls == (1 if phase == "begin" else 2)

        async def delayed_control():
            if pause:
                reached.set()
                await release.wait()
            return await awaitable

        return await original(delayed_control())

    async def change():
        nonlocal pid
        async with db.transaction() as session:
            pid = await session.fetch_val("SELECT pg_backend_pid()")
            await session.execute(
                "UPDATE case_document SET uploaded_name = $2::text WHERE document_id = $1::bigint",
                [case["documents"][0]["document_id"], "transaction-probe.pdf"],
            )
            wrote.set()
            if phase == "rollback":
                await asyncio.Event().wait()

    monkeypatch.setattr(database_module, "_settle", controlled_settle)
    task = asyncio.create_task(change())
    try:
        async with asyncio.timeout(5):
            if phase == "rollback":
                await wrote.wait()
                task.cancel()
            await reached.wait()
            for _ in range(2):
                task.cancel()
                await asyncio.sleep(0)
            assert not task.done(), "transaction control was abandoned on cancellation"
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        after = await _state(db, case["case_id"])
        if phase == "commit":
            assert after["documents"][0]["uploaded_name"] == "transaction-probe.pdf"
        else:
            assert after == before
        if pid is not None:
            assert not await db.fetch_val(
                "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE pid = $1::int AND state = 'idle in transaction')", [pid],
            )
        assert await db.fetch_val("SELECT 1") == 1
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("operation", ["upload", "verify"])
async def test_pending_signatures_waits_for_writer_and_reads_one_state(document_cases, monkeypatch, operation):
    db, (case, _) = document_cases
    for document in case["documents"][:-1]:
        await commands.upload_document(db, case["case_id"], document_id=document["document_id"], sample="matching")
    if operation == "verify":
        await commands.upload_document(
            db, case["case_id"], document_id=case["documents"][-1]["document_id"], sample="matching",
        )
    reached, release = asyncio.Event(), asyncio.Event()
    original = commands._bump
    pid = None

    async def pause_before_stage(session, *args, **kwargs):
        nonlocal pid
        pid = await session.fetch_val("SELECT pg_backend_pid()")
        reached.set()
        await release.wait()
        return await original(session, *args, **kwargs)

    monkeypatch.setattr(commands, "_bump", pause_before_stage)
    tasks = []
    async with connected_database() as reader:
        try:
            writing = commands.upload_document(
                db, case["case_id"], document_id=case["documents"][-1]["document_id"], sample="matching",
            ) if operation == "upload" else _verify(db, case)
            tasks.append(asyncio.create_task(writing))
            await asyncio.wait_for(reached.wait(), timeout=5)
            tasks.append(asyncio.create_task(tools.pending_signatures(reader, case["case_id"])))
            await _wait_for_blocked_connection(db, pid, tasks[1])
            release.set()
            _, progress = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
            assert progress["stage"] == ("signed" if operation == "upload" else "verified")
            assert progress["identity_verified"] is (operation == "verify")
            assert progress["submitted_for_review"] is False
            assert progress["signed"] == 10
            assert progress["pending"] == 0
            assert progress["missing"] == ()
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("operation", ["issue_documents", "upload_document", "decide"])
async def test_case_commands_two_connection_race_commits_one_change(document_cases, monkeypatch, operation):
    db, (case, _) = document_cases
    arguments = [{}, {}]
    match operation:
        case "issue_documents":
            await db.execute("DELETE FROM case_document WHERE case_id = $1::bigint", [case["case_id"]])
            await db.execute('UPDATE "case" SET stage = $2::text WHERE case_id = $1::bigint', [case["case_id"], Stage.PROPOSED.value])
            expected_stage = Stage.ISSUED
        case "upload_document":
            for document in case["documents"][:-1]:
                await commands.upload_document(db, case["case_id"], document_id=document["document_id"], filename="signed.pdf")
            arguments = [
                {"document_id": case["documents"][-1]["document_id"], "filename": filename}
                for filename in ("winner.pdf", "loser.pdf")
            ]
            expected_stage = Stage.SIGNED
        case "decide":
            await _sign_all(db, case)
            await _verify(db, case)
            await commands.submit_for_review(db, case["case_id"])
            arguments = [{"approved": approved, "reason": "fixture decision", "by": "fixture"} for approved in (True, False)]
            expected_stage = Stage.APPROVED
    before = await _state(db, case["case_id"])
    reached = asyncio.Event()
    release = asyncio.Event()
    original = commands._bump
    pid = None

    async def hold_before_stage_write(session, *args, **kwargs):
        nonlocal pid
        pid = await session.fetch_val("SELECT pg_backend_pid()")
        reached.set()
        await release.wait()
        return await original(session, *args, **kwargs)

    monkeypatch.setattr(commands, "_bump", hold_before_stage_write)
    tasks = []
    async with connected_database() as other:
        try:
            tasks.append(asyncio.create_task(getattr(commands, operation)(db, case["case_id"], **arguments[0])))
            await asyncio.wait_for(reached.wait(), timeout=5)
            tasks.append(asyncio.create_task(getattr(commands, operation)(other, case["case_id"], **arguments[1])))
            await _wait_for_blocked_connection(db, pid, tasks[1])
            release.set()
            winner, loser = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
            assert isinstance(winner, commands.Applied)
            assert winner.stage is expected_stage
            assert isinstance(loser, commands.Refusal)
            assert not loser.missing
            after = await _state(db, case["case_id"])
            assert after["case"]["case_version"] == before["case"]["case_version"] + 1
            assert after["audit_events"] == before["audit_events"] + 1
            assert len(after["documents"]) == len(commands.ENROLMENT_DOCUMENTS)
            if operation == "upload_document":
                assert after["documents"][-1]["uploaded_name"] == "winner.pdf"
                assert len(after["grants"]) == len(before["grants"]) + len(commands.SIGNING_PARTIES)
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
