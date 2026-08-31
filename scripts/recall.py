"""
Measure what each retrieval channel finds, against a gold set the code already asserts.

Nothing here is labelled by hand. Every statute pair comes from a scenario module that
names the provision it is about — in its docstring, a comment, or a constant like
`payment.GRACE_ARTICLE` — and lists, in its own `description` or that same docstring, the
sentences a customer says to reach it. Every clause pair comes from a tool's own heading
filter: `reinstatement_clauses` selects on `復效|效力停止|恢復效力`, so for a reinstatement
question those clauses ARE the right answer by the desk's own definition, and recall is
measured against that set. `CLAUSE_FILTERS` holds one predicate per filter, copied verbatim
from the tool that runs it, so a filter changing there and not here is visible as a diff.

`可以用保單借錢嗎 -> art.120` was dropped from the original 17: no scenario module claims
§120 for a borrowing question — the two places §120 appears (`payment.py`, `reinstate.py`)
both name it as a *distractor* that wrongly outranks the real answer, which is the opposite
of an assertion.

Run: `uv run python scripts/recall.py`
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from policydesk.core.db import Database
from policydesk.retrieval.base import CLAUSE, STATUTE, HybridRetriever
from policydesk.retrieval.index import open_index
from policydesk.retrieval.vectors import open_vectors

# (question a customer asks, the statute it's in, the article(s) the scenario says answer it)
STATUTE_GOLD: list[tuple[str, str, tuple[str, ...]]] = [
    # reinstate.py / payment.py — GRACE_ARTICLE = 116 (payment.py:47); reinstate.py's own
    # injection quotes 〔保險法 第116條第3項〕 for the same rule (reinstate.py:270-276).
    ("保單停效多久內可以復效", "insurance_act", ("art.116",)),
    ("我這期保費忘記繳會怎樣", "insurance_act", ("art.116",)),
    ("停效之後還要繳保費嗎", "insurance_act", ("art.116",)),
    # payment.py:234-238 — PAYMENT.description lists these verbatim as what this scenario
    # answers, and its only statute is §116 (payment.py:11-13, GRACE_ARTICLE at :47).
    ("寬限期還有多久", "insurance_act", ("art.116",)),
    ("沒繳保單會不會失效", "insurance_act", ("art.116",)),
    # disclosure.py — DISCLOSURE.description (disclosure.py:204-207) names §64; the
    # injection (disclosure.py:214-217) requires citing the same article's 除斥期間 branch
    # in the same breath as the duty itself.
    ("我有據實說明啊你們憑什麼解約", "insurance_act", ("art.64",)),
    ("健康告知沒寫到會怎樣", "insurance_act", ("art.64",)),
    ("投保時沒講的病現在要補說嗎", "insurance_act", ("art.64",)),
    ("解除契約有除斥期間限制嗎", "insurance_act", ("art.64",)),
    ("公司知道我沒據實說明多久後就不能再解約", "insurance_act", ("art.64",)),
    # beneficiary.py:13-18 — designation_rules retrieves §110-111 for "指定變更受益人"
    # and "什麼時候對保險人生效"; the injection (beneficiary.py:328-330) cites
    # 〔保險法 第111條第2項〕 for the notice requirement specifically.
    ("我要改受益人", "insurance_act", ("art.110", "art.111")),
    ("受益人可以填誰", "insurance_act", ("art.110",)),
    ("我離婚了想換受益人", "insurance_act", ("art.110", "art.111")),
    ("受益人變更要通知才對保險公司生效嗎", "insurance_act", ("art.111",)),
    # beneficiary.py:81-89, 149-186 — undesignated_fallback filters the shared query down
    # to §113 alone (beneficiary.py:167-175); designated_protection filters the same query
    # to §112 alone (beneficiary.py:178-186), the mirror-image claim.
    ("沒有指定受益人的話保險金給誰", "insurance_act", ("art.113",)),
    ("受益人比被保險人早過世怎麼辦", "insurance_act", ("art.112", "art.113")),
    ("指定受益人的保險金會列入遺產嗎", "insurance_act", ("art.112",)),
    # occupation.py:25-29 (module docstring) states §59 has four 項 and this scenario
    # brings all four back in one call; :92-94 lists these customer sentences verbatim as
    # the ones checked against the live corpus to rank §59 first.
    ("我換工作要通知保險公司嗎", "insurance_act", ("art.59",)),
    ("職業變更會不會加保費", "insurance_act", ("art.59",)),
    ("我現在做工地會不會影響理賠", "insurance_act", ("art.59",)),
    ("職業變得比較安全可以請保險公司重新核定保費嗎", "insurance_act", ("art.59",)),
    # soothe.py — COMPLAINT_ROUTE["basis"] (soothe.py:89-96) is 〔金融消費者保護法
    # 第13條第2項〕, named as the statutory basis for the escalation route, not a service
    # promise. Scoped to financial_consumer_protection_act: `art.13` also exists (as a
    # different provision) in `insurance_act` and `insurance_act_rules`.
    ("我要申訴要多久內提出", "financial_consumer_protection_act", ("art.13",)),
    ("申訴要先找誰處理", "financial_consumer_protection_act", ("art.13",)),
    ("評議中心多久內要受理申訴", "financial_consumer_protection_act", ("art.13",)),
]

# Every predicate here is copied verbatim from the tool it names, so it stays the
# definition of "correct" rather than this file's own guess at one.
CLAUSE_FILTERS: dict[str, str] = {
    # tools.py:429 `benefit_headings`, filter at tools.py:470-473.
    "benefit_headings": (
        "kind = 'grant' AND heading ~ '保險金|保險範圍|承保範圍' "
        "AND heading !~ '申領|申請|通知|指定|減少|變更|受益人' "
        "AND heading !~ '之限制$|的限制$'"
    ),
    # tools.py:656 `required_documents`, filter at tools.py:709.
    "required_documents": "heading ~ '申領|保險金的申請|檢具|應檢附'",
    # reinstate.py `reinstatement_clauses`, filter at reinstate.py:177.
    "reinstatement_clauses": "heading ~ '復效|效力停止|恢復效力'",
    # cooling_off.py:53,75-78 `cooling_off_clause`/`member_rescission` — an exact heading
    # match rather than a regex, but the same "the filter defines the answer" contract.
    "rescission_headings": "heading = ANY(ARRAY['契約撤銷權', '附約撤銷權'])",
}

# (question, which filter answers it)
CLAUSE_GOLD: list[tuple[str, str]] = [
    # policy_overview — POLICY_OVERVIEW.description (scenario.py:70-73) lists these as
    # what routes here; its tools are list_policies + benefit_headings (scenario.py:88).
    ("我這張保單保什麼", "benefit_headings"),
    ("我保了什麼", "benefit_headings"),
    ("目前的保障範圍是什麼", "benefit_headings"),
    ("有沒有保到重大疾病", "benefit_headings"),
    ("手上這幾張保單保障範圍分別是什麼", "benefit_headings"),
    # claim_checklist — CLAIM_CHECKLIST.description (scenario.py:222) and its injection
    # (scenario.py:226-233), which names required_documents as the source of the 一、二、三
    # document list and the 診斷證明書須列明手術名稱及部位 example (scenario.py:229).
    ("理賠要準備哪些文件", "required_documents"),
    ("申請保險金要附哪些文件", "required_documents"),
    ("住院理賠要準備什麼文件", "required_documents"),
    ("診斷證明書要寫到什麼程度", "required_documents"),
    # reinstate — REINSTATE.description (reinstate.py:256-258): "保單停效了，問怎麼辦、
    # 可不可以復效、能不能救回來"; the injection (reinstate.py:266-268) ties the per-policy
    # reinstatement period to reinstatement_clauses's own text.
    ("保單停效之後怎麼復效", "reinstatement_clauses"),
    ("保單停效了怎麼辦", "reinstatement_clauses"),
    ("停效的保單能不能救回來", "reinstatement_clauses"),
    ("這張保單的復效期限是多久", "reinstatement_clauses"),
    # cooling_off.py:2 — the module's own opening docstring gives these three customer
    # sentences verbatim as what this scenario is for.
    ("我後悔了可以退嗎", "rescission_headings"),
    ("剛買的保單可以取消嗎", "rescission_headings"),
    ("有沒有猶豫期", "rescission_headings"),
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


async def gold_cases(db: Database, *, verbose: bool = False) -> list[tuple[str, str, set[str]]]:
    """
    Resolve `STATUTE_GOLD` and `CLAUSE_GOLD` against the live corpus.

    Args:
        db: The database.
        verbose: Print a `[skip]` line for every pair whose gold set comes back empty —
            the corpus doesn't (or no longer) contains what the pair claims.

    Returns:
        (corpus, question, gold doc_ids) triples, one per pair that resolved to at least
        one row. This is the single source both `recall.py` and any sweep script should
        build their cases from, so there is one gold set rather than two that drift.

    """
    cases: list[tuple[str, str, set[str]]] = []
    for question, statute_id, articles in STATUTE_GOLD:
        rows = await db.fetch(
            "SELECT doc_id FROM statute_article "
            "WHERE statute_id = $1::text AND (doc_id = ANY($2::text[]) OR doc_id LIKE ANY($3::text[]))",
            [statute_id, list(articles), [f"{a}.%" for a in articles]],
        )
        gold = {r["doc_id"] for r in rows}
        if gold:
            cases.append((STATUTE, question, gold))
        elif verbose:
            print(f"  [skip] {question} — {articles} not in {statute_id}")

    for question, filter_key in CLAUSE_GOLD:
        rows = await db.fetch(f"SELECT DISTINCT clause_id FROM clause WHERE {CLAUSE_FILTERS[filter_key]}")  # noqa: S608
        gold = {r["clause_id"] for r in rows}
        if gold:
            cases.append((CLAUSE, question, gold))
        elif verbose:
            print(f"  [skip] {question} — {filter_key} matched nothing")

    return cases


async def main() -> None:
    db = Database()
    lexical, semantic = await asyncio.gather(open_index(db), open_vectors(db))
    channels = {"bm25": lexical, "embedding": semantic,
                "hybrid": HybridRetriever([lexical, semantic])}

    cases = await gold_cases(db, verbose=True)
    n_statute = sum(1 for c in cases if c[0] == STATUTE)
    n_clause = len(cases) - n_statute

    print(f"\n{len(cases)} 題（statute {n_statute}、clause {n_clause}）· recall@k 與 MRR，值越高越好\n")
    header = f"{'channel':<11}" + "".join(f"  R@{k}" for k in K) + "    MRR"
    print(header)
    print("-" * len(header))
    for name, retriever in channels.items():
        totals = dict.fromkeys(K, 0.0)
        rr = 0.0
        for corpus, question, gold in cases:
            hits = retriever.search(question, corpus=corpus, scope=[], limit=max(K))
            ranked = [h.doc_id for h in hits]
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


if __name__ == "__main__":
    asyncio.run(main())
