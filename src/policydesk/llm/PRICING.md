# Where these rates come from

`model_pricing.json` holds USD per **1,000** tokens. OpenAI publishes per million, so
every number here is the published one divided by 1,000 — `gpt-5.6-sol` at $4.00/1M input
is `0.0040`.

| model | input | cached | output | source |
|---|---|---|---|---|
| `gpt-5.6-luna` | $0.20/1M | $0.02/1M | $1.20/1M | <https://developers.openai.com/api/docs/pricing>, confirmed on the model's own page |
| `gpt-5.6-sol` | $4.00/1M | $0.40/1M | $20.00/1M | same, confirmed on the model's own page |
| `gpt-5.3-codex` | $1.75/1M | $0.175/1M | $14.00/1M | <https://developers.openai.com/api/docs/models/gpt-5.3-codex> |
| `claude-haiku-4-5` | $1.00/1M | — | $5.00/1M | <https://docs.anthropic.com/en/docs/about-claude/pricing>, read 2026-09-05 |
| `claude-haiku-4-5-20251001` | $1.00/1M | — | $5.00/1M | same rate; see the two-rows note below |
| `stub` | 0 | — | 0 | `ScriptedProvider` answers from a fixture and makes no API call, so zero here is a fact rather than a placeholder |

## The Claude rows are an estimate, not an invoice

`AnthropicProvider` authenticates with Claude Code's subscription OAuth token, so its
calls are billed against a flat monthly subscription and **not** charged per token. The
rate above is Anthropic's published list price — what the same traffic would cost on a
pay-as-you-go Console key. Read the 成本 column on those rows as "what this turn would
have cost", not "what was paid". A deployment that switches to a Console key is the one
where the figure becomes literal.

Two rows, same rate, because the API answers with the dated id: a call sent to
`claude-haiku-4-5` comes back reporting `claude-haiku-4-5-20251001`, and `llm_usage.model`
records what served rather than what was asked for. Only the dated row is ever read
today; the undated one covers a caller that names the model itself. No `cached` rate is
listed — this desk sends no `cache_control`, and `price()` bills a cached token at the
`input` rate when the entry omits one, which is the right default for traffic that never
reads a cache.

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
