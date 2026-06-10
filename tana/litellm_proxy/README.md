# Tana LiteLLM Provider

This package is a small LiteLLM custom provider for Tana's internal
`llmProxy` endpoint. It is intended as a development/demo integration, not as a
stable public Tana API.

## Usage

```bash
bazelisk run //tana/litellm_proxy:demo -- \
  --model tana/claude-3-5-sonnet-latest \
  --prompt 'Reply with exactly: tana-litellm-ok'
```

Tool-call smoke:

```bash
bazelisk run //tana/litellm_proxy:demo -- \
  --model tana/claude-3-5-sonnet-latest \
  --tool-demo
```

Streaming smoke:

```bash
bazelisk run //tana/litellm_proxy:demo -- \
  --model tana/claude-3-5-sonnet-latest \
  --stream \
  --prompt 'Reply with exactly: tana-stream-ok'
```

Streaming tool-call smoke:

```bash
bazelisk run //tana/litellm_proxy:demo -- \
  --model tana/claude-3-5-sonnet-latest \
  --stream \
  --tool-demo
```

The model prefix before the first slash is LiteLLM's custom provider name. The
provider strips `tana/` and sends the remainder as Tana's `options.model`.

## Authentication

The provider exchanges a Firebase refresh token for a Firebase ID token, then
uses that ID token as `Authorization: Bearer <id-token>` when calling:

```text
POST https://app.tana.inc/functions/llmProxy
POST https://app.tana.inc/functions/llmProxyNext
```

Refresh token lookup order:

1. `TANA_FIREBASE_REFRESH_TOKEN`
2. `TANA_FIREBASE_REFRESH_TOKEN_FILE`
3. Kubernetes secret via local `kubectl`

The default secret is `tana-mcp/tana-firebase-refresh-token`, key
`refresh_token`, matching the in-cluster Tana MCP setup.

The default `userContext` is `Generic AI Query`, one of the labels observed in
the Tana client. The live endpoint rejects arbitrary labels during request
validation. Treat it as a Tana action/accounting label, not as LLM prompt
content; model input goes in `args.messages`.

## LiteLLM Mapping

The provider maps basic OpenAI-style chat fields onto Tana's request shape.
OpenAI/LiteLLM `messages` are always sent as Tana `args.messages` envelopes;
the provider does not collapse chat into a single prompt string.

| LiteLLM/OpenAI option                  | Tana option                     |
| -------------------------------------- | ------------------------------- |
| `model="tana/<model>"`                 | `args.options.model="<model>"`  |
| `messages`                             | `args.messages`                 |
| `temperature`                          | `args.options.temperature`      |
| `top_p`                                | `args.options.topP`             |
| `max_tokens` / `max_completion_tokens` | `args.options.maxOutputTokens`  |
| `stop`                                 | `args.options.stopStrings`      |
| `frequency_penalty`                    | `args.options.frequencyPenalty` |
| `presence_penalty`                     | `args.options.presencePenalty`  |
| `tools`                                | `llmProxyNext.dynamicTools`     |
| `stream=True`                          | `isStreaming: true`             |

Message normalization preserves message boundaries and maps common OpenAI/AI SDK
aliases into the Tana core-message shape:

- message and content-block `provider_options` become `providerOptions`
- content blocks with `type: "input_text"` become Tana `type: "text"` blocks
- assistant `tool_calls` become `content` blocks with `type: "tool-call"`
- OpenAI `tool` messages become `content` blocks with `type: "tool-result"`

When `tools` are present, the provider switches to `llmProxyNext`, sends
source-observed `userContext: "Ask Tana"`, and maps OpenAI function tools to
Tana client-runtime dynamic tools:

```json
{
  "name": "tool_name",
  "description": "Tool description",
  "kind": "mcpTool",
  "runtime": "client",
  "schema": { "type": "object", "properties": {} }
}
```

Returned Tana `toolCalls` are converted back into OpenAI-style
`message.tool_calls`. The provider does not execute tools locally; callers
should execute the returned function calls and continue the chat themselves.

For `stream=True`, the provider returns LiteLLM streaming chunks. Plain text
streaming parses Tana `data: {"type":"text-delta",...}` and AI SDK-style
`0:"..."` records. Tool streaming parses `llmProxyNext` tool-input events into
OpenAI-style `delta.tool_calls`; Tana can emit an initial empty `{}` tool
argument delta before the full JSON arguments, and the demo merges those deltas
before printing.
