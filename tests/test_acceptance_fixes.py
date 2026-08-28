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


def test_json_decode_error_is_the_type_the_server_catches():
    """Guards the import: catching the wrong exception type puts the bug straight back."""
    with pytest.raises(DecodeError):
        json.decode(b"{not json")
