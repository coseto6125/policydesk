# Supervisor notes

## Document demo acceptance — 2026-09-06

The user limits this release to an internal demonstration, with no real document intake or signatures.
The customer chooses fixed matching or mismatched samples; matching records both mock signing roles.
The UI and printable forms identify simulation, with an accessible ! explanation of planned local-model checks.
No local model verifies this release's samples. A data check would not establish identity or signature validity.

Commands and DB records determine progress. Queries cannot sign, verify or submit a case.
State tests prove workflow correctness, not truthful model prose. Reply fidelity requires separate semantic evaluation.
The document-specific keyword checker and rewrite loop were removed; the model receives current records and writes natural guidance.
Current tool facts include successful identity checks, submission status and the actual signing roles.
These facts are read under the case lock; model wording never grants permission to advance.
Scenario routing now looks up application-progress follow-ups instead of inferring current state from earlier replies.

Targeted command:
`rtk proxy .venv/bin/python -m pytest tests/test_executor_citation.py tests/test_identity_gate.py tests/test_document_flow.py tests/test_desk_ui.py tests/test_acceptance_fixes.py tests/test_identity_inventory.py -q -ra --tb=short`.
Result: 362 passed, 0 skipped, in 12.79 seconds. Ruff and the prompt reference audit passed.
Mutation proof: changing submission's target from REVIEW to VERIFIED made the new lifecycle test fail at the state assertion.
The mutation was restored. No full suite or corpus-loader tests ran, and no browser visual acceptance is claimed.

The final transcript uses the real customer socket handler, DB and Anthropic HTTP provider; the socket transport is a test driver.
Staff prepares documents and performs mock verification/submission through core commands, not nonexistent customer buttons.
Raw transcripts and semantic probes live under `data/evaluations/document-guidance-*-final-20260906.txt`.
The final walk completed all ten samples, verified, then submitted; both progress questions left records unchanged.
After cleanup: product=660, policy=288, clause=11775, contract_clause=10512, doc-prefixed members=0.
The final Haiku HTTP probe has three replies per arm for nine branches, plus six routing trials.
All three final verified replies describe completed verification and a future submission; all six routes select document_progress.
Earlier probes exposed a repeated-verification error and an invented signer; the final tool facts address those evidence gaps.
This is scoped semantic review, not a guarantee about future answers or local-model verification.
Earlier A/B keyword scores, including v5's 0/3, are not semantic acceptance evidence.
The old-template control is instruction wording, not an old-runtime replay; small samples do not establish a production error rate.

FU-2026-09-06-ced541697419 is scoped to demo transparency and guidance.
Real intake still requires authenticated role-specific consent, actual file/storage/signature checks,
client-viewed version binding, replay handling and per-upload audit. Those are outside this release.

## Security findings 04 and 06 — 2026-09-06, 00:24

04 is fixed in the runtime dispatch gate, not only in inventory tests.
An explicit False declaration is public; explicit True requires identity confirmation.
Missing or invalid declarations are unreviewed and excluded even after confirmation.
Both shared-module and scenario-owned tools follow this rule, including name overrides.
`alternatives` now explicitly declares public catalogue access. No tool schema changed.
The imported signing helper and the private clause helper were not promoted to public tools.
The new gather regression first proved an unmarked member reader actually executed in
a confirmed session, then passed after the fix. Gate matrices cover absent declarations,
None, zero, one, string flags, public access and confirmed private access.

The original, pre-scope 06 assessment follows; the document demo section above supersedes its release scope.
Real intake needs deterministic socket/core checks, not LLM recheck.
Existing guards cover confirmed session, session case ID, document ownership, stage,
filename length and a single transaction with case/document locks.
Actual handler probes show string IDs and list filenames raise uncaught exceptions;
bool and fractional IDs become integer 1; an oversized integer reaches the core;
client-supplied old document SHA is ignored. The framework frame limit is 1048576 bytes.
The upload command reads the current SHA itself, so its signature comparison does not
prove that the customer saw that version. A mock-DB probe of the actual signature writer
requests four grant inserts across two uploads while nine other documents remain pending.
Neither partial upload requests an audit insert. This is not a live DB row-count claim.

Recommended order: validate upload message types/ranges; bind the client's viewed SHA
under the existing lock; define repeat-upload behavior and audit each partial success.
Formal intake is a separate scope: the UI sends only a filename, the core records both
roles automatically, and issued SHA values are demo identifiers rather than content digests.
Before real intake, isolate that mock signing action and establish authenticated actors,
role-specific consent, persistent rate limits and actual file/storage/signature verification.
OCI edge controls and browser network behavior were not tested. No real file bytes are
accepted today, so a suspicious filename alone is not evidence of filesystem traversal.
The user subsequently selected internal demo scope; the acceptance section above records that decision.

Actual probe scripts, outputs and test commands:
`data/evaluations/security-04-06-20260906.md` (local audit artifact).
Corrected regression baseline: 13 failed, 81 passed, zero skips, in 0.52 seconds.
Final command: `.venv/bin/python -m pytest tests/test_identity_inventory.py tests/test_identity_gate.py tests/test_document_flow.py tests/test_acceptance_fixes.py -q -ra --tb=short`.
Result: 274 passed, 0 skipped, 0 deselected, in 10.56 seconds; Ruff passed.
This includes existing DB upload ownership, illegal-stage and rollback tests; it does
not turn the uncovered 06 probe findings into passing coverage.
The service was active, TCP 5434 connected and SELECT 1 succeeded before DB tests.
After cleanup: policy=288, clause=11775, contract_clause=10512, doc-prefixed fixture members=0.
No full suite or corpus-loader tests ran. User fixes 02/05b and other workers' diffs were not edited.

## Rejected names and duplicate documents — 2026-09-06, 00:06

The second pass separates unresolved labels from real same-name documents.
The positive title selector rejected the reported section headings, but the refresh
fallback kept their old labels. That fallback is now an explicit `商品名稱待核對`.
The PDF builder uses the same label instead of a filename. Document IDs remain separate;
the shared label does not assert that unresolved documents describe the same product.
An explicit `本公司『…』(以下簡稱本商品)` declaration supplies a disclosure's product name.
Mere examples, references and conflicting declarations do not.

Applied 166 name-only updates: 11 recovered names and 155 explicit unresolved labels.
The 155 comprise 152 unknown sources and 3 brochures. All 299 contract names are unchanged.
The reported vendor-heading, carbon-label, health-promotion and two fund-heading groups
now have zero old labels. Footer names and padded names remain zero. No rows were merged
or deleted. No unresolved name appears in sale_catalog; all 288 held policies still
reference contract sources. This does not mean all 660 records have verified names.
Old names, IDs and source URLs remain in `data/evaluations/product-name-review-20260906.json`.
Actual SQL and raw output are in `data/evaluations/corpus-sql-audit-20260906.txt`.
These are local audit artifacts. FU-2026-09-05-ac4c0e84b442 remains open for manual review.

Before: 660 rows, 448 distinct names and 170 duplicate-name groups.
After excluding the unresolved label: 505 named rows, 320 distinct names and 161
duplicate-name groups. Of those groups, 155 mix source kinds; five contain multiple contracts.
These counts are not a deduplication score. The shared unresolved label must be excluded.

The two requested examples were checked against local PDF pages:

- 實全心意 has two 8-page contracts: 84844c349049 is filed on 109.04.28;
  c9bcc741c6c6 adds 112.02.08 and 113.07.01 revisions. The other two records are
  2-page introductions: 7f54efbd9e21 is 2023-07/CV1; 55361627506e is 2024-07/CV2.
  Each introduction contains M10/M20/M30; these are not separate coverage-band PDFs.
- 真富利雙享 has a 39-page contract (8bad693d64b3), a 737-page product explanation
  (1002c5a77160, issued 111-10), and a 4-page introduction (6c665b43a75a, 2022-10/V63).
  Keeping these source records is appropriate; calling them three contracts is not.

An exception prevents claiming all five contract pairs are different versions:
新吉美發 6e7ec357daa7 and b6d2e482c470 have identical full extracted text after
whitespace removal. Their raw extracted text differs. This does not compare images or
signatures. The demo prices remain 39160 and 17160; a PDF hash is not evidence for
a version-price difference. FU-2026-09-06-39c5a044dc8d tracks catalogue identity review.
No document or price was merged in this name-only patch.

Clause count 11775 and policy count 288 are unchanged. Before/after fingerprints match:
clause product/ID/heading/text/page=df9e6247c510463b0794025eef812081;
policy ID/member/product/cover=d9723e35e019d25dd3eaeed6f46a8cb6.
contract_clause remains 10512 rows, maximum 54451 characters, zero over 100000.
This patch does not change clause boundaries, text, source kinds or vector inputs.
No vector rebuild is required for this patch. The raw brochure-boundary follow-up stays open.
The old tracked SQLite cache remains unchanged; importing it still requires names-only refresh.

Regression baseline: 9 failed, 80 passed. After the fix, source tests and DB reads passed.
Final command, after applying the metadata update:
`.venv/bin/python -m pytest tests/test_clause_index.py tests/test_quote.py tests/test_product_clauses.py tests/test_corpus_loader.py::test_copy_corpus_legacy_ideographs_are_normalized_before_postgres tests/test_corpus_loader.py::test_refresh_product_names_printed_title_updates_only_unchanged_source tests/test_corpus_loader.py::test_refresh_product_names_unresolved_is_reported_not_guessed tests/test_corpus_loader.py::test_refresh_product_names_unresolved_marker_is_idempotent tests/test_corpus_loader.py::test_refresh_product_names_same_product_different_sources_keeps_both_records -q -ra --tb=short`.
Result: 210 passed, 0 skipped, 0 deselected, in 18.04 seconds. Ruff passed.
The service was active; TCP 5434 and SELECT 1 succeeded before DB tests.
Only named isolated/mock corpus-loader cases ran, not that whole file or the full suite.

## Corpus names and demonstration rates — 2026-09-05, 23:43

The old title selector accepted the first readable line after a furniture denylist.
Readable footers, carbon-label numbers and section headings therefore became product names.
The replacement requires printed title evidence, handles bounded wrapped titles and
qualifiers, and leaves multiple-product or unreadable covers unresolved rather than guessing.
It changes title metadata only, not article boundaries, clause text or document kinds.

Inspected all 660 local PDFs and applied 195 name/padding corrections to Postgres in
one transaction guarded by product ID, document SHA and the previously read name.
Footer names: 62 -> 0. Names with leading/trailing spaces: 24 -> 0.
All 299 contract documents resolve. Another 166 documents remain unresolved:
163 unknown and 3 brochure. Their existing stripped labels remain, including section
and fund-disclosure labels; this is not a claim that all 660 names are now correct.
The full changes and unresolved IDs/source URLs are in
`data/evaluations/product-name-review-20260905.json` (local audit artifact).
SQL and raw before/after output are in `data/evaluations/corpus-sql-audit-20260905.txt`.
FU-2026-09-05-ac4c0e84b442 remains open for the unresolved documents.

Health demonstration premiums previously used a daily-benefit-sized range against
generic insured-amount units. They now use 10-50 per 1,000 insured amount (1%-5%),
without changing policy cover or claiming these are insurer tariff rates.
The live catalogue has 77 health contract entries in that range.
Five pairs of same-name on-sale contract records still exist; the product-ID hash still
explains differences in their synthetic premiums. They were not merged or deleted by name.
Those five pairs are not the same population as the original 58 duplicate-name groups.

Before/after probes match: clause=11775, policy=288;
clause product/ID/heading/text/page fingerprint=df9e6247c510463b0794025eef812081;
policy ID/member/product/cover fingerprint=d9723e35e019d25dd3eaeed6f46a8cb6.
The title-only parser test also checks unchanged clauses, document ID and kind.
Vector source queries read contract clauses, not product names or catalogue rates;
this change does not require rebuilding the user's new Cloudflare index.

Boundary correction: contract_clause has 10512 rows, maximum 54451 characters and
zero over 100000; raw clause still has 40 brochure rows over 100000, maximum 420375.
FU-2026-09-05-84ab360decab remains open: retrieval isolation is not a raw parser fix.

Final targeted command: `.venv/bin/python -m pytest tests/test_clause_index.py tests/test_quote.py tests/test_payment.py tests/test_conversation_memory.py tests/test_corpus_loader.py::test_copy_corpus_legacy_ideographs_are_normalized_before_postgres tests/test_corpus_loader.py::test_refresh_product_names_printed_title_updates_only_unchanged_source tests/test_corpus_loader.py::test_refresh_product_names_unresolved_is_reported_not_guessed -q -ra --tb=short`.
Result: 189 passed, 0 skipped, 0 deselected, in 20.58 seconds; Ruff passed.
The service was active and TCP 5434/SELECT 1 succeeded before testing.
Only four isolated/mock corpus-loader cases ran, not that whole file or the full suite.

The tracked SQLite corpus cache is unchanged. A full PDF rebuild uses the new selector;
importing the old cache directly can restore old names and must be followed by
`policydesk-ingest --names-only` against the source PDFs. Updating/committing that
83 MB cache is pending the user's choice, not silently included in this patch.

Document changes were committed in 2e061a0 and health rates in 9df66a0.
The document stash ae26e7629e6e88dca88fad665041979c77741a5c was reviewed against
the completed document code/tests and dropped with the user's authorization.
The unrelated cost-work stash remains. Both feat/desk-cost-and-hardening and
feat/desk-ui-motion have zero commits outside main and no attached worktree;
there is no identified reason to retain those branches, but they were not deleted here.

## Document transactions completed — 2026-09-05, 23:10

The upload-refusal and transaction work left unfinished in 75e3069 is now implemented.
The confirmed socket passes its session case ID to one core upload command.
That command validates before writing the filename and both signature roles in one transaction.
True refusals leave no changes and produce a warning, including refusals without a missing list.
Partial signatures and failed identity checks retain their existing persistence semantics.

All stage commands lock the case before its documents, in document-ID order.
Opening a case first locks the member, including when no case exists yet.
Snapshots use the same case lock to avoid mixing document and stage generations.
The transaction session never acquires another connection or retries individual statements.
Transaction control completes before pool release, including repeated cancellation during BEGIN and rollback.
Cancellation after COMMIT starts can still commit; the caller must read back the outcome before retrying.
This remains a filename-only demo, not verification of file bytes or cryptographic signatures.

Baseline: both illegal-stage socket uploads failed by changing uploaded_name.
Four new tests also failed before implementation: missing atomic upload, exception rollback,
cancellation rollback, and concurrent duplicate document issuance.
Final command: `.venv/bin/python -m pytest tests/test_document_flow.py tests/test_acceptance_fixes.py tests/test_desk_ui.py tests/test_identity_gate.py tests/test_identity_inventory.py tests/test_db_fixture.py -q -ra --tb=short`.
Result: 303 passed, zero skipped, zero deselected, in 11.66 seconds.
The checks include controlled two-connection issuance, last-upload and decision races.
A process-local mutation bypassing the issuance transaction made the stage-race test fail:
an approved case incorrectly moved back to issued. The source files were not mutated.
Ruff and diff whitespace checks passed. No full suite or corpus-loader test ran.
Service status was active; TCP 5434 and SELECT 1 succeeded before testing.
After cleanup: policy=288, clause=11775, contract_clause=10512, doc-prefixed fixture members=0.
No tool schema, executor, memory or retrieval code changed in this work.

## Document status progress and remaining upload failure — 2026-09-05, 18:10

The document stash was restored without changing the corpus or rebuilding the container.
Current-version grants now determine the displayed signing state, pending files and review completeness.
Required but absent forms remain explicit in `document_status`; no fake document IDs are created.
The board, document table, modal and pending count read that shared status.
The public `commands.DocumentKind` import has a regression test; an explicit module alias survives Ruff's import cleanup.

Document, acceptance, UI, identity-gate and identity-inventory checks: 243 passed, two known upload tests deselected.
The two deselections are failures, not unavailable infrastructure or passing coverage; see below.
The new status assertions first produced 11 failures. The UI initially produced two failures and one pass.
UI tests execute the page's original render functions with Node and a minimal DOM adapter.
They require Node.js on PATH and do not establish browser layout or network behavior.
A process-local mutation removing grant-to-current-SHA matching made the changed-version review test fail.
The mutation advanced stale evidence to REVIEW, so the guard is exercised. No source mutation persisted.

Two actual customer-handler tests remain red at `test_customer_socket_upload_illegal_stage_reports_refusal_without_writes`.
After real identity confirmation, upload at PROPOSED or VERIFIED changes `uploaded_name` despite refusing the signature.
The handler writes before validation and hides refusals that have no `missing` list.
These tests remain in the working tree for the next fix; do not call the whole document file green.

The next change needs one transaction-bound query session and a core upload command for metadata plus both roles.
The proposed lock order is case row, then that case's document rows in document-ID order.
All competing stage writers must read their preconditions under the same case lock, not only signing and submission.
Transaction tests must cover partial-signature commit, mid-write rollback, cancellation and controlled two-connection races.
A partial signature currently returns Refusal with missing forms but intentionally persists; it must not roll back as a true refusal.
File bytes and cryptographic signatures are still not verified by this filename-only demo.
The transaction design is source-reviewed, not implemented or empirically verified.

At 18:06 the read-only vector audit reported 11724 source documents and 23588 vectors.
Missing, stale and uncovered were zero; source_matches, matrix_valid and complete were true.
The encoder remains llama-server 8090, bge-m3-Q5_K_M.gguf; generation and source hash match the 17:29 record below.
This establishes coverage of DB sources, not correct extraction of every PDF attachment.

## PostgreSQL after reboot — corrected 2026-09-05

`policydesk-pg` runs under `/usr/bin/podman`, not Docker Desktop.
The user installed `~/.config/systemd/user/policydesk-pg.service` to start the existing container automatically.
Both `systemctl --user is-active policydesk-pg.service` and `is-enabled` were verified after installation.
If it is stopped, use `systemctl --user start policydesk-pg` or `/usr/bin/podman start policydesk-pg`.
Inspect that exact container before starting it. Docker Desktop WSL integration is not required.
The corpus lives in this container's writable layer. Do not recreate the container or replace this setup with Quadlet.
The service uses `podman start` deliberately; recreating from the image would discard that corpus.
Before DB tests, verify port 5434 and `SELECT 1` through the project's `Database` class.
Connection failure must stop the DB test run; skipped tests are not integration evidence.
The current recovery was verified with container state `running` and `SELECT 1 = 1`.

The user independently verified clause=11775, contract_clause=10512, policy=288 and llm_usage=2181 after recovery.
Their compatibility-ideograph count remains 0 and fullwidth-punctuation clause count remains 11656.
Build Unicode measurement ranges from code points, such as `chr(0xF900)` and `chr(0xFAFF)`.
Check both a matching and a nonmatching character before trusting a count.
Literal compatibility ideographs can change during command transport and invalidate the measurement.

During cross-member tests, other workers must not run `test_corpus_loader.py` or the full suite.
Use unique fixture identifiers and clean only those fixtures in `finally`, including their usage records.
Check fixture absence after cleanup so later corpus counts do not include test records.

## DB test outages now fail — 2026-09-05

All test pool construction now goes through `tests/conftest.py:connected_database`.
The shared module fixture checks `SELECT 1`; connection or construction failure raises
`pytest.fail`, with instructions to check DATABASE_URL and start the existing container/service.
The failure is raised outside the caught connection exception so pytest does not print a potentially secret-bearing DSN.
Pools close after successful tests, failed tests and failed connectivity checks.
Statute bootstrap wrappers keep their initialization, and ownership fixtures keep independent pools and exact cleanup.
Skips for absent scenario data or optional retrieval artifacts remain; they are not connection-failure skips.

Actual failure probe: run `test_payment.py::test_the_grace_rule_is_116_and_only_116`
with process-local DATABASE_URL pointing to localhost port 1. Result: 1 error, exit 1, zero skips,
with both recovery commands visible and no original exception chain. The live DB was not stopped.
Normal verification: `test_db_fixture`, `test_acceptance_fixes`, `test_payment`,
`test_conversation_memory`, `test_beneficiary`, and `test_reinstate`: 171 passed, zero skips.
The identity/validator/executor-citation/product-clauses/exercise regression run: 236 passed, zero skips.
Ruff and diff whitespace checks passed. No corpus-loader test or full suite ran.

## Live boundary baseline — 2026-09-05

The four new cases have now run through real WebSockets, PostgreSQL and the deployed
Codex CLI provider (gpt-5.6-luna, reasoning effort low), three repetitions each, 27 turns total.
Reports are `data/evaluations/live-boundaries-20260905-r{1,2,3}.jsonl`.
Each fixture was removed by its exact generated identity after its run; policy count returned to 288.
Impostor and off-topic checks passed all six turns each. Restated used budgets 20000, 5000, 5000
in each repetition, but four of its nine answers were withheld by quotation checks.
One inspected raw completion omitted source conditions inside a claimed verbatim quotation;
this is not the punctuation-width false alarm. Full long completions cannot always be reconstructed
because the usage recorder truncates them; quotation generation and trace completeness remain open.
Locked-out checks originally passed, but all six replies asked for another ID despite the connection lock.
The new lock-state checks expose that false green. State propagation and guidance fixes have narrow
unit coverage, but their live-model post-change measurement has not yet run; do not claim the fix empirically verified.

### Locked-state follow-up — 2026-09-05

The post-change run uses the same deployed CLI alias and effort, with Codex CLI 0.153.4.
Reports are `data/evaluations/locked-state-fixed-20260905-r{1,2,3}.jsonl`.
Each repetition asks the original two private questions, then a public life-product catalogue question.
All six private replies now explain the connection lock and direct verification to service staff without requesting another ID.
The original scorer missed the phrase `服務人員`. Its added regression failed before the scorer fix and passed after it.
Only that handoff check was rescored from the saved replies; the original frame/privacy checks remain unchanged.
The six private replies pass after rescoring, versus zero of six baseline replies on the lock/handoff criteria.

All three public replies select browse_products and preserve the recorded privacy/no-retry checks.
Their five product names, premiums, pricing unit, catalogue count and demo disclosure match the actual catalogue tool output.
They do not repeat the lock/manual-handoff instruction on that public-only turn, so the original all-turn lock scorer still fails there.
This establishes public-query availability, not compliance with every next-step instruction or coverage of all scenario modules.
No raw reports were overwritten. Each fixture was removed; policy count returned to 288.

Long-completion diagnosis uses a separate process and RAM-only capture at the provider seam.
It does not extend DB retention or console visibility. Existing truncated historical completions cannot be recovered.
The real handler, DB tools and hybrid retriever remain in use; only socket delivery is replaced with a discard sink.
This diagnostic path does not itself test socket authentication or browser behavior.

### Quote experiment, outage and answer-quality findings — 2026-09-05

A diagnostic run completed one three-turn restated conversation: two answers failed quote checks, one passed.
All five failing quotes had visible sources matching DB text; none matched another visible source or passed by changing 30 to 三十.
The second repetition stopped on a real DB connection refusal at 16:55. Its cleanup also failed at that time.
Systemd had restarted the existing container by 16:56:17; its journal records a scheduled restart.
No container/service change was made by this run. The one known leftover fixture was then removed by exact identity.
The final check found no quote-probe or quote-pair members; policy=288, clause=11775, contract_clause=10512.

The subsequent paired experiment ran three identical-input A/B pairs with the same CLI alias, effort and hybrid retriever.
Only the quote-schema description changed. Arm order was A/B, B/A, A/B.
Both descriptions passed quote checks in all three pairs (A: 26 quotes; B: 33 quotes).
The control passed throughout, so this probe did not establish an improvement. The experimental wording and its test were removed.
This is not evidence that wording cannot help, or that delivered summaries preserve every condition.
Full completions remained in RAM and were discarded; only structural diagnostic metrics were emitted.

Independent review of the nine saved restated replies found four withheld replies and five delivered replies.
All five delivered replies use the correct current budget, but three have confirmed answer-quality defects.
The second repetition's first reply ranks a 1950-per-unit product above a 1610-per-unit rider for affordable units.
At budget 20000 those prices allow 10 versus 12 units, so the comparison direction is wrong.
The last replies of repetitions one and three list cosmetic-surgery exclusions but omit the reconstruction-of-basic-function exception.
All five currently cited exclusion sources contain that exception. Source presence alone does not validate summary completeness.
The second repetition's second reply calculates amounts without recorded calculator evidence; the arithmetic itself is correct.
These findings remain open. Do not label the restated scenario fully verified from its correct router parameters.

### Retry-log privacy — 2026-09-05

The outage exposed SQL parameters, including model reply text, through stamina's default retry logging.
The DB now sanitizes retry hook details for its four exact retry operations using stamina's public hook interface.
SQL and parameters are removed; the replacement exception keeps its type but has no message, attributes, cause or traceback.
Attempts, wait timings, other hooks and non-DB events remain intact. The original exception still reaches its caller unchanged.
Registration is process-wide; subsequent replacement of global hooks requires reapplying this protection.
This does not sanitize arbitrary exception logging elsewhere or change retained model responses.
The focused mock run passed 32 tests; fault probes verify recovery, exhaustion, nontransport failures and secret absence.

### Contract appendix coverage — 2026-09-05

The old 40 clauses above 100000 characters belong entirely to brochures, not contract_clause.
Current contracts contain 10512 clauses, with maximum length 54451 and 45 clauses above 10000 characters.
Fresh parsing of contract products ab58d5514acc/art.47, 207b27602262/art.37 and 043dd0fabbe4/art.41
reproduces the stored last-clause text exactly: 54451, 38896 and 336 characters.
These last clauses are titled 管轄法院 but include following attachments. The parser ends the last article at the PDF's end.
A future structural split must retain those attachments as searchable, citable contract evidence rather than discard them.
No corpus rewrite, embedding rebuild or appendix fix ran in this investigation.

## Budget facts and live reply limits — 2026-09-05, 17:29

The selection tool now returns calculator-derived whole pricing units, annual premium and both expressions.
These describe unit-rate arithmetic, not underwriting limits or comparable coverage amounts.
Riders keep their rate but return `main_contract_cost_unknown`, without an affordable-unit count.
Missing or nonpositive pricing bases and nonpositive premiums return `pricing_basis_unavailable`.
No prompt, stored clause or index changed for this calculation fix.

The seven initial regressions failed before implementation and passed afterward.
Further tests cover negative bases, decimal rates, distinct unit labels and two real-catalogue budgets.
An isolated mutation adding one unit produced three failures; no mutation reached the service or source file.
Final quote/calculator/identity-inventory/scoping run: 176 passed, zero skips.
The earlier quote/retrieval run passed 83 tests; four later quote tests are covered by the final run.

The old scoping assertion required ASKED_ALREADY inside IDENTITY_PENDING.
The rule now reaches both model phases through their shared unverified context.
Four actual run_turn tests replace the source-location assertion, covering pending/locked and both answer paths.
They prove rule delivery, not model compliance. No prompt wording changed.

Real WebSocket reports: `data/evaluations/budget-facts-20260905-r{1,2,3}.jsonl`.
These use the deployed Codex CLI path, gpt-5.6-luna, reasoning effort low, with PostgreSQL and hybrid retrieval.
All nine turns route with the current budget: 20000, then 5000, then 5000.
Seven replies are withheld by quote checks. Two replies pass the mechanical checks, both in repetition two.
The delivered comparison states two main-contract units cost 3900 and that riders need an unknown main-contract cost.
Its cited exclusions cover five products with a basic-function reconstruction exception, which its summary omits.
The other delivered reply confirms the 5000 budget without repeating the comparison.
This is not a successful end-to-end recommendation result or a controlled A/B improvement claim.
Reply latency: median 58.29 seconds, maximum 71.32 seconds.

Each generated fixture and its usage rows were removed by exact identity after its repetition.
Final independent check: no fixture members; policy=288, clause=11775, contract_clause=10512.
The owned localhost:8101 test server stopped after the run. PostgreSQL and its systemd service were not changed.
The latest read-only vector audit reports missing=0, stale=0, uncovered=0, source_matches=true and matrix_valid=true.
It identifies generation 20eecc84c55a43e98c1f29ce8a5ef2cd, 23588 vectors and 11724 source documents.
Its source_sha256 is 42a128e71b9c0578d085967074ef61c026c2222aba339a9db29f08289cfbea61.
The encoder is llama-server 8090, bge-m3-Q5_K_M.gguf, with 256 tokens and 48 overlap.
This audit covers current DB sources, not completeness of extraction from every PDF.

### Document workflow gap found during this run

Source inspection shows upload sends a filename and document ID, not file bytes.
The handler reads the stored document SHA and records grants for both signing parties.
Issued SHA values are case/kind identifiers rather than digests of issued bytes.
record_signature checks document ownership but does not compare the supplied SHA with the current document SHA.
Its party count ignores SHA; submit_for_review trusts signed_at without rechecking document versions.
The stage check also follows signature writes, so a refusal can follow persisted side effects.
These are source-confirmed gaps, not yet reproduced through real command lifecycle tests.
Current upload tests mock record_signature and therefore do not cover those gaps.
The next document tests need matching, wrong and superseded versions, plus refusal-without-write assertions.
Do not describe the filename-only demo path as file-content or digital-signature verification.

### Paused document experiment and comparison probes — 2026-09-05, 17:50

The user requested logical commits followed by positive cross-member ownership verification.
Document changes are parked in stash commit `ae26e7629e6e88dca88fad665041979c77741a5c`, not in the working tree.
The stash contains commands, a shared documents module, pending-signature queries and lifecycle tests.
Original lifecycle tests reproduced five failures and two passes; two later missing-document tests also failed before implementation.
The experimental implementation reached 144 passes and one acceptance failure across three selected files.
That acceptance test still treats a timestamp as sufficient signature evidence.
The experiment also leaves missing-document UI counts and concurrent version/stage changes unresolved.
It is unfinished; restoring the stash requires integration with the later tools commits, not a container or corpus rebuild.

Two RAM-only CLI probes finished without changing production prompts or reasoning defaults.
Three low/medium reasoning pairs passed all quotation checks in one and two answers respectively.
All six summaries omitted the basic-function reconstruction exception.
Three separate control/fidelity-rule pairs passed all quotation checks in two and one answers respectively.
Only the first fidelity-rule answer named the reconstruction exception; later answers merely referred to unspecified exceptions.
These small, mixed results do not establish a reliable improvement or complete summary fidelity.
Both probes used the actual handler, PostgreSQL tools and hybrid retriever, with a discard socket sink.
Each fixture and its usage were removed; policy count returned to 288. No full completion was retained.

## Ownership, collision and quote-width verification — 2026-09-05

The focused run passed 210 tests with zero skips: `test_identity_gate`, `test_validator`,
`test_executor_citation`, and `test_product_clauses`. Ruff and diff whitespace checks passed.
No corpus-loader test or full suite ran.

Ownership tests use real PostgreSQL, the customer handler, executor and tool queries, with scripted model completions.
Each run creates two UUID-named members with separate policies, claims and beneficiaries on the same product.
Returned record IDs are resolved back to DB owners, without supplying the expected owner in the lookup filter.
Each tool result and each case snapshot must independently be nonempty and contain the complete expected set.
Changing the actual gather member to the other member produced two failures; restoring it passed both directions.
Emptying only the overview tool result also produced two failures, despite populated case snapshots.
Both mutations were removed. Cleanup after passing and failing runs left policy=288, llm_usage=2181 and no fixture members.
This proves DB routing and record isolation, not real-model routing or end-to-end browser behavior.

### Positive ownership acceptance re-run — 2026-09-05, 17:57

Completed changes were committed by topic before this acceptance pass.
The positive test existed only in the working tree; it is separate from the unconfirmed sentinel tests.
It now requires every reply to include the owner's returned policy numbers, not merely a fixed success sentence.
The scripted provider echoes real query values only after checking their presence in the actual model input.
Additional checks match case IDs, the identity-confirmed audit row and beneficiary details against PostgreSQL.
Beneficiary ownership uses returned policy IDs because that tool does not expose beneficiary record IDs.

The fixed-success reply failed both directions under the new assertion; the query-backed reply passed both.
A process-local probe then replaced the member ID passed from the real handler into run_turn with the other fixture member.
Both directions failed at the independent policy-ID-to-DB-owner assertion, not at a mock identity field.
After that probe exited, the identity-gate/inventory/beneficiary/claim-status run passed 182 tests with zero skips.
Final DB counts were policy=288, clause=11775 and contract_clause=10512, with zero auth-prefixed fixture members.
Fixture teardown also checks that cases, claims, beneficiaries, conversation and usage records are absent.
No authentication, tool query, corpus or index implementation changed for this acceptance test.
The model completions and socket transport remain scripted; this is not live-model or network WebSocket evidence.

Ambiguous names now render exact candidate numbers and full product names directly from selection data.
Quote-width normalization exists only in comparison keys; database text, displayed quotes and embedding input remain unchanged.
The quote check still tests substring presence, not the semantic fidelity of a selectively shortened quotation.

## Commit-time status after reboot — 2026-09-05

The user independently accepted items 1–6. Item 7 is split into logical commits on `feat/retrieval-hardening`.
PostgreSQL on port 5434 was unavailable during those commits. The recovery cause is corrected above.
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
