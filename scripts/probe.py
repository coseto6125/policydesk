"""
Ask the retrieval channels a customer's question and show why they answered it that way.

Run as `uv run scripts/probe.py [question ...]`. Every claim about ranking in this project should be
reproducible by whoever reads the claim, and a score on its own is not a reason — 4.5
beats 6.8 or loses to it depending on facts about the corpus that the number does not
carry. So each hit is printed with the two facts that decide it: how rare each matched
word is, and how long the provision is against the corpus mean.

Those two are what the diagnoses so far have turned on:

**Rarity.** 保單停效可以復效嗎 and 猶豫期幾天可以撤銷 returned an identical ranking, which
reads like the tokeniser dropping the sentence. It was not. 可以 appears in four of 1,212
provisions and 復效 in seven, so the filler word is the *rarer* of the two and BM25 was
right to weight it higher — given that nobody had told it 可以 is filler. Fixed by
`QUERY_STOP`, which is why this script prints document frequency: the diagnosis was
invisible from the ranking alone and obvious from one column of counts.

**Length.** Even with 可以 gone, 第116條 lost. 復效 alone ranks it first at 4.5; adding
保單 hands the top to 第140條第1項 — 保險公司得簽訂參加保單紅利之保險契約 — at 6.8. That
provision is 19 characters against a 70-character mean, so BM25's length normalisation
treats one incidental match in it as denser evidence than the real match in a 159-character
one. That is `b`, and tantivy-py 0.26 exposes no way to set it — `boost_query` weights a
term, not a document — so the answer is the embedding channel, whose scores do not depend
on document length at all. The `SHORT` marker below flags the documents this is happening
to.

**Vocabulary.** 我要改受益人 is the one that survives both fixes: 保險法 §111–114 say 處分
and 轉讓, the customer says 改, and no reweighting bridges that. That query is what the
embedding channel is for, and telling the three cases apart is the point of this script —
they present identically as "the right provision did not come back".
"""

import asyncio
import sys

from policydesk.core.db import Database
from policydesk.retrieval.base import CLAUSE, QUERY_STOP, STATUTE, Retriever
from policydesk.retrieval.index import cut, open_index
from policydesk.retrieval.vectors import open_vectors

SEP = "\u2063"
"""The sentinel `cut` joins tokens with. Splitting on it recovers what the query became."""

QUESTIONS: tuple[str, ...] = (
    "你們憑什麼說要解除我的契約",
    "當初業務員沒說有等待期",
    "保單停效可以復效嗎",
    "猶豫期幾天可以撤銷",
    "我要改受益人",
    "我要去金管會申訴",
)
"""The questions the diagnoses above were made on. Kept so a change can be measured against
the same set rather than against whatever came to mind that day."""


async def _corpus_stats(db: Database, corpus: str) -> tuple[int, float]:
    """
    Count the documents and their mean length.

    Args:
        db: The database.
        corpus: CLAUSE or STATUTE.

    Returns:
        Document count and mean length in characters. Both are what a score has to be read
        against: length only means something next to the mean, and a document frequency
        only means something next to the total.

    """
    table = "statute_article WHERE paragraph IS NOT NULL" if corpus == STATUTE else "clause WHERE true"
    row = await db.fetch_one(f"SELECT count(*) AS n, avg(length(verbatim)) AS mean FROM {table}")  # noqa: S608
    return int(row["n"]), float(row["mean"] or 0)


async def _document_frequency(db: Database, corpus: str, words: list[str]) -> dict[str, int]:
    """
    Count how many documents contain each word.

    Args:
        db: The database.
        corpus: CLAUSE or STATUTE.
        words: The tokens the query was cut into.

    Returns:
        Word to document count. A substring count, not a token count — close enough to
        show which word is driving a ranking, and honest about being an approximation of
        what tantivy actually stores.

    """
    # Written out per corpus rather than interpolated. Both halves are literals either
    # way, so the risk was never real — but a SQL string built by formatting is a shape a
    # reader has to check, and there are only two of them.
    sql = (
        "SELECT count(*) FROM statute_article WHERE verbatim LIKE '%' || $1 || '%' AND paragraph IS NOT NULL"
        if corpus == STATUTE
        else "SELECT count(*) FROM clause WHERE verbatim LIKE '%' || $1 || '%'"
    )
    return {word: await db.fetch_val(sql, [word]) for word in words}


async def _text_of(db: Database, corpus: str, scope_id: str, doc_id: str) -> str:
    """
    Read the document a hit named.

    Args:
        db: The database.
        corpus: CLAUSE or STATUTE.
        scope_id: The statute or product.
        doc_id: The provision or clause.

    Returns:
        Its verbatim text, or empty when the id resolves to nothing — which is itself
        worth seeing, since an id the index holds and the table does not is a stale index.

    """
    if corpus == STATUTE:
        sql = "SELECT verbatim FROM statute_article WHERE statute_id = $1 AND doc_id = $2"
    else:
        sql = "SELECT verbatim FROM clause WHERE product_id = $1 AND clause_id = $2"
    return await db.fetch_val(sql, [scope_id, doc_id]) or ""


async def probe(
    db: Database, retriever: Retriever, question: str, *, corpus: str = STATUTE, limit: int = 5
) -> None:
    """
    Ask one question and print the ranking with its reasons.

    Args:
        db: The database.
        retriever: The channel, or the hybrid over several.
        question: What a customer would type.
        corpus: Which corpus to search.
        limit: How many hits to show.

    """
    # Both lists, because the gap between them is the point. `cut` is what the tokeniser
    # produced; `words` is what the search actually weighted after QUERY_STOP. A term
    # printed as dropped is one that is no longer steering the ranking, and before the stop
    # list existed that distinction was the bug.
    every = [w for w in cut(question).split(SEP) if w]
    words = [w for w in every if w not in QUERY_STOP]
    dropped = [w for w in every if w in QUERY_STOP]
    frequency = await _document_frequency(db, corpus, every)
    total, mean = await _corpus_stats(db, corpus)

    print(f"\n{'=' * 78}\n{question}")
    # Rarest first: the top of this list is what the ranking is mostly made of, and seeing
    # a filler word there is the whole diagnosis. A zero means the word is in no document
    # at all, so it contributes nothing however rare it looks.
    order = sorted(((w, frequency[w]) for w in words), key=lambda kv: kv[1])
    print("  used:    " + "  ".join(f"{w}({n})" for w, n in order) + f"   [of {total}]")
    if dropped:
        print("  dropped: " + "  ".join(f"{w}({frequency[w]})" for w in dropped) + "   [QUERY_STOP]")

    hits = await asyncio.to_thread(retriever.search, question, corpus=corpus, scope=(), limit=limit)
    if not hits:
        print("  (nothing)")
        return
    for hit in hits:
        text = await _text_of(db, corpus, hit.scope_id, hit.doc_id)
        matched = [w for w in words if w in text]
        length = len(text)
        # `short` is the flag for the length-normalisation case: a document well under the
        # mean wins on one incidental word, and the score alone never shows that.
        marker = " SHORT" if length and length < mean * 0.5 else ""
        # Which of the query's words this hit did NOT contain. A ranking whose top entries
        # all miss the same word is the one to read closely: that word is the question, and
        # something else won on evidence the customer did not ask about.
        missing = [w for w in words if w not in text and frequency.get(w)]
        print(
            f"  {hit.score:6.2f}  {hit.doc_id:<16} {hit.scope_id:<34} "
            f"{length:>4}c (mean {mean:.0f}){marker}  matched={matched}"
            + (f"  MISSED={missing}" if missing else "")
        )
        print(f"          {text[:88]}")


async def run(questions: tuple[str, ...], *, corpus: str) -> None:
    """
    Probe every question on every channel that opens.

    Args:
        questions: What to ask.
        corpus: CLAUSE or STATUTE.

    Each channel is probed separately rather than through the hybrid. Fusion reports a
    reciprocal rank, which is a number about the ranking rather than about the document —
    it cannot show that one channel found the provision and the other outvoted it, and
    that is usually the thing worth knowing.

    """
    db = Database()
    lexical, semantic = await asyncio.gather(open_index(db), open_vectors(db))
    channels = [r for r in (lexical, semantic) if r is not None]
    if not channels:
        print("no retrieval channel opened; run policydesk-index first")
        await db.close()
        return
    for channel in channels:
        print(f"\n\n######## channel: {channel.name} ########")
        for question in questions:
            await probe(db, channel, question, corpus=corpus)
    await db.close()


def main() -> None:
    """Probe the questions named on the command line, or the standard set."""
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    corpus = CLAUSE if "--clause" in sys.argv else STATUTE
    asyncio.run(run(tuple(args) or QUESTIONS, corpus=corpus))


if __name__ == "__main__":
    main()
