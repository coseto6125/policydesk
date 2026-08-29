"""
Price the calls that were recorded before there was a rate to price them at.

`cost_usd` has been on `llm_usage` since the first migration and nothing wrote it, so
every row already in the table is NULL. Going forward the recorders fill it; this fills
what is behind them, which is the difference between a console that starts showing a
bill today and one that shows a bill for the whole history.

Run it again after editing `model_pricing.json` — it only touches rows that are still
NULL, so a row priced at the rate in force when it ran keeps that price, and a model
that has just gained an entry gets one. Re-pricing a row that already has a cost is a
different job: it needs someone to decide which of the two rates the call was actually
billed at, and this command does not make that decision quietly.
"""

import asyncio
import sys
from decimal import Decimal

from policydesk.bootloader import logger
from policydesk.core.db import Database
from policydesk.llm.pricing import price, rates


async def backfill(db: Database) -> dict[str, int]:
    """
    Give every unpriced row a cost, where its model has a rate.

    Args:
        db: The database.

    Returns:
        How many rows were priced and how many stayed NULL for want of a rate.

    Grouped by model and token counts rather than updated row by row: a fortnight of
    traffic is thousands of rows over a handful of distinct models, and the arithmetic
    is the same for every row sharing a model and a token triple.

    A row with no tokens is still priced, at zero. The stub provider answers from a
    fixture and records no tokens, and 204 such rows reported as 未定價 would say the
    console could not price them when what it means is that they were free. The rows
    left out are the ones with no model at all — a call that never returned, which has
    no rate because it has no provider, not because the table is missing an entry.

    """
    if not rates():
        logger.error("no_rates", note="model_pricing.json is missing or empty; nothing to price")
        return {"priced": 0, "unpriced": 0}

    groups = await db.fetch(
        """SELECT model, prompt_tokens, completion_tokens, cached_tokens, count(*) AS n
           FROM llm_usage
           WHERE cost_usd IS NULL AND model <> ''
           GROUP BY model, prompt_tokens, completion_tokens, cached_tokens""",
    )
    priced = unpriced = 0
    for g in groups:
        usd = price(
            g["model"],
            prompt_tokens=g["prompt_tokens"],
            completion_tokens=g["completion_tokens"],
            cached_tokens=g["cached_tokens"],
        )
        if usd is None:
            unpriced += g["n"]
            continue
        await db.execute(
            """UPDATE llm_usage SET cost_usd = $1::numeric
               WHERE cost_usd IS NULL AND model = $2::text
                 AND prompt_tokens = $3::int AND completion_tokens = $4::int AND cached_tokens = $5::int""",
            [Decimal(f"{usd:.6f}"), g["model"], g["prompt_tokens"], g["completion_tokens"], g["cached_tokens"]],
        )
        priced += g["n"]
    logger.info("backfill_done", priced=priced, unpriced=unpriced, models=len(rates()))
    return {"priced": priced, "unpriced": unpriced}


def main() -> int:
    """
    Run the backfill.

    Returns:
        0 when it ran, 1 when there were no rates to run it with — an empty table is a
        deployment misconfiguration and a caller in a pipeline should see it fail.

    """
    result = asyncio.run(backfill(Database()))
    print(f"priced {result['priced']} rows · {result['unpriced']} still unpriced")
    return 0 if result["priced"] or not result["unpriced"] else 1


if __name__ == "__main__":
    sys.exit(main())
