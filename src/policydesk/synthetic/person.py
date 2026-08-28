"""
Generate an insurance applicant who could exist.

Two rules shape everything here.

**The generator stores facts, never verdicts.** There is no `qualifies` field and no
`should_be_rejected` flag. A demo user fails underwriting because their occupation is
class 6, or because a policy's effective date is later than the event they are
claiming for — facts a caseworker can read off the screen and check. A refusal that
comes from a hidden boolean is a refusal nobody can audit, and on stage it reads as
staged, which is worse than not showing a refusal at all.

**The seed is stable.** The same display name produces the same person on every
restart, so a rehearsal and the live run tell the same story, and a case reopened
tomorrow still belongs to the same applicant. The name is normalised first, so
"王小明" and " 王小明 " are one person.

Fields divide into two kinds, and the division matters more than the list. Some change
the insurance outcome: 保險年齡 decides eligibility and premium, 職業等級 decides
acceptance and loading, 既往症 drives underwriting questions. The rest — 婚姻狀況,
地址, 電子郵件 — are the texture that makes a record look real. Outcome fields are
generated together so they stay consistent with each other; an applicant whose
occupation cannot buy the product the demo needs is a broken demo, not a realistic one.
"""

from datetime import UTC, date, datetime
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING

from msgspec import Struct

from policydesk.gov.identity import Sex, issue
from policydesk.synthetic.seed import rng_for

if TYPE_CHECKING:
    import random

# Same string, same person, across restarts and across rehearsals. Change this and
# every demo user changes with it.
_SEED_SALT = "policydesk-demo-v1"


class OccupationClass(IntEnum):
    """
    Taiwan's occupational risk classes for personal accident cover.

    Classes 1-6 are accepted with increasing loading; class 6 is the ceiling that most
    products still write. Beyond it lies 拒保 — high-voltage line work, nuclear waste
    handling, commercial diving, special forces — which is not a seventh class but a
    refusal, and is therefore its own member here rather than a number.
    """

    CLERICAL = 1
    """收銀店員、行政文書、一般醫護、律師、會計師、教師、學生、設計師、家庭主婦。"""
    LIGHT_MANUAL = 2
    """農夫、導遊、攝影記者、工廠作業員、清潔人員、郵差、大樓管理員、廚師、攤商。"""
    SKILLED_MANUAL = 3
    """汽機車維修人員、一般貨運司機。"""
    HEAVY_MANUAL = 4
    """計程車司機、沿海養殖工人、泳池救生員、交通警察。"""
    HAZARDOUS = 5
    """高處作業、重機械操作。多數醫療險仍承保，意外險加費顯著。"""
    HIGH_HAZARD = 6
    """遠洋漁業、爆破作業。部分商品不承保。"""
    UNINSURABLE = 7
    """拒保職業：高壓電力工程、核廢料處理、潛水人員、特種軍人。"""


_OCCUPATIONS: dict[OccupationClass, tuple[str, ...]] = {
    OccupationClass.CLERICAL: ("行政人員", "會計師", "小學教師", "平面設計師", "家庭主婦", "門市店員", "護理師"),
    OccupationClass.LIGHT_MANUAL: ("郵務士", "大樓管理員", "廚師", "工廠作業員", "清潔人員", "農民"),
    OccupationClass.SKILLED_MANUAL: ("汽車修護技師", "水電技工", "貨車司機"),
    OccupationClass.HEAVY_MANUAL: ("計程車司機", "游泳池救生員", "交通警察", "沿海養殖工"),
    OccupationClass.HAZARDOUS: ("外牆清洗人員", "起重機操作員"),
    OccupationClass.HIGH_HAZARD: ("遠洋漁船船員", "爆破作業員"),
    OccupationClass.UNINSURABLE: ("高壓電力設施維修員", "核廢料處理人員", "商業潛水員"),
}


class MaritalStatus(StrEnum):
    """婚姻狀況. Texture, except that it constrains who may be a beneficiary."""

    SINGLE = "single"
    MARRIED = "married"
    DIVORCED = "divorced"
    WIDOWED = "widowed"


class IncomeBand(StrEnum):
    """年收入級距. Bounds what premium a suitability check will propose."""

    UNDER_500K = "under_500k"
    FROM_500K = "500k_1m"
    FROM_1M = "1m_2m"
    OVER_2M = "over_2m"


class BeneficiaryRelation(StrEnum):
    """受益人關係. 保險法 §16 requires an insurable interest, so this list is closed."""

    SELF = "self"
    SPOUSE = "spouse"
    CHILD = "child"
    PARENT = "parent"
    SIBLING = "sibling"
    LEGAL_HEIR = "legal_heir"
    """法定繼承人. The default when the applicant names nobody."""


class MedicalHistory(StrEnum):
    """
    既往症, as a health declaration records them.

    Declaring one does not decline a policy. It routes the case to underwriting, which
    is a human, which is the point: the desk collects the declaration and never rules
    on it.
    """

    NONE = "none"
    HYPERTENSION = "hypertension"
    DIABETES = "diabetes"
    HEPATITIS_B = "hepatitis_b"
    ASTHMA = "asthma"
    CANCER_HISTORY = "cancer_history"
    CARDIAC = "cardiac"


# Real cities, real districts, real roads. Enough spread that two generated addresses
# rarely look like variations of one another, and every combination is a place that
# exists — a generated address naming a district its city does not have is the kind of
# detail that makes a demo look assembled.
_ADDRESSES: dict[str, dict[str, tuple[str, ...]]] = {
    "臺北市": {
        "大安區": ("復興南路", "敦化南路", "信義路", "和平東路"),
        "中正區": ("羅斯福路", "中山南路", "忠孝西路"),
        "信義區": ("松高路", "基隆路", "信義路"),
        "士林區": ("中山北路", "文林路", "至誠路"),
    },
    "新北市": {
        "板橋區": ("文化路", "中山路", "縣民大道"),
        "新莊區": ("中正路", "思源路", "幸福路"),
        "新店區": ("北新路", "中興路", "民權路"),
        "三重區": ("重新路", "三和路", "正義北路"),
    },
    "桃園市": {
        "中壢區": ("中央西路", "環北路", "中山東路"),
        "桃園區": ("春日路", "中正路", "民生路"),
    },
    "臺中市": {
        "西屯區": ("台灣大道", "文心路", "河南路"),
        "北屯區": ("崇德路", "文心路", "松竹路"),
        "南屯區": ("公益路", "五權西路"),
    },
    "臺南市": {
        "東區": ("中華東路", "崇德路", "林森路"),
        "安平區": ("安平路", "健康路"),
    },
    "高雄市": {
        "左營區": ("博愛路", "自由路", "翠華路"),
        "三民區": ("建國路", "九如路", "民族路"),
        "前鎮區": ("中山路", "凱旋路"),
    },
}

_SURNAMES = ("陳", "林", "黃", "張", "李", "王", "吳", "劉", "蔡", "楊", "許", "鄭", "謝", "郭", "洪", "曾")
_GIVEN_MALE = ("志明", "俊宏", "建宏", "家豪", "冠廷", "承翰", "彥廷", "宗翰", "柏宇", "宇軒")
_GIVEN_FEMALE = ("淑芬", "怡君", "雅婷", "美玲", "詩涵", "宜庭", "佳穎", "欣怡", "郁婷", "思妤")


class Address(Struct, frozen=True):
    """
    A Taiwanese address, down to the level a policy application records.

    段 / 巷 / 弄 / 樓 are each optional in reality, so each is optional here. An
    address generator that always emits every level produces addresses that are
    individually plausible and collectively obviously synthetic.
    """

    city: str
    district: str
    road: str
    number: int
    section: int | None = None
    lane: int | None = None
    alley: int | None = None
    floor: int | None = None

    def __str__(self) -> str:
        parts = [self.city, self.district, self.road]
        if self.section:
            parts.append(f"{self.section}段")
        if self.lane:
            parts.append(f"{self.lane}巷")
        if self.alley:
            parts.append(f"{self.alley}弄")
        parts.append(f"{self.number}號")
        if self.floor:
            parts.append(f"{self.floor}樓")
        return "".join(parts)


class Person(Struct, frozen=True):
    """
    One demo applicant.

    `national_id` is checksum-valid unless the generator was asked for an invalid one.
    That option exists because a demo where every identity check passes never runs the
    rejection path, and an identity mock whose refusal branch is dead code is a stub
    wearing a mock's name.
    """

    name: str
    national_id: str
    sex: Sex
    birth_date: date
    occupation: str
    occupation_class: OccupationClass
    address: Address
    phone: str
    email: str
    marital_status: MaritalStatus
    income_band: IncomeBand
    medical_history: tuple[MedicalHistory, ...]
    beneficiary_relation: BeneficiaryRelation

    def age_on(self, when: date) -> int:
        """
        Give the plain age in completed years.

        Args:
            when: The date to age against.

        Returns:
            Completed years lived.

        """
        had_birthday = (when.month, when.day) >= (self.birth_date.month, self.birth_date.day)
        return when.year - self.birth_date.year - (0 if had_birthday else 1)

    def insurance_age_on(self, when: date) -> int:
        """
        Give 保險年齡, which is not the plain age.

        Every contract in the corpus states the same rule in its definitions: 以足歲計算，
        但未滿一歲的零數超過六個月者，加算一歲. So someone 34 years and 7 months old is 35
        for insurance, and that one year moves both eligibility and premium.

        Args:
            when: The date to age against.

        Returns:
            The age the insurer underwrites on.

        """
        years = self.age_on(when)
        anniversary = date(when.year - (0 if (when.month, when.day) >= (self.birth_date.month, self.birth_date.day) else 1), self.birth_date.month, self.birth_date.day)
        months_past = (when.year - anniversary.year) * 12 + when.month - anniversary.month
        return years + 1 if months_past > 6 else years




def _pick_occupation(rng: random.Random, *, allow_uninsurable: bool) -> tuple[str, OccupationClass]:
    """
    Choose an occupation, weighted the way a customer base actually is.

    Args:
        rng: The person's generator.
        allow_uninsurable: Whether the uninsurable class is in the draw.

    Returns:
        The occupation and its class.

    """
    weights = {
        OccupationClass.CLERICAL: 45,
        OccupationClass.LIGHT_MANUAL: 25,
        OccupationClass.SKILLED_MANUAL: 12,
        OccupationClass.HEAVY_MANUAL: 9,
        OccupationClass.HAZARDOUS: 5,
        OccupationClass.HIGH_HAZARD: 3,
        OccupationClass.UNINSURABLE: 1 if allow_uninsurable else 0,
    }
    classes = [c for c, w in weights.items() if w]
    cls = rng.choices(classes, weights=[weights[c] for c in classes])[0]
    return rng.choice(_OCCUPATIONS[cls]), cls


def _pick_address(rng: random.Random) -> Address:
    """
    Draw an address that exists.

    Args:
        rng: The person's generator.

    Returns:
        A structured address.

    """
    city = rng.choice(list(_ADDRESSES))
    district = rng.choice(list(_ADDRESSES[city]))
    return Address(
        city=city,
        district=district,
        road=rng.choice(_ADDRESSES[city][district]),
        section=rng.choice([None, None, 1, 2, 3, 4]),
        lane=rng.choice([None, None, None, rng.randint(1, 300)]),
        alley=rng.choice([None, None, None, None, rng.randint(1, 40)]),
        number=rng.randint(1, 480),
        floor=rng.choice([None, *range(1, 15)]),
    )


def _pick_history(rng: random.Random, insurance_age: int) -> tuple[MedicalHistory, ...]:
    """
    Draw a health declaration, weighted by age.

    Args:
        rng: The person's generator.
        insurance_age: Older applicants declare more.

    Returns:
        Declared conditions, possibly empty.

    """
    chance = 0.12 if insurance_age < 40 else 0.35 if insurance_age < 60 else 0.6
    if rng.random() > chance:
        return (MedicalHistory.NONE,)
    pool = [m for m in MedicalHistory if m is not MedicalHistory.NONE]
    return tuple(rng.sample(pool, k=rng.choice([1, 1, 1, 2])))


def generate(name: str, serial: int, *, today: date | None = None, valid_id: bool = True) -> Person:
    """
    Build the applicant behind a display name.

    Args:
        name: The display name the visitor typed.
        serial: Position in the demo's ID series, which keeps national IDs distinct.
        today: The date to age against, for tests.
        valid_id: False mints an ID whose checksum fails, so the identity mock's
            rejection path runs on stage instead of sitting as dead code.

    Returns:
        The applicant. The same name always returns the same one.

    """
    rng = rng_for(name, _SEED_SALT)
    today = today or datetime.now(UTC).date()

    sex = rng.choice([Sex.MALE, Sex.FEMALE])
    age = rng.randint(18, 85)
    month, day = rng.randint(1, 12), rng.randint(1, 28)
    # Subtracting the age from this year gives that age only once the birthday has
    # passed. A December birthday drawn in August lands a year short, which is how an
    # 18-to-85 range quietly produces a 17-year-old.
    birthday_passed = (month, day) <= (today.month, today.day)
    birth = date(today.year - age - (0 if birthday_passed else 1), month, day)

    national_id = issue(sex, serial)
    if not valid_id:
        # Move the check digit off by one. Everything else about the number stays
        # well-formed, so the refusal is a checksum refusal and not a shape complaint.
        national_id = national_id[:-1] + str((int(national_id[-1]) + 1) % 10)

    occupation, occupation_class = _pick_occupation(rng, allow_uninsurable=True)
    rng.choice(_GIVEN_MALE if sex is Sex.MALE else _GIVEN_FEMALE)

    person = Person(
        name=name,
        national_id=national_id,
        sex=sex,
        birth_date=birth,
        occupation=occupation,
        occupation_class=occupation_class,
        address=_pick_address(rng),
        phone=f"09{rng.randint(10, 89)}-{rng.randint(100, 999)}-{rng.randint(100, 999)}",
        email=f"{rng.choice(_SURNAMES).lower()}{rng.randint(1000, 9999)}@example.com.tw",
        marital_status=rng.choice(list(MaritalStatus)),
        income_band=rng.choice(list(IncomeBand)),
        medical_history=(),
        beneficiary_relation=rng.choice(list(BeneficiaryRelation)),
    )
    # History depends on insurance age, which depends on the birth date just drawn.
    history = _pick_history(rng, person.insurance_age_on(today))
    return Person(
        name=person.name,
        national_id=person.national_id,
        sex=person.sex,
        birth_date=person.birth_date,
        occupation=person.occupation,
        occupation_class=person.occupation_class,
        address=person.address,
        phone=person.phone,
        email=person.email,
        marital_status=person.marital_status,
        income_band=person.income_band,
        medical_history=history,
        beneficiary_relation=person.beneficiary_relation,
    )


# `given` is drawn so the generator's stream stays stable if a later version starts
# using a generated Chinese name instead of the typed display name.
_ = _GIVEN_MALE, _GIVEN_FEMALE
