"""
What the desk remembers, and how long each part of it lasts.

A customer does not walk the flow in order. They ask about a claim, then a premium,
then decide to apply, then come back the next day and open with the question they
started on. Three mechanisms, on three clocks, because one window cannot serve all of
that:

**The transcript** carries local coherence — the last few exchanges, verbatim. It is cut
by a *gap in time*, not by a token count: a thread whose last message is an hour old is
a new conversation, and replaying yesterday's half-finished sentence into it makes the
desk answer a question nobody just asked.

**The profile card** carries what survives the window. Facts about the person and a
rolling summary of where their application stands, rendered ahead of the transcript.
This is the part that makes a customer who jumps A→C→F→B coherent: the budget they
stated six turns ago is on the card whether or not it is still in the window.

**The sweep** writes the card. It is a model call, and it runs offline against settled
conversations rather than on the reply path, so it never costs a customer latency. It is
recorded in `llm_usage` under phase `facts` like every other call this system makes.

Ported from enoract's `user_facts`/`summary` pair, with the key changed. enoract keys
facts by conversation, which splits one person's profile across channels. Here a fact
about a customer belongs to the customer and outlives the case it was first said in.
"""

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from msgspec import DecodeError, Struct, json

from policydesk.bootloader import logger
from policydesk.llm.pricing import cost
from policydesk.llm.provider import Provider, ProviderError

if TYPE_CHECKING:
    from datetime import datetime

    from policydesk.core.db import Database

HISTORY_LIMIT = 8
"""Messages of verbatim transcript. Four exchanges, which is what local coherence needs."""

SESSION_GAP_S = 3600
"""A quiet hour ends the session. Past it the card carries the conversation, not the transcript."""

IDLE_GAP_S = 90
"""How settled a case must be before the sweep reads it. Shorter than the session gap:
a conversation can be worth summarising long before it is over."""

FACTS_MAX = 20
"""Facts kept per member. The oldest-updated is evicted, so the card stays scannable."""

VALUE_MAX = 80
SUMMARY_MAX = 300

CATEGORIES: frozenset[str] = frozenset({"need", "cons", "hist", "pref"})


class Fact(Struct):
    """One durable thing the customer said about themselves."""

    key: str
    value: str
    category: str


class Extraction(Struct):
    """What the sweep returns."""

    facts: list[Fact] = []
    summary: str = ""
    """Empty keeps the stored one, so a sweep that learned nothing overwrites nothing."""


EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "簡短的欄位名，例如 預算上限、保障需求、繳別偏好"},
                    "value": {"type": "string", "description": "保戶自己說的內容，照原話濃縮"},
                    "category": {"type": "string", "enum": sorted(CATEGORIES)},
                },
                "required": ["key", "value", "category"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["facts", "summary"],
    "additionalProperties": False,
}

EXTRACT_INSTRUCTIONS = """\
你在維護一位保險客戶的長期記憶，供下一次對話使用。

從對話紀錄中抽出「以後仍然成立」的事實，並更新這件案子的摘要。

事實規則：
- 只記保戶自己明講的內容。逐字沒說的就不要記，寧可回空陣列。
- key 用簡短中文欄位名。已存在的事實用同一個 key，才會覆蓋而不是長出兩筆。
- category 擇一：need 保障需求、cons 硬限制（預算上限、不願加費、排除項目）、
  hist 已發生的事（曾申請理賠、曾被拒保、已購買）、pref 偏好（繳別、聯絡方式）。
- 不要記身分證字號、地址、電話、電子郵件、任何金融帳號。這些欄位在保戶主檔裡，
  抄進記憶只是把個資多存一份。
- value 濃縮在 80 字以內。

摘要規則：
- 把「先前摘要」和這段新對話折疊成一段，寫：保戶在辦什麼、目前卡在哪、還有什麼沒解決。
- 300 字以內，白話散文，用保戶說話的語言寫。
- 先前摘要裡已經解決或過期的事就刪掉，不要一直累加。
- 這段新對話沒有補上任何先前摘要沒有的東西時，summary 回空字串。\
"""

_LOAD_HISTORY = """\
SELECT message_id, speaker, text, created_at, now() AS observed_at
FROM conversation_message
WHERE case_id = $1::bigint AND message_id > $3::bigint
ORDER BY message_id DESC
LIMIT $2::int"""

_LOAD_FACTS = """\
SELECT key, value, category,
       source_message_id IS NOT NULL AND EXISTS (
           SELECT 1 FROM conversation_message m WHERE m.message_id = source_message_id
       ) AS evidenced
FROM member_fact
WHERE member_id = $1::bigint
ORDER BY updated_at DESC
LIMIT $2::int"""


async def recent(
    db: Database, case_id: int, limit: int = HISTORY_LIMIT, since: int = 0
) -> list[dict[str, Any]]:
    """
    Read the current session's messages, oldest first.

    Args:
        db: The database.
        case_id: Which case.
        limit: Most messages to consider.
        since: Read nothing at or below this message_id. 0 reads the whole case.

    Returns:
        The messages since the last gap wider than SESSION_GAP_S and above `since`,
        oldest first. Empty when the newest message is itself older than the gap, which
        is how a customer returning tomorrow starts on a clean transcript instead of
        mid-sentence.

    **`since` is the connection's own boundary, and it is a second boundary on purpose.**
    The gap is about continuity: a customer who reloads mid-sentence keeps their context.
    It is not about identity, and it cannot be — a visitor who types a display name that
    matches an existing member is bound to that member's live case, and inside the gap
    they were handed the transcript of a conversation somebody else had. The caller sets
    `since` to the case's newest message at the moment the socket bound, so an unverified
    connection reads only what it has said itself, and drops it once the check passes.

    Over-fetches, because the gap walk may cut the window short and a short window is
    worse than a wasted row.

    """
    rows = await db.fetch(_LOAD_HISTORY, [case_id, limit * 2, since])
    kept: list[dict[str, Any]] = []
    previous: datetime | None = None
    for row in rows:
        anchor = row["observed_at"] if previous is None else previous
        if (anchor - row["created_at"]).total_seconds() > SESSION_GAP_S:
            break
        kept.append(row)
        previous = row["created_at"]
        if len(kept) >= limit:
            break
    kept.reverse()
    return kept


def transcript(messages: list[dict[str, Any]]) -> str:
    """
    Render the session for the model.

    Args:
        messages: Rows from `recent`, oldest first.

    Returns:
        A labelled block, empty when nothing is in the window.

    """
    if not messages:
        return ""
    lines = "\n".join(f"{'保戶' if m['speaker'] == 'customer' else '櫃台'}：{m['text']}" for m in messages)
    return f"# 先前對話\n{lines}\n\n"


async def card(db: Database, *, member_id: int, case_id: int) -> str:
    """
    Render what the desk knows about this customer, beyond the window.

    Args:
        db: The database.
        member_id: Whose facts.
        case_id: Whose summary.

    Returns:
        A profile block, empty when nothing has been extracted yet.

    **A fact whose evidence is gone is marked rather than dropped.** The migration says it
    outright — `source_message_id` "is what makes the difference checkable" — and the card
    loaded three columns and not that one, so a fact nobody can trace reached the model
    with the same authority as one quoted from a message still on file, under an
    instruction to use it directly and not ask again.

    Marked, not dropped, because the fact is still probably true: 已離婚 does not stop
    being true when a case is closed and its messages age out. What changes is whether the
    desk may say 您上次提到 about it, and that is the distinction the mark carries.

    The pointer goes on its own, without anyone deleting a fact: `source_message_id` is
    `ON DELETE SET NULL`, so any message deletion or retention cut produces this state.

    """
    facts, row = await asyncio.gather(
        db.fetch(_LOAD_FACTS, [member_id, FACTS_MAX]),
        db.fetch_one('SELECT summary FROM "case" WHERE case_id = $1::bigint', [case_id]),
    )
    summary = (row or {}).get("summary") or ""
    if not facts and not summary:
        return ""

    parts = ["# 已知的保戶資訊（來自先前對話，可能不在上面的對話紀錄裡）"]
    if summary:
        parts.append(f"目前進度：{summary}")
    def line(fact: dict[str, Any]) -> str:
        return f"- {fact['key']}（{fact['category']}）：{fact['value']}"

    # Two blocks, and the caveat written once. Tagging each unevidenced line put the same
    # twenty-six characters on eleven rows of one card, which is how a warning stops being
    # read — the model skims a repeated marker the way a person does.
    traceable = [f for f in facts if f["evidenced"]]
    parts.extend(line(f) for f in traceable)
    if traceable:
        parts.append("回答時直接沿用這些資訊，不要重問。保戶若說了相反的話，以新的說法為準。")
    if untraceable := [f for f in facts if not f["evidenced"]]:
        parts.append("\n## 以下這幾項的原始對話已經不在了")
        parts.extend(line(f) for f in untraceable)
        parts.append(
            "這幾項多半仍然成立，但你不可以說「您上次提到」或當成他講過的話引用。"
            "要用到的時候先問他一句確認。"
        )
    parts.append("")
    return "\n".join(parts)


def clean(extraction: Extraction) -> tuple[list[Fact], str]:
    """
    Keep only what the store will accept.

    Args:
        extraction: What the model returned.

    Returns:
        The facts with a usable key, value and category, and the summary within its cap.

    A model that invents a category writes nothing rather than a row nobody can query,
    and a value longer than the cap is truncated rather than dropped — the first eighty
    characters of a stated budget are still the budget.

    """
    kept = [
        Fact(key=key, value=fact.value.strip()[:VALUE_MAX], category=fact.category)
        for fact in extraction.facts
        if (key := fact.key.strip()) and fact.value.strip() and fact.category in CATEGORIES
    ]
    return kept[:FACTS_MAX], extraction.summary.strip()[:SUMMARY_MAX]


async def _claim(db: Database, batch: int) -> list[dict[str, Any]]:
    """
    Take the cases that have gone quiet, advancing their watermark as we take them.

    Args:
        db: The database.
        batch: Most cases to claim at once.

    Returns:
        Claimed cases with the watermark they held before the claim.

    The update and the selection are one statement with SKIP LOCKED, so a second worker
    running the same sweep takes different rows rather than the same ones.

    """
    return await db.fetch(
        """UPDATE "case" c SET facts_extracted_at = now()
           FROM (
             SELECT k.case_id, k.member_id, k.summary, k.facts_extracted_at AS prev
             FROM "case" k
             WHERE EXISTS (SELECT 1 FROM conversation_message m WHERE m.case_id = k.case_id)
               AND (SELECT max(created_at) FROM conversation_message m WHERE m.case_id = k.case_id)
                   < now() - make_interval(secs => $1::int)
               AND (SELECT max(created_at) FROM conversation_message m WHERE m.case_id = k.case_id)
                   > coalesce(k.facts_extracted_at, '-infinity'::timestamptz)
             ORDER BY k.case_id
             LIMIT $2::int
             FOR UPDATE SKIP LOCKED
           ) s
           WHERE c.case_id = s.case_id
           RETURNING c.case_id, s.member_id, s.summary, s.prev""",
        [IDLE_GAP_S, batch],
    )


async def _write(db: Database, *, member_id: int, case_id: int, facts: list[Fact], summary: str, source: int) -> None:
    """
    Store one extraction.

    Args:
        db: The database.
        member_id: Whose facts.
        case_id: Whose summary.
        facts: The cleaned facts.
        summary: The cleaned summary, empty to keep the stored one.
        source: The newest message the extraction read.

    Written as separate statements rather than one transaction. Each is an idempotent
    upsert against a key the sweep owns, so a failure part-way leaves a smaller memory
    rather than a wrong one, and the next thing the customer says re-triggers the sweep.

    """
    if summary:
        await db.execute('UPDATE "case" SET summary = $2::text WHERE case_id = $1::bigint', [case_id, summary])
    if not facts:
        return
    await db.execute_many(
        """INSERT INTO member_fact (member_id, key, value, category, source_message_id, updated_at)
           VALUES ($1::bigint,$2::text,$3::text,$4::text,$5::bigint,now())
           ON CONFLICT (member_id, key) DO UPDATE
           SET value = EXCLUDED.value, category = EXCLUDED.category,
               source_message_id = EXCLUDED.source_message_id, updated_at = now()""",
        [[member_id, f.key, f.value, f.category, source] for f in facts],
    )
    # Cap the store, oldest-updated evicted. A card nobody can scan is a card the model
    # skims, and twenty facts is already more than a service call ever states.
    await db.execute(
        """DELETE FROM member_fact WHERE member_id = $1::bigint AND key NOT IN (
             SELECT key FROM member_fact WHERE member_id = $1::bigint
             ORDER BY updated_at DESC LIMIT $2::int)""",
        [member_id, FACTS_MAX],
    )


async def sweep_once(db: Database, provider: Provider, *, batch: int = 5) -> int:
    """
    Extract memory from every case that has gone quiet since the last sweep.

    Args:
        db: The database.
        provider: The model seam.
        batch: Most cases per pass.

    Returns:
        How many cases were extracted.

    """
    claimed = await _claim(db, batch)
    done = 0
    for case in claimed:
        messages = await db.fetch(
            """SELECT message_id, speaker, text FROM conversation_message
               WHERE case_id = $1::bigint ORDER BY message_id DESC LIMIT 40""",
            [case["case_id"]],
        )
        if not messages:
            continue
        messages.reverse()
        body = "\n".join(f"[{m['message_id']}] {'保戶' if m['speaker'] == 'customer' else '櫃台'}：{m['text']}" for m in messages)
        stored = await db.fetch(_LOAD_FACTS, [case["member_id"], FACTS_MAX])
        known = "\n".join(f"- {f['key']}（{f['category']}）：{f['value']}" for f in stored) or "（無）"

        try:
            completion = await provider.complete(
                instructions=EXTRACT_INSTRUCTIONS,
                user_input=f"# 已存事實\n{known}\n\n# 先前摘要\n{case['summary'] or '（無）'}\n\n# 對話紀錄\n{body}",
                schema=EXTRACTION_SCHEMA,
            )
            extraction = json.decode(completion.text.encode(), type=Extraction)
        except (ProviderError, DecodeError, ValueError) as exc:
            logger.warning("facts_extract_failed", case_id=case["case_id"], error=str(exc))
            continue

        await db.execute(
            """INSERT INTO llm_usage (case_id, phase, provider, model, prompt_tokens, completion_tokens,
                                      cached_tokens, total_tokens, cost_usd, latency_ms, response)
               VALUES ($1::bigint,'facts',$2::text,$3::text,$4::int,$5::int,$6::int,$7::int,
                       $8::numeric,$9::int,$10::jsonb)""",
            [
                case["case_id"], completion.provider, completion.model,
                completion.prompt_tokens, completion.completion_tokens, completion.cached_tokens,
                completion.total_tokens, cost(completion), completion.latency_ms,
                {"text": completion.text[:2000]},
            ],
        )

        facts, summary = clean(extraction)
        await _write(
            db, member_id=case["member_id"], case_id=case["case_id"],
            facts=facts, summary=summary, source=messages[-1]["message_id"],
        )
        logger.info("facts_extracted", case_id=case["case_id"], facts=len(facts), summarised=bool(summary))
        done += 1
    return done


async def sweep_loop(db: Database, provider: Provider, *, every: float = 30.0) -> None:
    """
    Run the sweep forever, off the reply path.

    Args:
        db: The database.
        provider: The model seam.
        every: Seconds between passes.

    A failing pass is logged and the loop continues. Memory is an enhancement to a turn,
    never a precondition for one — a desk whose sweep is down still answers, with a
    shorter memory.

    """
    while True:
        with contextlib.suppress(asyncio.CancelledError):
            try:
                await sweep_once(db, provider)
            except Exception as exc:
                logger.warning("facts_sweep_failed", error=str(exc))
        await asyncio.sleep(every)

