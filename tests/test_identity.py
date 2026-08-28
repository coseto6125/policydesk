"""
The checksum is what makes the identity mock honest, so it gets real cases.

A123456789 is the canonical valid example: A=10 contributes 1 and 0*9, then the digits
1..9 at weights 8,7,6,5,4,3,2,1,1 sum with it to 130, which is divisible by ten.
"""

import pytest

from policydesk.gov.identity import Sex, checksum_ok, complete, issue, serials, verify


def test_checksum_ok_known_valid_number_passes():
    assert checksum_ok("A123456789")


def test_checksum_ok_rejects_wrong_check_digit():
    """Same number, last digit off by one."""
    assert not checksum_ok("A123456788")


def test_checksum_ok_rejects_zero_in_the_sex_digit():
    """The originally-specified series A000000001 fails here, which is why it changed."""
    assert not checksum_ok("A000000001")


@pytest.mark.parametrize("bad", ["", "A12345678", "A1234567890", "123456789A", "?123456789", "A12345678X"])
def test_checksum_ok_rejects_malformed_shapes(bad: str):
    assert not checksum_ok(bad)


def test_complete_appends_the_digit_that_validates():
    assert checksum_ok(complete("A12345678"))


def test_complete_rejects_a_stem_with_no_sex_digit():
    with pytest.raises(ValueError, match="sex digit"):
        complete("A02345678")


def test_issue_produces_valid_numbers_for_both_sexes():
    for sex in (Sex.MALE, Sex.FEMALE):
        national_id = issue(sex, 0)
        assert checksum_ok(national_id)
        assert national_id[1] == sex.digit


def test_issue_serial_is_reflected_in_the_number():
    assert issue(Sex.FEMALE, 42).startswith("A2000004")


def test_issue_rejects_a_serial_that_does_not_fit():
    with pytest.raises(ValueError, match="seven digits"):
        issue(Sex.MALE, 10_000_000)


def test_serials_yields_distinct_valid_numbers():
    got = [n for n, _ in zip(serials(Sex.MALE), range(50), strict=False)]
    assert len(set(got)) == 50
    assert all(checksum_ok(n) for n in got)


def test_verify_refuses_a_malformed_number_with_a_reason():
    result = verify("A000000001")
    assert not result.verified
    assert "檢查碼" in result.reason


def test_verify_accepts_a_well_formed_number_when_no_allowlist_is_given():
    assert verify("A123456789").verified


def test_verify_refuses_a_valid_but_unissued_number():
    """The rejection the demo needs: correct format, no record behind it."""
    issued = frozenset({issue(Sex.MALE, 0)})
    result = verify("A123456789", known=issued)
    assert not result.verified
    assert "查無" in result.reason


def test_verify_accepts_an_issued_number():
    minted = issue(Sex.FEMALE, 7)
    assert verify(minted, known=frozenset({minted})).verified
