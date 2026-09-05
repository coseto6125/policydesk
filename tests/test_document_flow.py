"""Document command lifecycle; SHA values here identify versions, not verified bytes."""

from types import SimpleNamespace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from msgspec import json

from conftest import connected_database
from policydesk.agent import tools
from policydesk.core import commands
from policydesk.core.models import Stage
from policydesk.synthetic.person import generate
from policydesk.synthetic.portfolio import enrol
from policydesk.web import server
from policydesk.web.session import Registry

if TYPE_CHECKING:
    from policydesk.core.db import Database


def test_commands_document_kind_keeps_its_public_import():
    assert commands.DocumentKind.APPLICATION.value == "要保書"


async def _corpus_counts(db: Database) -> dict:
    return await db.fetch_one(
        """SELECT (SELECT count(*) FROM policy) AS policies,
                  (SELECT count(*) FROM clause) AS clauses,
                  (SELECT count(*) FROM contract_clause) AS contract_clauses"""
    )


@pytest.fixture
async def document_cases():
    names = [f"doc-{uuid4().hex}" for _ in range(2)]
    async with connected_database() as db:
        before = await _corpus_counts(db)
        cases = []
        try:
            product_id = await db.fetch_val(
                "SELECT product_id FROM product WHERE document_kind = 'contract' ORDER BY product_id LIMIT 1"
            )
            assert product_id is not None, "document lifecycle requires an existing contract product"
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
            assert await _corpus_counts(db) == before, "document tests must not change policy or clause counts"


async def _state(db: Database, case_id: int) -> dict:
    return {
        "case": await db.fetch_one('SELECT stage, case_version FROM "case" WHERE case_id = $1::bigint', [case_id]),
        "documents": await db.fetch(
            "SELECT document_id, sha, signed_at, uploaded_name FROM case_document WHERE case_id = $1::bigint ORDER BY document_id",
            [case_id],
        ),
        "grants": await db.fetch(
            "SELECT grant_id, scope, document_sha FROM authorization_grant WHERE case_id = $1::bigint ORDER BY grant_id",
            [case_id],
        ),
        "audit_events": await db.fetch_val("SELECT count(*) FROM audit_event WHERE case_id = $1::bigint", [case_id]),
    }


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
async def test_customer_socket_upload_illegal_stage_reports_refusal_without_writes(document_cases, stage):
    db, (case, _) = document_cases
    await db.execute('UPDATE "case" SET stage = $2::text WHERE case_id = $1::bigint', [case["case_id"], stage.value])
    member = await db.fetch_one("SELECT display_name, national_id FROM member WHERE member_id = $1::bigint", [case["member_id"]])
    frames, before = [], {}

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
