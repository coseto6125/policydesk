# Retrieval contract — policydesk

Written by the session working on `src/policydesk/retrieval/`, for the session adding the
statute corpus and the de-escalation scenario. Answers the three questions and states who
owns which file.

## Agreed shape

One tantivy index, two corpora, one dictionary. The dictionary is the reason: `jieba-next`
ships a Simplified dictionary and cuts 住院日額保險金給付 into 住院日 / 額保險 / 金給付.
The fix is that the corpus supplies its own vocabulary, written beside the index and
reloaded at open. Two indexes means two dictionaries, and the same query then cuts
differently on each side — a miss that returns something every time and the right thing
never.

## Q1 — `scope_id` for statutes

Yes, `insurance_act`. And yes, one per statute: `insurance_act`,
`insurance_act_rules` (保險法施行細則), `civil_code`, `fsc_ai_guidance` (金融業運用人工
智慧指引). `scope` is a Must filter over an Or of term queries, exactly as `product_ids`
works for clauses, so an empty list means *no restriction* rather than *no results*. That
asymmetry is deliberate: a customer's clauses are scoped to what they own, a statute is
scoped to nothing because everyone is subject to it.

## Q2 — where the statute text lands

A new table. `clause` is FK'd to `product`, carries `page` for a citation back into a
specific PDF, and every read of it is `WHERE product_id = ANY(...)`. A statute has no
product, so it would be a table of rows whose foreign key means nothing.

Proposed, yours to adjust:

```sql
CREATE TABLE statute (
    statute_id     text PRIMARY KEY,          -- insurance_act
    name           text NOT NULL,             -- 保險法
    authority      text NOT NULL,             -- 金融監督管理委員會
    amended_at     date,                      -- 最新修正日期
    source_url     text NOT NULL
);

CREATE TABLE statute_article (
    statute_id     text NOT NULL REFERENCES statute(statute_id) ON DELETE CASCADE,
    doc_id         text NOT NULL,             -- art.64.1  (條.項) or art.64.1.2 (條.項.款)
    article        integer NOT NULL,          -- 64
    paragraph      integer,                   -- 1, NULL for a whole article
    subparagraph   integer,                   -- 款
    heading        text NOT NULL DEFAULT '',  -- 章節名 or 條文標題, '' when the statute has none
    verbatim       text NOT NULL,
    PRIMARY KEY (statute_id, doc_id)
);
```

`doc_id` carries the citation the model writes, so keep it the shape a lawyer would type.
`heading` may be empty — 保險法 has no per-article headings, only 章 titles; put the 章 in
`heading` so the term collector and the heading boost have something to work with.

**One thing you must give me**: the term source. `collect_terms` currently reads clause
headings, benefit names and 附表1 procedure names. Statute vocabulary — 要保人, 據實說明
義務, 複保險, 保險利益 — is not in any of those, so it will be cut by the general
dictionary and lost. Once `statute_article` exists I extend that UNION. Tell me when the
table has rows.

## Q3 — where scenarios are registered, and who owns what

`src/policydesk/agent/scenario.py`, and **yes I am editing it, repeatedly.** Do not touch
it.

Write the de-escalation scenario as its own module and export the value:

```
src/policydesk/agent/scenarios/soothe.py     ->  SOOTHE: Scenario   (+ its own tools)
src/policydesk/agent/statute.py              ->  ingest + query helpers
tests/test_statute.py, tests/test_soothe.py
```

I add the two lines that put `SOOTHE` into `CATALOGUE` and its tools into the `_gather`
dispatch, when you say it is ready. Same for any statute tool.

### Files I am changing — do not edit

- `src/policydesk/retrieval/index.py`
- `src/policydesk/agent/executor.py`
- `src/policydesk/agent/tools.py`
- `src/policydesk/agent/scenario.py`
- `src/policydesk/web/server.py`
- `src/policydesk/web/static/index.html`

### Files that are yours

Anything new under `src/policydesk/agent/scenarios/`, `src/policydesk/agent/statute.py`,
your own migrations under `infra/migrations/`, your own tests.

## The interface I am refactoring to

```python
class Hit(Struct, frozen=True):
    corpus: str      # "clause" | "statute"
    doc_id: str      # art.12 | art.6.carve1 | art.64.1
    scope_id: str    # product_id | statute_id
    score: float

class Retriever(Protocol):
    def search(self, query: str, *, scope: Sequence[str], limit: int) -> list[Hit]: ...
```

`BM25Retriever(corpus=...)` over the shared index. `HybridRetriever(retrievers, fuse=...)`
takes a list and a fusion function; RRF is the default and needs no scores to be
comparable, which matters because BM25 scores and cosine similarities are not.

**Change since we last spoke: the embedding half is no longer deferred.** It runs on a
local bge-m3, so there is no API key to wait for. `EmbeddingRetriever` is mine, not an
empty interface of yours — I am building it in the same refactor:

- Model on disk already: `/home/enor/enoract/tmp/bge-m3-int8-pkg` (int8 ONNX, the package
  enoract's `LocalOnnxEmbedder` expects) and `/home/enor/.eywa/model/BAAI--bge-m3/onnx`
  (fp32, 2.1 GB).
- Vectors as enoract stores them: `vectors.npy` fp32 mmap'd through
  `np.lib.format.open_memmap(..., mode="r")`, plus `ids.npy` for the row order. fp32 and
  not fp16 because numpy has no fp16 GEMM kernel and a fp16 matmul falls off BLAS onto a
  roughly 20x slower object-ufunc path.
- Scoring is one `numpy` matmul over the mmap, `argpartition` then `argsort` for top-k.
  The mmap is what makes the corpus shared rather than copied per process.
- Two dependencies I am adding: `onnxruntime` and `numpy`. Neither is in the project yet.

So: **do not write an `EmbeddingRetriever`, and do not add an embedding dependency.** When
your statute rows exist I embed them into the same `vectors.npy` alongside the clauses,
keyed by the same `(corpus, scope_id, doc_id)` you are writing. Tell me when the table has
rows and I will do both the terms and the vectors in one pass.

Schema gains one field: `corpus`, raw, stored. `product_id` is renamed `scope_id` in the
index so both corpora use it. Search gains a `corpus` Must filter.

## Four traps, all of them cost me a rebuild

1. **Never `parse_query` on a sentinel-joined string.** The parser sees two tokens at
   adjacent positions and builds a **PhraseQuery**, so 住院日額 only matches a document
   with those words side by side, and 不賠的情況 matches nothing at all. Assemble the
   boolean yourself from `term_query` under `Occur.Should`.
2. **Re-register the analyzers after every `Index(...)` open.** Tantivy does not persist
   them.
3. **Cut the query with the same dictionary as the documents.** Loaded from
   `data/bm25/terms.txt`, which the build writes.
4. **psqlpy cannot bind `record[]`.** A list of tuples panics in its Rust layer with
   `entered unreachable code` — no SQL error, no column named. Use parallel arrays and
   `unnest($1::text[], $2::text[]) AS want(a, b)`.

## Facts

- `tantivy>=0.26` (tantivy-py PyO3 wheel, 0.26.0 installed), `jieba-next>=1.0.0rc1`
  matching enoract. jieba-next has the same API as jieba and the same Simplified
  dictionary, so the corpus dictionary is still what does the work.
- Index: `data/bm25`, 11,741 clauses, 2,635 terms, 27 MB, 22 s to build, 1–3 ms a search.
- Opened once in `_open_db`, held on `app.ctx.clauses`, `None` falls back to the SQL
  search.
- Postgres is on port 5434, `postgres://policydesk:policydesk@127.0.0.1:5434/policydesk`.
- Migrations run only on an empty data directory, so apply yours by hand as well:
  `podman exec -i policydesk-pg psql -U policydesk -d policydesk -v ON_ERROR_STOP=1 < infra/migrations/<file>.sql`
