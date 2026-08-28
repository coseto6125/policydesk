"""
Scenarios that live in their own module rather than in `scenario.py`.

One file per scenario once it carries logic of its own — its tools, its citation format,
its own checker. `scenario.py` stays the catalogue and the shared shape.

The import below is load-bearing. A scenario module needs `Scenario` and `Param` from
`scenario.py`, and `scenario.py` needs the finished value back to put in `CATALOGUE`, so
the two form a cycle whose outcome depends on which one Python reaches first. Reached
through `scenario.py` it resolves; reached directly — `from policydesk.agent.scenarios.
soothe import SOOTHE`, which is what a test does — Python starts this package's module,
finds it empty, and raises ImportError on a name that is genuinely there.

Importing `scenario` here makes the order deterministic: the package cannot be entered
without `scenario.py` being driven to completion first, whichever module the caller
asked for.
"""

from policydesk.agent import scenario as _scenario
