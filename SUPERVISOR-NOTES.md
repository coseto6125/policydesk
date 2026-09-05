# Supervisor notes

## Commit-time status after reboot — 2026-09-05

The user independently accepted items 1–6. Item 7 is split into logical commits on `feat/retrieval-hardening`.
PostgreSQL on port 5434 is unavailable after reboot. The user must enable Docker Desktop WSL integration.
Do not interpret skipped DB tests as passing integration evidence.
The four new socket cases are defined, but have not run against a live DB after reboot.
`data/evaluations/boundaries-20260905-r1.jsonl` records connection failures, not scenario results.

Commit-time verification: 250 passed, zero skipped, using only pure functions, mocks, and local PDF fixtures.
The selected files are `test_exercise`, `test_codex_provider`, `test_executor_citation`, `test_clause_index`,
`test_validator`, `test_alternatives`, and `test_identity_inventory`.
Selected additional tests cover the mocked FACTS sweep, prompt ordering, billing fixture dates,
empty retrieval scope, and eight requirements per product.
Ruff passed for `src/policydesk`, `tests`, and `scripts/exercise.py`.
No full suite, PostgreSQL integration, live dialog, migration, or HTTP temperature measurement ran at commit time.
The HTTP temperature experiment still requires an API key and at least three trials per arm.

The selected tests found one stale assertion that still expected a 12-row context.
It now supplies 45 rows and checks the shared evidence limit and excluded citation key.
The new driver tests load the script with `runpy`, since `scripts` is not an installed package.

Local `.claude/` state stays untracked. `SUPERVISOR-ARCHIVE.md` also stays untracked because a scan found identity-number-shaped data.
Do not publish that archive without a separate redaction review. No remote push is part of this work.

## Temperature backend boundary — 2026-09-05

The deployed provider is CodexCliProvider when OPENAI_API_KEY and POLICYDESK_PROVIDER are unset.
Codex CLI has no temperature setting. The observer tested four forms:
`model_temperature` and `temperature` are unknown fields; `model.temperature` fails because
`model` is a string; `sampling` does not exist. Do not repeat that configuration search.
Use the existing `model_reasoning_effort` and output schema to constrain CLI output.

Per-phase temperature belongs only in OpenAIProvider's HTTP request body.
The initial settings are 0.1 for routing/validation/repair and 0.3 for answering.
These are implementation defaults, not measured optima. The floor stays at 0.1.
Run at least three trials per arm with POLICYDESK_PROVIDER=openai and a configured API key.
Label those results HTTP-path evidence, not deployed CLI-path evidence.
No HTTP temperature trial has run in this session: the API key is not configured.

### Phase coverage and usage are different — 2026-09-05

The observer's count is route 1,439, answer 547, facts 170; the other four phases have zero rows.
The DB constraint includes seven phases. The enum previously omitted FACTS despite its docstring.
FACTS now belongs to the enum; memory.sweep_once passes it to complete() and binds its value in SQL.
HTTP fact extraction receives temperature 0.1. CLI fact extraction remains on its existing sampler.

Source inspection distinguishes the four zero-row phases:

| Phase | Runtime evidence | Meaning of zero rows |
|---|---|---|
| scenario_tools | executor._gather calls Python/DB/retrieval functions, not Provider.complete | No separate model-call stage exists here. Tool work runs, but this table does not trace it. |
| validate | validator.validate calls Provider.complete; only tests call validate, while executor calls deterministic recheck | The model-validation path is not integrated. validate itself returns Completion without writing usage; a future caller must record it. |
| repair | No repair call or branch exists in src; failed citations produce WITHHELD | No model-repair path exists to exercise. |
| embedding | ServerEmbedder.encode calls /v1/embeddings directly; EmbeddingChannel.search encodes queries | Real model calls bypass llm_usage. This is an embedding usage/trace gap, not proof that embeddings are disabled. |

At 08:19 the running service reported bm25+embedding and 23,588 vector rows.
The index audit reported missing=0, stale=0, uncovered=0 and complete=true against current DB text.
Its encoder is llama-server 8090, bge-m3-Q5_K_M.gguf; source_sha256 starts 5594dc4b48eba9dc.
This proves current DB embedding coverage, not completeness of PDF extraction.
Do not fabricate phase rows for stages that do not make model calls.

An observing session watches this work and writes here. Every number below was re-run
against the live DB, not copied from a report. Read this block, then the section for
whatever you are about to touch. The dated sections below are the working record, oldest
first; where two disagree, the later date wins.

## Read this first if you are about to measure a prompt rule — 05:34

`codex debug prompt-input` does not take `--ignore-user-config` or `--ignore-rules`, and
you do not need it. You have tried three variants of that command; the flag is not the
problem, the target is. That command isolates the **codex CLI's own** prompt. The rule you
want to measure is the instruction **policydesk** sends to `POLICYDESK_MODEL`, assembled at
`executor.py:207` and `executor.py:707`, and the driver for it is `scripts/exercise.py`.
The `CLAUDE_CONFIG_DIR` recipe in `validate-prompt-rules` isolates `claude -p`; it does not
apply here either. Full shape in "The A/B you are setting up is aimed at the wrong
process" below.

## Open — 05:50, verified at your idle point

Done, and I checked each one rather than taking the report: the article boundary (longest
clause 420,375 -> 54,451 chars, 40 over 100k -> 0), the brochure classification and its two
views, the index rebuild (53,120 -> 23,576 passages, top-40 share 32% -> 14.3%), the reload
guard (`__main__.py` binds `copy_corpus` at line 52 to `refresh_document_kinds` at line 82),
and the fabricated-rate disclosure with a correctly run A/B (control 0/6, rule 6/6, on the
deployment model, read individually).

Still open, most consequential first:

1. **Statute headings are all empty** — 1,212 articles with none, against 10,478 clauses
   with one, so `BOOST_HEADING = 3.0` never fires on that half. **Corrected 06:55:** this
   does not produce wrong answers today. Scenarios pass their own curated query and scope
   to `search_statute`, and the live replies cite §64, §59, §111-113 and 金保法 §13
   correctly. The cost is generality — the curated queries and `GRACE_ARTICLE = 116` are
   compensations for weak ranking. recall.py's R@1 0.57 measures the bare channel, not
   answer quality; do not quote it as the latter. See the archive.

2. **The health premium range is calibrated for a unit it no longer has.** basis 1,000 with
   a 1,200-4,800 annual premium is 120%-480% of cover; every other line is 0.09%-30%. The
   range belongs to 每日 1,000 元住院日額.
3. **Commit.** 38 paths, 0 commits, four context resets.
4. **`cited()` reads `FROM clause`** at `web/console.py:218`. One word.
5. **The excerpt marker.** `row["excerpt"]` is a boolean no prompt reads. Put `…` in the
   sliced text.
6. **Two editions of one contract, both in the catalogue.** 5 products, 10 rows, premiums
   differing because `_stable_premium` hashes `product_id`.
7. **Third-person address.** 3 of 26 replies say 這位保戶 to the customer.
8. **24 product names carry leading or trailing whitespace.**
9. **The writing tests share the live database.** There is no `tests/conftest.py` — you
   have searched for it three times across restarts — so each of the 19 test files opens
   its own `Database()` against the live instance. That absence is the reason, and a
   conftest with a database fixture is where the isolation belongs.

## On test_quote going green

You changed the premise, which was the right call — the old docstring claimed `unit_label`
was 「the government's own vocabulary」 and it is not, it is `_UNITS`, invented here. Same
false premise as quote.py's 「The rate is public」.

The cost is that the only assertion pinning a health product's unit is gone, while
`_UNITS["health"]` still pairs 每 1,000 元保額 with a daily-benefit premium range. The suite
went green; the number a customer reads did not become sane. Put the plausibility back as
its own assertion rather than as a label match: a quoted annual premium is a sane fraction
of the cover it buys, for every line.

## Standing constraints

- No hardcoded prompt keyed to a topic. A scenario declares its own function tools; the
  meaning goes in the data, and the prompt keeps only what the data cannot carry.
- Measure a prompt rule before shipping it: `/validate-prompt-rules`, n>=3 per arm,
  isolated, control arm read first.
- A reply states no figure, clause or provision the tools did not return.
- 理賠是人工審查. The agent promises nothing.

---

10. **Reply latency is climbing and nothing watches it.** `exercise.py` records
   `seconds` on every turn; no check reads it. Median across the two comparable full runs
   moved 17.3s -> 26.8s, slowest turn 32.7s -> 52.9s. Not a controlled comparison — see
   the archive — but the direction is wrong and everything added since costs per turn. A
   customer waiting 48 seconds has left. Add a scenario-independent threshold check.

11. **A statute citation is validated for existence, not provenance.** `_allowed_clauses`
   bounds contract clauses to this turn's tool results; nothing does that for statutes, so
   any of 保險法's 995 articles may appear in prose and pass. This is the 前輪條號混用 you
   named, and it is a hole in the desk's own red line. Build `_allowed_statutes` the way
   `_allowed_clauses` is built.



## Recall baseline, 07:33 — pinned on the hash, not the generation

```
generation 5d61c141   passages 23588   source_sha256 5594dc4b48eba9dc

channel      R@1   R@3   R@5    MRR
bm25         0.50  0.57  0.57   0.53
embedding    0.50  0.72  0.75   0.60
hybrid       0.57  0.72  0.85   0.67
```

Against the previous reading (R@1 0.57, R@3 0.75, R@5 0.82, MRR 0.67): R@5 up 3 points,
R@3 down 3, R@1 and MRR unchanged, and one question flipped from miss to hit
(評議中心多久內要受理申訴), 7 misses down to 6.

**Do not attribute that.** What changed between the two was a re-parse of two contracts
(10,478 → 10,512 clauses) and a rebuild. One question out of forty flipping is inside the
noise of a single run. The honest reading is unchanged.

I recorded the earlier baseline against the generation id, which was wrong — the id was
reused while `source_sha256` changed underneath it. The hash identifies the corpus; pin
that.

## Where the evidence is

`SUPERVISOR-ARCHIVE.md` beside this file holds every measurement, the reasoning and the
resolved items, oldest first. Read it for why; this file is what is still open.

## Nothing on this list has landed

Five items were handed over across the session. At 07:24: `@public` on 1 tool of 16,
`temperature` still unset, 8,815 of 10,478 clauses still carrying spurious spaces,
`cited()` still on the raw table, 0 commits against 48 changed paths.

The work being done instead is good and self-found — the 申領 classifier, 廿/卅, the schema
enums, the empty-quote hole. The pattern is that the list only grows. Both halves are worth
saying: the found work is real, and none of the handed-over work has started.

## Compatibility ideographs in the corpus — and do NOT apply NFKC wholesale

`test_required_documents_compatibility_heading_returns_document_list` fails on
`assert "受益人的身分證明" in row["verbatim"]` while the text visibly contains it. The
character is not the one it looks like:

```
'益'  U+FA17     (CJK Compatibility Ideographs)   not U+76CA
```

Identical on screen, different codepoint, so every string comparison fails and nobody can
see why. Stripping whitespace does not help — this is not the space defect.

Measured over all 10,512 contract clauses: 10,470 (99.6%) contain at least one character
that is not in NFKC normal form, 122 distinct characters, 182,566 occurrences. But the
count splits into two populations that need opposite treatment.

**Full-width punctuation — leave it alone.** 108,740 of those are `，` U+FF0C, plus `：`,
`（`, `）`, `；`. NFKC turns them into ASCII `,` `:` `(` `)`, and a Chinese insurance
contract is correctly written with full-width punctuation. Normalising them would make the
text the customer reads look like badly typeset English.

**Compatibility ideographs — these are the bug.** Small in count, and every one breaks
matching, segmentation and retrieval:

```
'行' U+FA08 → 行    1,988
'年' U+F98E → 年      786
'益' U+FA17 → 益    (the one this test hit)
```

行 and 年 are among the most common characters in a contract — 本契約, 保險年齡, 一年, 執行.
jieba cannot segment 保險年齡 when 年 is U+F98E, BM25 cannot match a query for 年 against
those 786, and the embedding tokenizer sees a character it may not know. Display is
perfect, which is why this can sit for a long time.

So normalise the compatibility block only:

```python
def normalise(text: str) -> str:
    # Compatibility ideographs to their canonical form. Full-width punctuation is how a
    # Chinese contract is written, so it stays.
    return "".join(
        unicodedata.normalize("NFKC", c) if "豈" <= c <= "﫿" else c
        for c in text
    )
```

Same layer and same blast radius as the CJK line-join fix — ingest, then jieba, BM25 and
the vectors — and easier, because it is a codepoint range rather than a boundary judgement.
Do both in one re-ingest, then re-measure recall and record `source_sha256` beside the
numbers.

## DOCUMENTS_PER_PRODUCT: the distribution says 8, and 12 is waste

You are comparing k = 2, 6, 12. Measured over the corpus, 292 of 299 contracts carry
clauses whose heading says 申領 or 保險金的申請 — median 4 each, p90 5, max 8:

```
k= 2  →  41.1% of contracts fully covered
k= 4  →  71.2%
k= 6  →  98.6%
k= 8  →  100%
k=12  →  100%
```

These clauses average 176 characters, so the cost is small: k=2 is ~352 characters, k=6
~1,056, k=12 ~2,112.

**Today's k=2 fully covers 41% of contracts.** More than half of customers asking 理賠要
準備什麼 get a list with items missing, and nothing in the reply says a part is missing.

k=12 can be dropped from the comparison: it buys nothing over k=8 and costs twice the
characters. The real choice is 6 against 8 — 98.6% for 350 fewer characters, or 100%. For a
document checklist, where one missing item is simply a wrong answer, 8 looks right: four
contracts handing out incomplete lists is worse than 350 characters.

**`_short`'s twelve-row cap is coupled to this.** `DOCUMENTS_PER_PRODUCT`'s own docstring
says twelve rows total is what makes five products fit. At k=8 a customer holding five
policies needs 40 rows. Raise one and the other has to move, or the cap silently truncates
what the k was raised to include — which would leave the same defect with a larger number
in front of it.

## The English reply switches back to Chinese halfway, and every check passes

The `english` case passes route, no_faults, has_reply, contract_sources_present and
documents_open. None of them looks at the language:

```
You currently hold two active policies.

Product: 國泰人壽脂有為你特定傷病定期健康保險(外溢型).
Policy number: CL8866-280475.
Sum insured: 1,500 元.
Status: Active.
給付項目：              ← switches here
保
```

Four field labels are translated and the fifth is not. That is not a design decision, it is
the model losing the thread — most likely because that section comes from a different tool
result, and `i18n.hint` only says 「reply in English」 in the instructions with nothing
holding the whole reply to it.

Separate the two halves before fixing:

- **The product name should stay Chinese.** 國泰人壽脂有為你特定傷病定期健康保險 is what is
  printed on the customer's policy; translating it stops them matching the reply to their
  own document. `1,500 元` is arguable.
- **A field label like 給付項目 must follow the reply's language.** The four labels above it
  did.

A check that catches this is cheap and scenario-independent: outside product names and
policy numbers, the CJK ratio of a reply whose locale is `en` should be near zero. Today it
is a fifth of the text.

## A second English turn was withheld

```
locale=und
本次查詢的回覆引用了無法查證的條款或法條，為避免提供錯誤資訊，已保留該回覆並轉由專人與您確認。
```

`_unverifiable` held a reply back in the English conversation. Two possible causes with
opposite fixes: the reply genuinely cited something the tools did not return, or the English
rendering of a citation is a shape `_CITATION` cannot read, so a correct reply was withheld.
Worth resolving before the fidelity check is wired, because that will add a second way for a
correct reply to be held.

Note also `locale=und` on that row: the detector returned UNKNOWN and the fallback chose the
reply language. Whether the withheld turn and the undetected locale are the same failure is
part of the same question.

## The observing session made four changes — 08:05

Done by the observer, not by you. Verified by `pytest tests/test_identity_inventory.py
tests/test_console.py tests/test_executor_citation.py tests/test_product_clauses.py`,
167 passed.

1. **All 37 tools now declare.** 22 already carried `@requires_identity`. The other 15 are
   public reference lookups and now carry `@public`. Measured against `PERSONAL_TABLES`:
   none of the 15 reads a personal table. The gate's behaviour did not change, because an
   unflagged tool already defaulted to ungated. What changed is that absence of a flag is
   no longer indistinguishable from a decision.
2. **`PERSONAL_TABLES` had a false positive.** `"beneficiary"` was a bare word among
   `FROM member` / `JOIN policy` neighbours, so it matched the docstrings of
   `designated_protection`, `designation_rules` and `grace_rule` — three §110-112 statute
   tools that read no record. It is now `FROM beneficiary` / `JOIN beneficiary`.
3. **Two evidence paths left the raw table.** `web/console.py` `cited()` and
   `web/server.py`'s clause viewer both read `FROM clause`; both now read
   `FROM contract_clause`. Measured first: all 216 qualified citations and all 288
   policies point at products whose `document_kind` is `contract`, so nothing existing
   stops resolving. `test_console.py`'s fake routes on a SQL substring, so its two
   `"FROM clause"` keys moved with the query.
4. **`AGENTS.md` now exists at the repo root.** You look for it on every start; it was
   missing at all three levels, which is why the standing rules did not survive a restart.
   It points at `~/.claude/CLAUDE.md`, `../CLAUDE.md` and this file. It restates no rule.

`ecp` reports `list_policies` at tools.py:168; the file says 192. Its line anchors are
stale against 48 uncommitted paths. Grep to confirm a line before you read a region.

## The provider file you keep guessing at — 08:10

You have looked for `llm/codex_provider.py`, `llm/codex.py`, `agent/spec.py` and
`core/provider.py`. None exists. The file is **`src/policydesk/llm/provider.py`**, and it
holds all three providers: `OpenAIProvider` (101), `ScriptedProvider` (218),
`CodexCliProvider` (362), plus `build_provider()` (595).

For the temperature item, this is the whole map:

| What | Where |
|---|---|
| The request body the API sees | `provider.py:160-171`, `OpenAIProvider.complete` |
| `complete()`'s signature, which carries no phase today | `provider.py:134-142` |
| `Phase` enum: ROUTE, SCENARIO_TOOLS, ANSWER, VALIDATE, REPAIR, EMBEDDING | `provider.py:38-53` |
| Call sites that already name their phase | `executor.py:232, 670, 757, 761`; `validator.py:236` |

The phase is known where the call is made, and it reaches `_record` but not `complete`.
So per-phase temperature means one new keyword on `complete()`, passed from the call sites
that already hold the `Phase`.

Two constraints the user set, both measured, neither negotiable:

- The floor is **0.1**, not 0. Zero risks an infinite loop unless you also set a
  repetition penalty. The knobs differ by endpoint: `frequency_penalty` /
  `presence_penalty` on the OpenAI-compatible endpoint, `repeat_penalty` /
  `repeat_last_n` on llama-server 8090.
- **n≥3 per arm, whatever the temperature.** A lower temperature narrows the spread of
  wording. It does not make one trial evidence of causation, and provider batching keeps
  output non-deterministic even at 0.

## Baseline for item 2, taken before you change anything — 08:28

Both rulers self-test in both directions before the count is trusted. Write the range as
codepoint escapes; a literal U+FA08 typed into a heredoc normalises to U+884C on the way
in, and the ruler then matches everything or nothing. That mistake was made six times in
this session.

| Measure | Before | After the fix must be |
|---|---|---|
| Clauses holding U+F900-U+FAFF | 1,933 | 0 |
| Clauses holding U+FF01-U+FF5E | 11,656 | 11,656, unchanged |
| Full-width characters, total | 327,458 | 327,458, unchanged |
| Clauses | 11,775 | 11,775 |

The second and third rows are the point. Wholesale NFKC would take the compatibility
ideographs and the full-width punctuation together, and the clause text would read the
same on screen while 「（一）」 became 「(一)」. Convert the first range only, then run all
four counts and show them.

The CJK line-join defect is already at 0 of 11,775, so that one is closed. Re-run its
count after the reingest anyway, because it is the same pipeline.

### Item 2 verification — 2026-09-05

The parser now translates only U+F900–U+FAFF before the existing CJK line-join pass.
The existing DB was updated without changing clause IDs, pages, product classification, or PDF files.
The backup is `data/evaluations/corpus-before-ideographs-20260905.dump`.
There were 1,938 changed rows when heading and body were both considered; the baseline's 1,933 counts bodies only.

The rulers use escaped ranges and test both positive and negative codepoints:
U+FA08 matches / U+884C does not; U+FF08 matches / U+0028 does not.
After conversion the CJK gap ruler found 128 newly visible cases, because the old rule did not recognize compatibility ideographs.
Those rows passed through the existing `_tidy()` after normalization. No new gap rule was added.

| Measure | Verified after |
|---|---|
| Clauses holding U+F900–U+FAFF | 0 |
| Clauses holding U+FF01–U+FF5E | 11,656 |
| Full-width characters | 327,458 |
| Clauses | 11,775 |
| Clauses holding intra-CJK gaps | 0 |

Focused parser/loader tests: 59 passed. Parser/product/retrieval tests after the update: 134 passed, 4 skipped.
The first rebuild was stopped before completion after the gap measurement found 128 cases.
The replacement rebuild uses the corrected DB, llama-server 8090, 256 tokens and 48 overlap.
Its completion and post-build audit must be checked before the service restarts.

### Item 3 total answer-context budget — 2026-09-05

`DOCUMENTS_PER_PRODUCT=8` limits retrieval per product, not the customer's product count.
The answer assembler now applies two independent constants across every tool result:

- `MAX_EVIDENCE_ROWS=40` limits retained clause-row occurrences, including duplicates and nested rows.
- `MAX_EVIDENCE_CHARS=128_000` limits the actual serialized tool context, including metadata and formatting.

This character budget is not a token count or a bound on history, instructions, schema, or output.
The assembler shares rows round-robin across products and preserves rank within each product.
It removes whole rows until both limits hold; existing per-field clipping remains in place.
Only retained evidence enters the citation schema and quote subject.
`evidence_coverage` reports omitted rows and marks the returned evidence incomplete.
This describes the tool results, not recall against every clause in the PDFs.
The application adds a customer-visible incomplete-review notice without relying on model compliance.
If non-evidence material alone exceeds the character limit, the application withholds the context and skips answering.

Measured with 12 synthetic products and 8 returned rows per product:

| Clause body | Before rows / serialized characters | After rows / serialized characters | Products retained |
|---|---|---|---|
| 100 characters | 96 / 10,822 | 40 / 4,596 | 12 |
| 4,000 characters | 96 / 385,222 | 31 / 124,486 | 12 |
| 1,500 repeated three-character lines | 96 / 522,471 | 23 / 125,248 | 12 |

The integration tests call `run_turn()` and inspect the prompt passed to `Provider.complete()`.
They cover those three shapes, retained citation keys, the character ceiling, and the application notice.
Additional tests cover shared multi-tool budgets, nested evidence, oversized non-evidence material, and five-product coverage.
The five-product test expects all current per-product rows to remain when their text fits.
Increasing retrieval depth beyond that row capacity therefore requires an explicit test and budget decision.
An independent review found repeated tuple references bypassed the row cap through shared object identity.
The regression first reproduced 96 retained rows; `_short` now converts tuples through the same recursive path as lists.
The same probe now retains 40 rows and reports 56 omitted rows.
Temporarily changing `DOCUMENTS_PER_PRODUCT` from 8 to 9 makes the five-product coverage assertion fail as intended.
Focused executor, product-clause, validator, and identity tests: 137 passed. Ruff: clean.
Existing `_short` conversion and retrieval clipping regressions: 4 passed, 51 deselected.
These are deterministic assembly tests, not live-model quality measurements or a claim of complete multi-policy coverage.
