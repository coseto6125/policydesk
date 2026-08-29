# Where these rates come from

`model_pricing.json` holds USD per **1,000** tokens. OpenAI publishes per million, so
every number here is the published one divided by 1,000 — `gpt-5.6-sol` at $4.00/1M input
is `0.0040`.

| model | input | cached | output | source |
|---|---|---|---|---|
| `gpt-5.6-luna` | $0.20/1M | $0.02/1M | $1.20/1M | <https://developers.openai.com/api/docs/pricing>, confirmed on the model's own page |
| `gpt-5.6-sol` | $4.00/1M | $0.40/1M | $20.00/1M | same, confirmed on the model's own page |
| `gpt-5.3-codex` | $1.75/1M | $0.175/1M | $14.00/1M | <https://developers.openai.com/api/docs/models/gpt-5.3-codex> |
| `stub` | 0 | — | 0 | `ScriptedProvider` answers from a fixture and makes no API call, so zero here is a fact rather than a placeholder |

Read 2026-08-29. `platform.openai.com/docs/pricing` now redirects to the developers.openai.com
page above; `openai.com/api/pricing/` refuses automated fetches.

**`gpt-5.6-sol`'s rate is promotional.** OpenAI's page gives $4/$0.40/$20 against a
$5/$0.50/$30 list, and says the cut runs at least through 2026-11-21. A row priced before
that date keeps the price it was billed at, which is why `policydesk-price` never re-prices
a row that already has a cost.

## What is deliberately absent

`codex-default` is not here. It is the name the provider recorded for six calls on
2026-08-28 before it read a model back, and nothing on this machine says which model the
CLI resolved to on that day — `~/.codex/config.toml` names `gpt-5.6-sol` *today*, which is
not evidence about then. Those six rows show 未定價 in the console, which is the true
statement about them. Pricing them at today's config would put a number under a figure
nobody can source.
