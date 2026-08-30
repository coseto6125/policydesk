"""
Measure what each retrieval channel finds, against a gold set the code already asserts.

Nothing here is labelled by hand. Every statute pair comes from a scenario module that
names the provision it is about and lists, in its own `description`, the sentences a
customer says to reach it — one source for the question and the answer both. Every clause
pair comes from a tool's own heading filter: `reinstatement_clauses` selects on
`復效|效力停止|恢復效力`, so for a reinstatement question those clauses ARE the right
answer by the desk's own definition, and recall is measured against that set.

Run: `uv run python scripts/recall.py`
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from policydesk.core.db import Database  # noqa: E402
from policydesk.retrieval.base import CLAUSE, STATUTE, HybridRetriever  # noqa: E402
from policydesk.retrieval.index import open_index  # noqa: E402
from policydesk.retrieval.vectors import open_vectors  # noqa: E402

# (question a customer asks, article the scenario says answers it)
STATUTE_GOLD: list[tuple[str, tuple[str, ...]]] = [
    ("保單停效多久內可以復效", ("art.116",)),
    ("我這期保費忘記繳會怎樣", ("art.116",)),
    ("停效之後還要繳保費嗎", ("art.116",)),
    ("我有據實說明啊你們憑什麼解約", ("art.64",)),
    ("健康告知沒寫到會怎樣", ("art.64",)),
    ("投保時沒講的病現在要補說嗎", ("art.64",)),
    ("我要改受益人", ("art.110", "art.111")),
    ("受益人可以填誰", ("art.110",)),
    ("沒有指定受益人的話保險金給誰", ("art.113",)),
    ("受益人比被保險人早過世怎麼辦", ("art.112", "art.113")),
    ("我換工作要通知保險公司嗎", ("art.59",)),
    ("職業變更會不會加保費", ("art.59",)),
    ("可以用保單借錢嗎", ("art.120",)),
    ("我要申訴要多久內提出", ("art.13",)),
]

# (question, the heading filter the tool uses to answer it)
CLAUSE_GOLD: list[tuple[str, str]] = [
    ("保單停效之後怎麼復效", "復效|效力停止|恢復效力"),
    ("理賠要準備哪些文件", "申領|保險金的申請|檢具|應檢附"),
    ("我這張保單保什麼", "保險金|保險範圍|承保範圍"),
]

K = (1, 3, 5)


def _scores(ranked: list[str], gold: set[str]) -> tuple[dict[int, float], float]:
    """
    Score one ranking.

    Args:
        ranked: doc_ids, best first.
        gold: the ids that answer the question.

    Returns:
        recall@k for each k, and the reciprocal rank of the first correct hit.

    Recall here is "did any correct document reach the top k", which is what matters to
    a desk that shows the model three rows: one right row in three is an answered
    question, and the second right row changes nothing.

    """
    hit_at = next((i for i, d in enumerate(ranked) if d in gold), None)
    return (
        {k: 1.0 if hit_at is not None and hit_at < k else 0.0 for k in K},
        0.0 if hit_at is None else 1.0 / (hit_at + 1),
    )


def _prefix_match(doc_id: str, wanted: tuple[str, ...]) -> bool:
    """A paragraph counts as its article: `art.116.3` answers a question about §116."""
    return any(doc_id == w or doc_id.startswith(f"{w}.") for w in wanted)


async def main() -> None:
    db = Database()
    lexical, semantic = await asyncio.gather(open_index(db), open_vectors(db))
    channels = {"bm25": lexical, "embedding": semantic,
                "hybrid": HybridRetriever([lexical, semantic])}

    cases: list[tuple[str, str, set[str]]] = []
    for question, articles in STATUTE_GOLD:
        rows = await db.fetch(
            "SELECT doc_id FROM statute_article WHERE doc_id = ANY($1::text[]) OR doc_id LIKE ANY($2::text[])",
            [list(articles), [f"{a}.%" for a in articles]],
        )
        gold = {r["doc_id"] for r in rows}
        if gold:
            cases.append((STATUTE, question, gold))
        else:
            print(f"  [skip] {question} — {articles} not in the corpus")

    for question, pattern in CLAUSE_GOLD:
        rows = await db.fetch(
            "SELECT DISTINCT clause_id FROM clause WHERE heading ~ $1::text", [pattern])
        gold = {r["clause_id"] for r in rows}
        if gold:
            cases.append((CLAUSE, question, gold))

    print(f"\n{len(cases)} 題 · recall@k 與 MRR，值越高越好\n")
    header = f"{'channel':<11}" + "".join(f"  R@{k}" for k in K) + "    MRR"
    print(header)
    print("-" * len(header))
    for name, retriever in channels.items():
        totals = dict.fromkeys(K, 0.0)
        rr = 0.0
        for corpus, question, gold in cases:
            hits = retriever.search(question, corpus=corpus, scope=[], limit=max(K))
            ranked = [h.doc_id for h in hits]
            if corpus == STATUTE:
                ranked = [d for d in ranked]
            scored, reciprocal = _scores(ranked, gold)
            for k in K:
                totals[k] += scored[k]
            rr += reciprocal
        n = len(cases)
        print(f"{name:<11}" + "".join(f"  {totals[k] / n:.2f}" for k in K) + f"   {rr / n:.2f}")

    print("\n逐題（hybrid），未命中的列出來：")
    hybrid = channels["hybrid"]
    for corpus, question, gold in cases:
        ranked = [h.doc_id for h in hybrid.search(question, corpus=corpus, scope=[], limit=5)]
        scored, _ = _scores(ranked, gold)
        if scored[5] == 0.0:
            print(f"  ✗ 『{question}』 期望 {sorted(gold)[:3]}… 得到 {ranked[:3]}")


asyncio.run(main())
