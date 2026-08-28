"""
Scenarios that live in their own module rather than in `scenario.py`.

One file per scenario once it carries logic of its own — its tools, its citation format,
its own checker. `scenario.py` stays the catalogue.

Nothing is imported here on purpose. A scenario module takes its type from
`scenario_base`, which imports nothing back, so there is no cycle left to order around.
An import in this file would put one back: every module in the package would then drag
in the catalogue, and a scenario that only wants `Scenario` would load every other
scenario to get it.
"""
