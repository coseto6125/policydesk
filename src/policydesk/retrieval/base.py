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


def rrf(rankings: Iterable[list[Hit]], *, limit: int, k: int = RRF_K) -> list[Hit]:
    """
    Fuse several rankings by reciprocal rank.

    Args:
        rankings: One ranked list per channel.
        limit: Most hits to return.
        k: The rank offset.

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
    for ranking in rankings:
        for position, hit in enumerate(ranking):
            key = (hit.scope_id, hit.doc_id)
            fused[key] = fused.get(key, 0.0) + 1.0 / (k + position + 1)
            seen.setdefault(key, hit)
    ordered = sorted(fused.items(), key=lambda item: item[1], reverse=True)[:limit]
    return [
        Hit(corpus=seen[key].corpus, doc_id=seen[key].doc_id, scope_id=seen[key].scope_id, score=score)
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

    def __init__(self, retrievers: Sequence[Retriever], *, depth: int = 4) -> None:
        self._retrievers = [r for r in retrievers if r is not None]
        self._depth = depth

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
            r.search(query, corpus=corpus, scope=scope, limit=limit * self._depth) for r in self._retrievers
        ]
        return rrf(rankings, limit=limit)
