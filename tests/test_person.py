"""
保險年齡 is the rule worth testing hardest.

Every contract in the corpus states it identically — 以足歲計算，但未滿一歲的零數超過
六個月者，加算一歲 — and that one extra year moves both eligibility and premium, so an
off-by-one here is an off-by-one in what the applicant is quoted.
"""

from datetime import date

from policydesk.gov.identity import checksum_ok
from policydesk.synthetic.person import OccupationClass, generate


def test_generate_is_stable_for_the_same_name():
    today = date(2026, 8, 29)
    assert generate("王小明", 0, today=today) == generate("王小明", 0, today=today)


def test_generate_normalises_surrounding_whitespace():
    today = date(2026, 8, 29)
    padded = generate("  王小明  ", 0, today=today)
    plain = generate("王小明", 0, today=today)
    assert padded.birth_date == plain.birth_date
    assert padded.occupation == plain.occupation


def test_generate_differs_between_names():
    today = date(2026, 8, 29)
    a = generate("陳大文", 0, today=today)
    b = generate("林美華", 1, today=today)
    assert (a.birth_date, a.occupation, a.address.city) != (b.birth_date, b.occupation, b.address.city)


def test_generate_mints_a_valid_national_id_by_default():
    person = generate("測試甲", 3)
    assert checksum_ok(person.national_id)
    assert person.national_id[1] == person.sex.digit


def test_generate_can_mint_an_invalid_id_on_request():
    """Without one of these, the identity mock's refusal path is dead code on stage."""
    person = generate("測試乙", 4, valid_id=False)
    assert not checksum_ok(person.national_id)
    assert len(person.national_id) == 10, "still well-formed, so the refusal is about the checksum"


def test_generate_age_is_within_the_demo_range():
    today = date(2026, 8, 29)
    ages = {generate(f"人{i}", i, today=today).age_on(today) for i in range(60)}
    assert min(ages) >= 18
    assert max(ages) <= 85


def test_insurance_age_matches_plain_age_just_after_a_birthday():
    person = generate("甲", 0, today=date(2026, 8, 29))
    on = date(2026, 9, 1)
    born = person.birth_date.replace(year=1990, month=8, day=20)
    aged = type(person)(**{**_fields(person), "birth_date": born})
    assert aged.age_on(on) == 36
    assert aged.insurance_age_on(on) == 36


def test_insurance_age_rounds_up_past_six_months():
    """36 years and 7 months is 37 for insurance purposes."""
    person = generate("乙", 0, today=date(2026, 8, 29))
    born = date(1990, 1, 20)
    aged = type(person)(**{**_fields(person), "birth_date": born})
    on = date(2026, 8, 29)
    assert aged.age_on(on) == 36
    assert aged.insurance_age_on(on) == 37


def test_insurance_age_does_not_round_up_at_exactly_six_months():
    """The contract says 超過六個月, so six months to the day stays put."""
    person = generate("丙", 0, today=date(2026, 8, 29))
    aged = type(person)(**{**_fields(person), "birth_date": date(1990, 2, 28)})
    on = date(2026, 8, 28)
    assert aged.age_on(on) == 36
    assert aged.insurance_age_on(on) == 36


def test_uninsurable_occupations_are_generated_not_hidden():
    """拒保 is a fact on the record, so the refusal is computed rather than flagged."""
    today = date(2026, 8, 29)
    classes = {generate(f"職{i}", i, today=today).occupation_class for i in range(400)}
    assert OccupationClass.UNINSURABLE in classes


def test_addresses_only_name_districts_of_their_city():
    from policydesk.synthetic.person import _ADDRESSES

    today = date(2026, 8, 29)
    for i in range(120):
        addr = generate(f"址{i}", i, today=today).address
        assert addr.district in _ADDRESSES[addr.city]
        assert addr.road in _ADDRESSES[addr.city][addr.district]


def test_address_str_omits_the_levels_it_does_not_have():
    today = date(2026, 8, 29)
    rendered = {str(generate(f"格{i}", i, today=today).address) for i in range(80)}
    assert any("巷" not in a for a in rendered), "an address that always has every level reads as synthetic"
    assert all(a.count("號") == 1 for a in rendered)


def _fields(person) -> dict:
    """Copy a frozen Person's fields so a test can vary one of them."""
    return {f: getattr(person, f) for f in person.__struct_fields__}
