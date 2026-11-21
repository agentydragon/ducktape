# Tana LiteLLM Proxy TODO

This package is the local adapter from LiteLLM chat calls to Tana's
reverse-engineered `llmProxy` and `llmProxyNext` HTTP functions. The source
wire-shape notes live in
`../../../gaffer-private/tana/re/shared/llm-proxy-api.md`; keep changes here
source-faithful to that evidence.

## P0: Required before OpenCode or Claude Code use

- Add an Anthropic-compatible `/v1/messages/count_tokens` smoke test through
  LiteLLM Proxy.
- Map upstream failures into useful client-facing errors:
  - Firebase refresh-token exchange failures
  - Tana 5xx and malformed stream events
- Re-probe the RE-listed models and either support or intentionally suppress
  those that fail. The 2026-06-10 probe is historical; current model behavior is
  unverified. Keep the full list in `model_registry.py` and expose only the
  supported subset. The direct `probe_models_bin` refreshes Firebase credentials
  without persisting rotated refresh tokens; establish a safe credential path
  before a live re-probe.

## P1: Agent-client compatibility polish

- Decide code ownership before hardening the deployment:
  - keep deployment/image/Kubernetes wiring in `ducktape`
  - keep reverse-engineering notes and source evidence in
    `gaffer-private/tana/re`
  - if moving the adapter implementation to `gaffer-private`, define how the
    Ducktape image build consumes it without ad-hoc cross-repo state
- Preserve richer finish reasons instead of returning only `stop` or
  `tool_calls`.
- Preserve cache usage metadata:
  - `cachedInputTokens`
  - `cacheCreationInputTokens`
  - provider-specific Anthropic cache creation metadata
- Tighten tool-choice handling:
  - `tool_choice: "none"`
  - forced named tool choices
  - legacy OpenAI `function_call`
  - `parallel_tool_calls` behavior
- Distinguish streamed tool-output events from tool-call deltas.
- Add support for LiteLLM/SDK metadata that matters for tracing:
  - request id
  - user
  - tags or `litellm_metadata`
  - provider warnings
- Add a proxy healthcheck that proves:
  - refresh-token exchange works
  - Tana `llmProxy` accepts a tiny request
  - Tana `llmProxyNext` accepts a tiny tool request

## P2: Nice to have

- Structured output support via Tana `schemaName` if LiteLLM
  `response_format` can map cleanly.
- Multimodal message normalization if Tana accepts image/PDF blocks through the
  observed message envelope.
- Optional prompt-cache helper that adds Anthropic `cacheControl` annotations to
  large stable messages.
- Model capability metadata generation from the Tana RE model registry.
- Better local demo coverage for:
  - Anthropic-shaped requests
  - LiteLLM Proxy config
  - OpenCode config
  - Claude Code gateway config
