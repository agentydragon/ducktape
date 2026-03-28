# Prompt Caching Investigation — Anthropic API + autogen

## Problem

The function learning eval makes ~36 LLM calls per run with a growing conversation
history (1500→10000+ tokens). Prompt caching should save ~70% of input token cost by
caching the conversation prefix. But `cache_read_input_tokens` was always 0.

## Root Causes Found

### 1. Prompt caching requires explicit `cache_control` markers

Caching does **not** happen automatically. Unlike OpenAI (which caches long prompts
automatically), the Anthropic API requires explicit `cache_control: {"type": "ephemeral"}`
to activate caching. Without it, `cache_creation_input_tokens` and `cache_read_input_tokens`
are always 0, regardless of prompt size.

Confirmed via live API test (2026-03-28) with a 21,878-token system prompt on
`claude-haiku-4-5-20251001`:

| Approach                          | `input` | `cache_create` | `cache_read` |
| --------------------------------- | ------- | -------------- | ------------ |
| No `cache_control`, call 1        | 21,878  | 0              | 0            |
| No `cache_control`, call 2        | 21,878  | 0              | 0            |
| Top-level `cache_control`, call 1 | 3       | 21,875         | 0            |
| Top-level `cache_control`, call 2 | 3       | 0              | **21,875**   |
| Per-block `cache_control`, call 1 | 7       | 21,871         | 0            |
| Per-block `cache_control`, call 2 | 7       | **21,871**     | **0**        |

### 2. Per-block `cache_control` never produces cache reads (creates new entry every call)

The original implementation injected `cache_control` on the system message block and the
last conversation message block individually. This causes the Anthropic API to compute a
new cache key on every call (because the last-message block changes every turn), writing a
new cache entry rather than reading the existing one.

**Fix:** Use a single top-level `cache_control={"type": "ephemeral"}` parameter. This
activates Anthropic's automatic cache breakpoint management: the API places the cache
breakpoint at the last cacheable block and moves it forward as the conversation grows.
Each call correctly reads the prefix cached by the previous call.

### 3. autogen doesn't pass `cache_control` to the Anthropic API

`AnthropicChatCompletionClient` builds `request_args` by cherry-picking specific params
(`top_p`, `top_k`, `stop_sequences`, `metadata`). `cache_control` is not in the list and
gets silently dropped, even via `extra_create_args`.

**Fix:** Monkey-patch `raw_client.messages.create` to inject `cache_control` on every call.
This is implemented in `CachedAnthropicClient` in `../prompt_caching.py`.

### 4. autogen's `RequestUsage` doesn't expose cache token fields

`autogen_core.models.RequestUsage` is a plain dataclass with only `prompt_tokens` and
`completion_tokens`. autogen's Anthropic client extracts `result.usage.input_tokens` and
`result.usage.output_tokens` from the raw Anthropic response and discards
`cache_creation_input_tokens` and `cache_read_input_tokens`.

**Fix:** The `cached_create` wrapper captures cache tokens from the raw `Message.usage`
response and `CachedAnthropicClient.create()` attaches them as dynamic attributes on the
returned `RequestUsage` object:

```python
result.usage.cache_read_tokens = self._last_cache_read
result.usage.cache_creation_tokens = self._last_cache_creation
```

`function_learning.py` then reads them via `getattr(result.usage, "cache_read_tokens", 0)`.

### 5. Minimum cacheable prefix is ~4096 tokens for Haiku 4.5

Anthropic's documentation says "model-dependent (typically 1024-4096 tokens)." Empirically:

| Model               | Min tokens |
| ------------------- | ---------- |
| Claude Opus 4.5/4.6 | 4096       |
| Claude Sonnet 4.6   | 2048       |
| Claude Haiku 4.5    | 4096       |
| Claude Haiku 3.5    | 2048       |

Shorter prompts are processed normally without caching (no error, just 0 in usage fields).
The ~1500 token system prompt alone won't cache, but the growing conversation prefix
(system + tools + prior turns) exceeds 4096 tokens by turn ~5 and caches from there.

## Why skill-arm input_tokens was low in the 2026-03-28 run

The per-block injection was creating new cache entries every turn (issue #2 above), which
means caching WAS activating but always writing, never reading. The Anthropic API reports
only non-cached tokens in `input_tokens` — so the ~250 input_tokens across 50 turns in the
skill arm reflected that most of the prefix was being written to cache each turn (but
wastefully, since it was never re-read). The cache write tokens weren't captured due to
issue #4.

## Implementation

`CachedAnthropicClient` in `../prompt_caching.py`:

1. Wraps `raw_client.messages.create` to inject `cache_control={"type":"ephemeral"}` as a
   top-level parameter and capture cache token counts from the raw response
2. Overrides `create()` to attach the captured counts to `RequestUsage` as dynamic attrs

## Cost Impact (estimated, 30-turn run on Haiku 4.5)

- **Without caching:** ~600K input tokens @ $0.80/1M = $0.48/run
- **With caching (turns 5-30):** ~150K non-cached + ~450K cache-read @ $0.08/1M = ~$0.16/run
- **Savings:** ~67%
