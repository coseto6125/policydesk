# Writing a scenario module

Read this before adding a scenario. `src/policydesk/agent/scenarios/soothe.py` is the
worked example; this file is the contract it follows.

## The shape

One module under `src/policydesk/agent/scenarios/`, exporting three names:

```python
TOOLS: dict[str, Callable]      # every tool this scenario may call, by the name the Scenario lists
async def gather(db, params, *, member_id=None, today=None, retriever=None,
                 allowed: frozenset[str] | None = None, **_) -> dict[str, Any]
<NAME>: Scenario                # the value, with tools_module set to this module's dotted path
```

`gather` returns the material by tool name. The executor hands it to the model as etoon.
It may add `_allowed_clauses: frozenset[str]` naming the `clause_id`s the reply may cite;
omit it and the executor treats it as empty.

Import the type from `policydesk.agent.scenario_base`, **never** from
`policydesk.agent.scenario` — the catalogue imports you back, and that is a cycle whose
outcome depends on which module the process reached first.

## The identity gate

`@policydesk.agent.tools.requires_identity` marks a tool that reads one named customer's
record. The gate is derived from the marks on the functions in your `TOOLS`, so a
scenario that reads the member's book gets gated automatically and one that reads only
public tables does not. Do not declare the gate on the Scenario; there is no field for it.

An unresolvable tool name reads as gated. That is why `tools_module` is not optional.

The gate is per tool. `allowed` names the tools that may run this turn; a tool whose name
is not in it must not be called, so its query never runs rather than running and having
its output dropped. `None` means all of them, which is what a direct call in a test gets.

```python
def _can(name: str) -> bool:
    return allowed is None or name in allowed
```

Split the tools so the public half survives the gate. A customer who has not proved who
they are still gets 猶豫期是十天 and 據實說明義務是什麼, with the request for an ID
attached to that material rather than standing in place of it. A desk that refuses the
public half too is one that reads as stalling.

Do not set `_identity_required` yourself. The executor sets it whenever it withheld a
tool, so a module that forgets cannot hand the model a partial answer that reads as a
whole one. Write the `injection` to expect it: say what the public material supports, ask
for the national ID, say what it unlocks, and invent no part of the member half.

## What a reply may never contain

- 賠 / 不賠 as a decision. 核保與理賠的准駁由核保理賠人員決定.
- A promise: 我們會賠, 一定過, 保證通過, 我們會通融.
- 認錯 on the company's behalf.
- A figure the tools did not return. Money comes from a tool row, never from prose.
- A clause or statute the retrieval did not return. Misremembering a provision is worse
  than saying 這部分需要查證.

## Citation syntax

Contract clauses: `[art.12]`, checked against the member's own products.
Statute: `〔保險法 第64條第2項〕`, checked against `statute_article`. The two syntaxes are
deliberately different — `art.64.2` contains `art.64`, so a statute written that way
would be read as a clause citation and the whole reply withheld.

## Schema

```
member(member_id, display_name, national_id, sex, birth_date, occupation, occupation_class,
       address_city, address_district, address_rest, phone, email, marital_status,
       income_band, medical_history text[], beneficiary_relation, profile_frozen_at)
policy(policy_id, member_id, product_id, policy_number, sum_insured, effective_at,
       lapsed_at, main_policy_id)          -- main_policy_id NULL means a main policy
product(product_id, insurer, name, line, attachment, approval, pages, source_url)
                                            -- line: health|life|accident|annuity|investment
catalog_entry(product_id, issue_age_min, issue_age_max, max_occupation, unit_premium,
              unit_label, requires_main, on_sale)
clause(product_id, clause_id, kind, heading, verbatim, page, overrides text[])
benefit(product_id, name, trigger, formula, notes, page)
required_document(product_id, benefit, document, condition, page)
surgery_multiplier(product_id, schedule, code, procedure, multiplier, page)
statute(statute_id, name, authority, amended_at, source_url)
statute_article(statute_id, doc_id, article, branch, paragraph, subparagraph, chapter,
                heading, verbatim)
member_fact(member_id, key, value, category, source_message_id, updated_at)
```

`policydesk.synthetic.person.insurance_age(birth_date, when)` gives 保險年齡.
`policydesk.agent.statute.search_statute(db, query, statute_ids, limit=, retriever=)`
ranks the law. `policydesk.retrieval.base.Retriever` is the retriever protocol.

## psqlpy traps, each one measured here

- `numeric` binds from `Decimal` only. An int, a float or a str all fail with
  `insufficient data left in message`, naming neither the column nor the type.
- `record[]` panics in psqlpy's Rust layer with `entered unreachable code`. Bind parallel
  arrays and join through `unnest($1::text[], $2::text[]) AS want(a, b)`.
- `jsonb` binds from a dict, never from a JSON string.
- Parameters are `$1::int` style, passed as a list: `await db.fetch(sql, [a, b])`.

## Style

Python 3.14, `uv run` for everything. `ruff check --fix src tests` must pass with
`select = ["ALL"]`. Google docstrings on every function, with Args and Returns. Comments
explain why, never what. Tests are `tests/test_<name>.py`, named
`test_<function>_<scenario>_<expected>`, and call the real functions.

Never add a `Co-Authored-By: Claude` trailer to a commit, nor a 🤖 footer to a PR body.
