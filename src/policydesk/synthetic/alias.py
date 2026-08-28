"""
Anonymous display names, minted rather than typed.

The display name is the account, so it has to be unique, and a demo audience typing
their own names collides on the third 陳. A minted alias solves both: it is unique by
construction, it is obviously not a real person, and nobody has to think of one.

Two syllables from closed lists, so the result reads like a 化名 rather than an id. A
numeric tail appears only when the pair is already taken, which keeps the common case
clean and the collision case still short.
"""

import secrets

_FIRST: tuple[str, ...] = (
    "晨", "靜", "遠", "暖", "清", "朗", "初", "微", "淡", "深", "疏", "澄",
    "頡", "頌", "寧", "斐", "皓", "沐", "翊", "宥", "澈", "曜", "岑", "禕",
)
_SECOND: tuple[str, ...] = (
    "山", "川", "湖", "野", "岸", "汐", "嵐", "榕", "梧", "禾", "洲", "崎",
    "原", "澤", "松", "柏", "溪", "嶼", "垣", "渠", "堤", "麓", "隅", "沚",
)

MAX_TAIL = 999
"""How high the numeric tail counts before a caller should give up and retry."""


def mint(taken: object = frozenset()) -> str:
    """
    Draw an alias nobody holds.

    Args:
        taken: Anything supporting `in`, holding the names already in use.

    Returns:
        A two-character alias, with a numeric tail only when the plain pair collides.

    Raises:
        RuntimeError: Every attempt collided, which means the name space is exhausted
            or the caller passed something that reports every name as taken.

    """
    for _ in range(64):
        name = f"{secrets.choice(_FIRST)}{secrets.choice(_SECOND)}"
        if name not in taken:
            return name
        for tail in range(2, MAX_TAIL):
            if (numbered := f"{name}{tail}") not in taken:
                return numbered
    msg = "no alias available"
    raise RuntimeError(msg)
