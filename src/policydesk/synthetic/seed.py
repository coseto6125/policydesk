"""
One seed, derived from a name.

The same display name must produce the same person and the same policy history on
every restart, so a rehearsal and the live run tell one story and a case reopened
tomorrow still belongs to the same applicant.

That determinism does not make the demo feel rehearsed, because the freshness comes
from somewhere else: whoever is at the keyboard chooses the name. The seed fixes who a
name *is*; it never fixes which name gets typed.

Each generator passes its own salt, so a person's demographics and their policy
history are drawn from independent streams. Adding a field to one does not shift the
other, which is what keeps a portfolio stable while the person generator changes.
"""

import random
from hashlib import blake2b


def rng_for(name: str, salt: str) -> random.Random:
    """
    Build the generator for one name under one salt.

    Args:
        name: The display name as typed. Surrounding whitespace is ignored, so
            "王小明" and " 王小明 " are one person.
        salt: Which stream to draw from. Changing it changes every generated value.

    Returns:
        A Random seeded so this name always yields the same sequence.

    """
    key = f"{name.strip()}|{salt}".encode()
    return random.Random(int.from_bytes(blake2b(key, digest_size=8).digest(), "big"))
