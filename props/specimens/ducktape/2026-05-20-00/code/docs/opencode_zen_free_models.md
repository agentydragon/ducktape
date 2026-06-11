# OpenCode Zen — Free LLM Proxy

OpenCode (`github.com/anomalyco/opencode`) ships a free model proxy at
`https://opencode.ai/zen/v1/`. No account, API key, or subscription required —
auth is the literal string `"public"`.

## Available Free Models

| Model                        | Provider       | Context | Notes                |
| ---------------------------- | -------------- | ------- | -------------------- |
| `big-pickle`                 | MiniMax (M2.5) | 200k    |                      |
| `gpt-5-nano`                 | OpenAI         | 400k    |                      |
| `grok-code`                  | xAI            | 256k    |                      |
| `qwen3.6-plus-free`          | Alibaba        | 1M      |                      |
| `mimo-v2-pro-free`           | Moonshot       | 1M      |                      |
| `mimo-v2-flash-free`         | Moonshot       | 262k    |                      |
| `mimo-v2-omni-free`          | Moonshot       | 262k    |                      |
| `kimi-k2.5-free`             | Moonshot       | 262k    |                      |
| `glm-5-free`                 | Zhipu AI       | 205k    |                      |
| `glm-4.7-free`               | Zhipu AI       | 205k    |                      |
| `minimax-m2.5-free`          | MiniMax        | 205k    |                      |
| `minimax-m2.1-free`          | MiniMax        | 205k    |                      |
| `nemotron-3-super-free`      | NVIDIA         | 205k    |                      |
| `trinity-large-preview-free` | ?              | 131k    | No reasoning support |

All except `trinity-large-preview-free` support tool calling and reasoning.

## API Protocols

The proxy speaks four API formats at the same base URL:

| Format                  | Endpoint                               | Auth Header                    |
| ----------------------- | -------------------------------------- | ------------------------------ |
| OpenAI Chat Completions | `POST /chat/completions`               | `Authorization: Bearer public` |
| Anthropic Messages      | `POST /messages`                       | `x-api-key: public`            |
| OpenAI Responses        | `POST /responses`                      | `Authorization: Bearer public` |
| Google GenAI            | `POST /models/{model}:generateContent` | `x-goog-api-key: public`       |

Base URL: `https://opencode.ai/zen/v1/`

## Usage Examples

Python: openai.chat.completions client, model="big-pickle".

## Rate Limiting

- Per-IP daily request limit (exact numbers configured server-side, not public)
- New IPs get 2x the daily limit for ~7 days
- Token-based promo limits also tracked

## CI Considerations

Usable headlessly — no interactive auth or browser flow needed. The OpenAI Chat
Completions endpoint is the most universal choice for integration.

Risks for CI use:

- **Rate limits**: shared CI runner IPs may exhaust daily quotas quickly
- **Availability**: free tier with no SLA — not suitable for blocking CI gates
- **Model churn**: free model list may change without notice

Best suited for non-critical CI tasks: PR summary generation, commit message
drafting, optional code review suggestions. Not for anything where a failure
should block merges.
