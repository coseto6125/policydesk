# policydesk — start here

You read this file automatically on every start. It exists because this session restarts
often, and the standing rules have to survive the restart. Read the three files below
before you change any code.

## The three files

| File | What it holds | When to read it |
|---|---|---|
| `~/.claude/CLAUDE.md` | The machine-wide rules: philosophy, dispatch, search strategy, test discipline, prompt writing, code style, commit authorship. | Every start. |
| `../CLAUDE.md` | The workspace rules for `enor_agi`: the venv, the third-party API notes, `ruff --fix` on save, root-cause-before-fix. | Every start. |
| `SUPERVISOR-NOTES.md` | The open list, and every measured number behind it. An observing session keeps it current. | Every start, and again before you pick your next task. |

`SUPERVISOR-ARCHIVE.md` is the dated evidence record. Read it only to check how a number
in the notes was produced.

## What this desk guarantees

A reply cites the clauses the tools returned for that turn, and states no figure, clause
or provision that no tool returned. 理賠是人工審查, so the desk makes no promise about an
outcome.

## Three red lines

- The identity gate lives on the tool as `@requires_identity` or `@public`, never in a
  prompt. `tests/test_identity_inventory.py` holds the whole contract, including the
  fail-closed floor: a name the gate cannot resolve is gated.
- Prompts stay general. A scenario opens its own function tool to meet its need. A prompt
  that maps one question to one answer is the failure this rule prevents.
- The corpus holds synthetic PII. Treat it as real.

## Before you run the suite

Four test files write to the live corpus, so a full run changes the data other
measurements read. Run the files your change touches. Run the full suite once, at the end.
