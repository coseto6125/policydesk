"""
What a retriever is, and how two of them combine.

One shape for every channel, so a caller never learns which one answered. `find_clause`
asks a `Retriever` for hits and reads the rows; whether the ranking came from BM25 over
tokens or from cosine over embeddings is the retriever's business.

The fusion is **reciprocal rank**, not a weighted sum of scores. A BM25 score is an
unbounded function of term frequency and corpus size; a cosine similarity is bounded to
[-1, 1] and clusters near 0.6 for anything vaguely related. Adding them mixes two
different units, and normalising them per query makes the weights depend on how good the
best hit happened to be. RRF discards the magnitudes and keeps only the order each
channel put things in, which is the part both channels agree means something.
"""

from typing import TYPE_CHECKING, Protocol

from msgspec import Struct

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

CLAUSE = "clause"
"""Insurance contract clauses, scoped to the products a member holds."""

STATUTE = "statute"
"""Statute articles. Scoped to a statute id, or to nothing — everyone is subject to a law,
so an empty scope here means no restriction rather than no results."""

RRF_K = 60
"""The rank offset in reciprocal-rank fusion. 60 is the value the original paper settled
on; it flattens the difference between rank 1 and rank 2 enough that one channel's
confident first place cannot outvote the other channel entirely."""


QUERY_STOP: frozenset[str] = frozenset({
    "你們", "我們", "為什麼", "憑什麼", "怎麼", "這樣", "可以", "不可以", "應該", "什麼",
    "這個", "那個", "已經", "根本", "當初", "知道", "想要", "我要", "沒有", "還有", "現在",
    "公司", "你們公司", "貴公司", "保單",
})
"""Words a question is made of that no document is about. Dropped from a query, never
from a document — removing them at index time would change every document's length and
so change every score, which is a different intervention with a different failure.

Not a general stop list. 保險, 契約 and 解除 are common here and load-bearing.

The measurement that put 可以 in it: it appears in 4 of 1,212 statute articles, so its
IDF is 5.60 — higher than 復效's 5.09. 保單停效可以復效嗎 and 猶豫期幾天可以撤銷 therefore
returned the same three provisions as each other, neither of them about either question,
because the rarest term in both sentences was the filler. A customer's 語助詞 outranking
what they are actually asking is the shape of this failure, and it does not look like a
retrieval bug from the outside — it looks like a corpus that does not contain the answer.

公司 earns its place for a different reason: in a customer's sentence it is a pronoun
meaning *you*, and in the statute it is a corporate entity the regulator licenses.

保單 is the same confusion again, and it costs nothing to drop because of how the corpus
dictionary cuts. In a customer's sentence 保單 means *my contract*; in 保險法 it appears in
33 of 1,212 articles, all of them about 保單紅利 or 保單價值準備金 — so its IDF is high for
a word carrying no signal. 保單停效可以復效嗎 therefore ranked 第140條, 保險公司得簽訂參加
保單紅利之保險契約, above 第116條. Measured A/B on four queries: dropping it moves that one
from 第140條 to 第116條 and leaves 保單價值準備金怎麼算, 我的保單借款利息 and the clause
corpus's 保單停效多久內可以復效 in exactly the same order. The compounds survive because
the dictionary cuts 保單價值準備金 whole, so removing the standalone token never reaches
them.

Measured by the session that owns `agent/statute.py`, which holds the same set for its
SQL fallback and imports it from here.
"""


class Hit(Struct, frozen=True):
    """One retrieved document, in the only shape callers see."""

    corpus: str
    """CLAUSE or STATUTE."""
    doc_id: str
    """`art.12`, `art.6.carve1`, `art.64.1` — the id a citation names."""
    scope_id: str
    """The product or statute the document belongs to. A doc_id alone is ambiguous:
    every contract in the corpus has an `art.6`."""
    score: float
    """Whatever the channel scored it. Comparable within a channel, never across."""
    start: int | None = None
    end: int | None = None
    """Matched passage offsets in heading + newline + verbatim, when available."""


class Retriever(Protocol):
    """A source of ranked hits."""

    name: str

    def search(self, query: str, *, corpus: str, scope: Sequence[str], limit: int) -> list[Hit]:
        """
        Find documents bearing on a query.

        Args:
            query: What the customer asked, uncut.
            corpus: CLAUSE or STATUTE.
            scope: Which scope_ids to search. Empty means every scope in that corpus.
            limit: Most hits to return.

        Returns:
            Hits, best first. Empty is a valid answer and never an error — a channel
            that cannot answer lets the other one serve.

        """
        ...


WEIGHTS: dict[str, dict[str, float]] = {
    CLAUSE: {"bm25": 0.5, "embedding": 1.0},
    STATUTE: {"bm25": 0.75, "embedding": 1.0},
}
"""How much each channel's rank counts, per corpus. Unlisted channels count 1.0.

Unweighted RRF gives a wrong channel's first place the same 1/(k+1) as a right channel's,
so a confident answer gets diluted by a confident mistake. Measured on this corpus, that
happens in opposite directions on the two halves, which is why the weights are per corpus
rather than global:

- **Clause.** 換工作會不會影響保險 is the query this whole channel exists for. Embedding
  returns 職業或職務變更的通知義務 first; BM25 returns 本商品說明書僅供參考 boilerplate,
  because the customer's words appear nowhere in the contract. Unweighted, the boilerplate
  came first. 什麼情況不賠 is the same shape: embedding finds 除外責任, BM25 finds
  海外醫療專機運送服務, and the fusion led with the aeroplane.
- **Statute.** The other way round. 我有據實說明啊 has BM25 on 第64條 and embedding on
  第149-8條第2項第1款 了結現務, which is not about anything the customer said. The law is
  short, precise, and written in words a complaint sometimes uses verbatim; a contract is
  long and shares no vocabulary with the question at all.

0.5 halves a channel's vote rather than removing it: the weaker channel still breaks a tie
and still promotes a document both agree on, which is the property RRF is for. Removing it
would be running one channel per corpus and calling it a hybrid.

**The statute pair was 1.0/0.5 and is measured wrong at that setting.** Those numbers came
from a handful of queries; `scripts/recall.py` scores fourteen, each pairing a question a
scenario says it answers with the article that scenario names, and the ranking there is:

    bm25  embed   R@1   R@3   R@5    MRR
    1.00   0.50  0.36  0.43  0.50   0.41   <- what shipped
    1.00   1.00  0.43  0.57  0.57   0.49
    0.75   1.00  0.50  0.64  0.64   0.55   <- now
    0.50   1.00  0.43  0.64  0.64   0.51

Every setting that lifts embedding over 0.5 beats the shipped one, so the direction holds
independently of where the optimum sits. 我有據實說明啊 is still the reason bm25 keeps the
larger share it has: the law is short and a complaint sometimes uses its words verbatim.

The clause pair is unchanged. That half of the gold set has three questions, both extremes
score 1.00 and everything between scores 0.73 — an artefact of n=3, not a signal, and too
thin to overturn the queries the current pair was set on.
"""


def rrf(
    rankings: Iterable[tuple[str, list[Hit]]] | Iterable[list[Hit]],
    *,
    limit: int,
    k: int = RRF_K,
    weights: dict[str, float] | None = None,
) -> list[Hit]:
    """
    Fuse several rankings by reciprocal rank.

    Args:
        rankings: One ranked list per channel, each optionally paired with its channel
            name as `(name, hits)`. A bare list counts 1.0.
        limit: Most hits to return.
        k: The rank offset.
        weights: Channel name to weight. None counts every channel 1.0.

    Returns:
        The fused ranking, best first. `score` carries the fused value, not any
        channel's own — the units are gone by design, and a caller comparing a fused
        score against a BM25 threshold would be comparing nothing.

    A document found by both channels beats one found by either alone, which is the
    whole point: BM25 knows the customer used the contract's own words, embeddings know
    they used different words for the same thing, and the document both agree on is the
    one to show.

    """
    fused: dict[tuple[str, str], float] = {}
    seen: dict[tuple[str, str], Hit] = {}
    for entry in rankings:
        name, ranking = entry if isinstance(entry, tuple) else ("", entry)
        weight = (weights or {}).get(name, 1.0)
        if weight <= 0:
            # Zero reads as "switch this channel off for this corpus", and without this it
            # means "rank it last": its documents still enter `fused` at score 0.0 and
            # still occupy slots the other channel would have filled.
            continue
        for position, hit in enumerate(ranking):
            key = (hit.scope_id, hit.doc_id)
            fused[key] = fused.get(key, 0.0) + weight / (k + position + 1)
            if key not in seen or hit.start is not None:
                seen[key] = hit
    ordered = sorted(fused.items(), key=lambda item: item[1], reverse=True)[:limit]
    return [
        Hit(corpus=seen[key].corpus, doc_id=seen[key].doc_id, scope_id=seen[key].scope_id, score=score,
            start=seen[key].start, end=seen[key].end)
        for key, score in ordered
    ]


class HybridRetriever:
    """
    Several channels, one ranking.

    Each channel is asked for more than the caller wanted, because fusion only has
    something to work with when the lists overlap: two top-3s that share nothing fuse
    into a top-6 in an arbitrary order.
    """

    name = "hybrid"

    def __init__(
        self,
        retrievers: Sequence[Retriever],
        *,
        depth: int = 4,
        weights: dict[str, dict[str, float]] | None = None,
        reranker: object | None = None,
    ) -> None:
        self._retrievers = [r for r in retrievers if r is not None]
        self._depth = depth
        self._weights = WEIGHTS if weights is None else weights
        self.reranker = reranker
        """The cross-encoder, carried here and not used here.

        `search` is synchronous and holds nothing but ids; reranking needs the document
        text, and the only code that can read it is the async caller that was going to
        fetch the rows anyway. So this rides on the object every caller already receives,
        instead of a keyword threaded through `gather` in each of the ten scenario
        modules. None is a desk with the fused ranking unchanged."""

    @property
    def channels(self) -> list[str]:
        """
        Name the channels in play.

        Returns:
            Their names, in order. Recorded on a turn so a ranking can be explained
            after the fact.

        """
        return [r.name for r in self._retrievers]

    def search(self, query: str, *, corpus: str, scope: Sequence[str], limit: int) -> list[Hit]:
        """
        Ask every channel and fuse what comes back.

        Args:
            query: What the customer asked.
            corpus: CLAUSE or STATUTE.
            scope: Which scope_ids to search.
            limit: Most hits to return.

        Returns:
            The fused ranking. With one channel this is that channel's own ranking,
            re-scored — which is what makes the embedding half optional rather than
            structural.

        """
        rankings = [
            (r.name, r.search(query, corpus=corpus, scope=scope, limit=limit * self._depth))
            for r in self._retrievers
        ]
        return rrf(rankings, limit=limit, weights=self._weights.get(corpus))
