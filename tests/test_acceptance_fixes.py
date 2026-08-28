"""
Regressions for what an acceptance run found by refusing to cooperate.

Each of these was reachable from outside before it was fixed, and none of them was
reachable from the happy path — which is why they only appeared once someone drove the
system in the wrong order and with the wrong values.
"""

import pytest
from msgspec import DecodeError, json


def test_no_rows_recogniser_matches_psqlpy_empty_result():
    """Psqlpy raises here rather than returning None, and the message is the only tell."""
    from policydesk.core.db import _no_rows

    assert _no_rows(RuntimeError("query returned an unexpected number of rows"))


def test_no_rows_recogniser_ignores_other_failures():
    from policydesk.core.db import _no_rows

    assert not _no_rows(RuntimeError("connection refused"))


def test_no_rows_is_not_treated_as_a_transport_failure():
    """It was being retried twice before surfacing, because the text reads like a fault."""
    from policydesk.core.db import _is_transport_failure

    assert not _is_transport_failure(RuntimeError("query returned an unexpected number of rows"))


@pytest.mark.parametrize("bad", ["{not json", "", "[]", '"a string"', "null", "123"])
def test_decode_returns_none_for_anything_that_is_not_an_object(bad: str):
    """A malformed frame closed the whole socket before this; one bad client ended a good session."""
    from policydesk.web.server import _decode

    assert _decode(bad) is None


def test_decode_accepts_an_object():
    from policydesk.web.server import _decode

    assert _decode('{"type":"say","text":"hi"}') == {"type": "say", "text": "hi"}


def test_signing_party_must_be_a_contract_party():
    from policydesk.core.commands import SIGNING_PARTIES

    assert "業務員" not in SIGNING_PARTIES
    assert set(SIGNING_PARTIES) == {"要保人", "被保險人"}


def test_name_limit_is_short_enough_to_render():
    """An acceptance run created a case whose customer name was several hundred characters."""
    from policydesk.web.server import MAX_NAME

    assert 0 < MAX_NAME <= 64


def test_desk_token_exists_and_is_not_empty():
    """The desk queue names every customer, so it cannot be open to whoever reaches the port."""
    from policydesk.web.server import DESK_TOKEN

    assert DESK_TOKEN


def test_desk_token_is_not_a_value_that_ships_in_the_repo():
    """A default in the source is a password everyone already has."""
    import os

    from policydesk.web.server import DESK_TOKEN

    if os.environ.get("POLICYDESK_DESK_TOKEN"):
        pytest.skip("token is configured, so the generated-value rule does not apply")
    assert DESK_TOKEN != "desk-demo-token"  # noqa: S105  - asserting a value is refused, not setting one
    assert len(DESK_TOKEN) >= 16, "a per-boot token must be long enough to resist guessing"


def test_json_decode_error_is_the_type_the_server_catches():
    """Guards the import: catching the wrong exception type puts the bug straight back."""
    with pytest.raises(DecodeError):
        json.decode(b"{not json")


def test_document_route_requires_the_same_token_as_the_desk():
    """
    /doc/<id> renders an applicant's national ID, birth date and address.

    document_id is a sequential bigserial, so an unguarded route lets a loop over the
    integers walk every member's personal data — which a code review demonstrated by
    curling /doc/1 and reading 陳大文's ID off the page.
    """
    from pathlib import Path

    source = Path("src/policydesk/web/server.py").read_text()
    doc_route = source[source.index('@app.get("/doc/'):source.index('@app.get("/health")')]
    assert "DESK_TOKEN" in doc_route, "the document route must be gated like the desk socket"
    assert "403" in doc_route


def test_both_signing_parties_are_required():
    """要保人 and 被保險人 must each sign personally, or the contract may be void."""
    from policydesk.core.commands import SIGNING_PARTIES

    assert len(SIGNING_PARTIES) == 2


def test_identity_check_is_gated_before_it_is_recorded():
    """
    A verified row on a case still at INQUIRY satisfied submit_for_review forever.

    submit_for_review reads bool_or(verified), so a check taken before any document
    existed permanently cleared the identity leg of the completeness test. The stage
    gate has to run before the insert, not after it.
    """
    from pathlib import Path

    source = Path("src/policydesk/core/commands.py").read_text()
    body = source[source.index("async def verify_identity"):source.index("async def submit_for_review")]
    gate = body.index("may_advance")
    insert = body.index("INSERT INTO identity_check")
    assert gate < insert, "the stage gate must precede the insert"


def test_identity_check_compares_against_the_case_owner():
    """A well-formed number is not the case owner's number."""
    from pathlib import Path

    source = Path("src/policydesk/core/commands.py").read_text()
    body = source[source.index("async def verify_identity"):source.index("async def submit_for_review")]
    assert "member_national_id" in body, "the check must compare against the case's member"


def test_calculator_is_offered_to_the_model_not_merely_described():
    """
    The scenario text tells the model 金額由計算工具產生; the tool has to be reachable.

    A review found `calculate` had zero callers outside its own tests: the answering
    call passed no `tools=`, so the model wrote figures into prose from the material it
    had been handed and nothing checked them. A tool the model cannot reach is a claim.
    """
    from pathlib import Path

    source = Path("src/policydesk/agent/executor.py").read_text()
    assert "TOOL_SCHEMA" in source
    assert "tools=[TOOL_SCHEMA]" in source


def test_surgery_multipliers_are_reachable_from_a_scenario():
    """17,866 rows extracted from 附表1 were queried by nothing."""
    from policydesk.agent import tools
    from policydesk.agent.scenario import CLAIM_CHECKLIST

    assert hasattr(tools, "find_multiplier")
    assert "find_multiplier" in CLAIM_CHECKLIST.tools


def test_unverifiable_reply_is_withheld_rather_than_annotated():
    """
    Appending a caveat still put the invented clause number in front of the customer.

    tools.py claims a fabricated citation "fails a lookup rather than reaching a
    customer". That is only true if the text carrying it is withheld.
    """
    from pathlib import Path

    source = Path("src/policydesk/agent/executor.py").read_text()
    block = source[source.index("if not checked.trustworthy:"):source.index("    turn.reply = completion.text\n    return turn")]
    assert "completion.text" not in block, "the unverifiable text must not be forwarded"
    assert "轉由專人" in block


def test_the_model_session_is_reused_and_closed():
    """A session per call is a TLS handshake per call on the customer's own turn."""
    from policydesk.llm.provider import OpenAIProvider

    assert hasattr(OpenAIProvider, "close")
    assert hasattr(OpenAIProvider, "_open_session")
