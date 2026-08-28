"""
National ID numbers, and the mock that verifies them.

A mock that always returns success is a lie told on stage. This one implements the
real checksum, so it rejects a malformed number for the reason the real service would,
and a judge who types their own ID number gets the answer they expect. What is mocked
is the government service call — not the rule the government applies.

Format: one letter plus nine digits. The letter encodes the issuing county, digit one
encodes sex (1 male, 2 female), and the last digit is a checksum over the rest. That
is why the generator cannot simply count upwards from A000000001: digit one would be
0, which is not a sex, and the checksum would be wrong for every number in the run.
"""

from enum import StrEnum
from itertools import count

from msgspec import Struct

# The letter's numeric value, by issuing county. I and O sit out of sequence because
# they were assigned later, which is why this is a table and not arithmetic.
_LETTER = {
    "A": 10, "B": 11, "C": 12, "D": 13, "E": 14, "F": 15, "G": 16, "H": 17,
    "I": 34, "J": 18, "K": 19, "L": 20, "M": 21, "N": 22, "O": 35, "P": 23,
    "Q": 24, "R": 25, "S": 26, "T": 27, "U": 28, "V": 29, "W": 32, "X": 30,
    "Y": 31, "Z": 33,
}  # fmt: skip

# Positional weights: the letter contributes its tens digit at weight 1 and its units
# digit at weight 9, then the nine digits descend 8..1 with the checksum at weight 1.
_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 1, 1)


class Sex(StrEnum):
    """Encoded in the first digit of the ID."""

    MALE = "male"
    FEMALE = "female"

    @property
    def digit(self) -> str:
        """The digit this sex occupies in position one."""
        return "1" if self is Sex.MALE else "2"


def checksum_ok(national_id: str) -> bool:
    """
    Say whether an ID number satisfies the national checksum.

    Args:
        national_id: A candidate number, e.g. "A123456789".

    Returns:
        True when the number is structurally valid. This says nothing about whether
        such a person exists — that is what the government service answers, and what
        this module mocks.

    """
    if len(national_id) != 10:
        return False
    letter, digits = national_id[0].upper(), national_id[1:]
    if letter not in _LETTER or not digits.isdigit():
        return False
    if digits[0] not in ("1", "2"):
        return False

    tens, units = divmod(_LETTER[letter], 10)
    total = tens + units * 9 + sum(int(d) * w for d, w in zip(digits, _WEIGHTS, strict=True))
    return total % 10 == 0


def complete(prefix: str) -> str:
    """
    Append the checksum digit that makes a nine-character stem valid.

    Args:
        prefix: A letter plus eight digits, e.g. "A12345678".

    Returns:
        The full ten-character ID number.

    Raises:
        ValueError: The stem is not a letter plus eight digits, or its first digit is
            not a sex.

    """
    if len(prefix) != 9 or prefix[0].upper() not in _LETTER or not prefix[1:].isdigit():
        msg = f"{prefix!r} is not a letter followed by eight digits"
        raise ValueError(msg)
    if prefix[1] not in ("1", "2"):
        msg = f"{prefix!r} has {prefix[1]!r} where the sex digit belongs (1 male, 2 female)"
        raise ValueError(msg)

    letter, digits = prefix[0].upper(), prefix[1:]
    tens, units = divmod(_LETTER[letter], 10)
    total = tens + units * 9 + sum(int(d) * w for d, w in zip(digits, _WEIGHTS[:8], strict=True))
    return f"{letter}{digits}{(10 - total % 10) % 10}"


def issue(sex: Sex, serial: int, letter: str = "A") -> str:
    """
    Mint the next demo ID number for a given sex.

    Args:
        sex: Decides the first digit.
        serial: A counter, zero upwards, occupying the seven digits before the checksum.
        letter: Issuing county letter.

    Returns:
        A checksum-valid ID number, e.g. issue(Sex.FEMALE, 0) -> "A200000000" plus its
        checksum digit.

    Raises:
        ValueError: The serial does not fit in seven digits.

    """
    if not 0 <= serial <= 9_999_999:
        msg = f"serial {serial} does not fit in seven digits"
        raise ValueError(msg)
    return complete(f"{letter}{sex.digit}{serial:07d}")


class Verification(Struct, frozen=True):
    """
    What the identity service answers.

    Modelled on TW FidO, which returns whether verification succeeded and nothing
    about the person — no name, no biometric, no ID number echoed back. A mock that
    returned richer data than the real service would teach the rest of the system to
    depend on fields that will not exist.
    """

    verified: bool
    reason: str = ""


def verify(national_id: str, *, known: frozenset[str] | None = None) -> Verification:
    """
    Mock the government identity check.

    Args:
        national_id: The number to check.
        known: The set of numbers this demo has issued. When given, a well-formed but
            unissued number is refused, which is what lets the demo show a rejection
            that is not merely a typo.

    Returns:
        The service's answer, with a reason on refusal so the UI can say why.

    """
    if not checksum_ok(national_id):
        return Verification(verified=False, reason="身分證字號格式或檢查碼不符")
    if known is not None and national_id not in known:
        return Verification(verified=False, reason="查無此身分證字號之驗證紀錄")
    return Verification(verified=True)


def serials(sex: Sex, letter: str = "A"):
    """
    Yield demo ID numbers for one sex, in order.

    Args:
        sex: Decides the first digit.
        letter: Issuing county letter.

    Yields:
        Checksum-valid ID numbers.

    """
    for n in count():
        yield issue(sex, n, letter)
