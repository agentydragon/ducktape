# Tana LiteLLM Proxy TODO

This package is the local adapter from LiteLLM chat calls to Tana's
reverse-engineered `llmProxy` and `llmProxyNext` HTTP functions. The source
wire-shape notes live in
`../../../gaffer-private/tana/re/shared/llm-proxy-api.md`; keep changes here
source-faithful to that evidence.

## P0: Required before OpenCode or Claude Code use

- Add OpenAI-compatible smoke tests through LiteLLM Proxy:
  - `/v1/chat/completions`
  - streaming `/v1/chat/completions`
  - function-tool call and tool-result continuation
- Add Anthropic-compatible smoke tests through LiteLLM Proxy:
  - `/v1/messages`
  - streaming `/v1/messages`
  - Claude Code-shaped streaming `/v1/messages` with Anthropic system content
    blocks, `cache_control`, `tool_use`, and `tool_result` transcript entries
  - `/v1/messages/count_tokens`
  - tool use and tool-result continuation
- Make Tana auth deployment-safe:
  - reflect the resigner-maintained `tana-firebase-refresh-token` Secret into
    the LiteLLM namespace
  - populate `TANA_FIREBASE_REFRESH_TOKEN` from that reflected Secret
  - rely on reloader to restart LiteLLM when the reflected Secret changes
  - avoid relying on in-process `kubectl get secret` or direct resigner code in
    the deployed proxy path
  - document the reflected-secret path and restart-based freshness model
- Keep refresh-token ownership in the resigner:
  - LiteLLM reads the reflected refresh-token env var but never owns it
  - LiteLLM may cache Firebase ID tokens, but must not persist or adopt rotated
    Firebase refresh tokens
  - the existing resigner remains the only component that maintains the
    canonical refresh-token secret
  - avoid unless necessary: make the Tana MCP pod expose the currently valid
    renderer/session token to LiteLLM, because that couples two deployments and
    creates a new token-broker surface
- Map upstream failures into useful client-facing errors:
  - Firebase refresh-token exchange failures
  - Tana 401/403 auth failures
  - Tana 429 (out-of-quota / credits exhaustion): currently all `status_code >= 400`
    responses raise a bare `TanaProxyError` (RuntimeError), which LiteLLM wraps as a
    500 `APIConnectionError` — observed live as `Tana llmProxyNext failed with HTTP
429`. This loses the rate-limit status so LiteLLM's 429 retry/backoff never fires
    and agent clients (Claude Code, OpenCode) can't distinguish quota exhaustion from a
    dead server. Inspect the status code at the four `>= 400` sites (non-tool, tool,
    sync stream, async stream in `provider.py`) and raise the matching LiteLLM error
    class (`litellm.RateLimitError` for 429, `AuthenticationError` for 401/403) so the
    original status code and retryability propagate. Add a test that a 429 upstream
    response surfaces to the caller as a retryable rate-limit error, not a 500.
  - Tana 5xx and malformed stream events
- Make the remaining RE-listed models work or intentionally suppress them with
  a documented reason. `bazel run //tana/litellm_proxy:probe_models_bin --` on
  2026-06-10 found these current failures:
  - HTTP 500: `gpt-3.5-turbo-0125`, `gpt-3.5-turbo-instruct`,
    `gpt-4-turbo`, `gpt-4-turbo-2024-04-09`, `gpt-4-turbo-preview`,
    `gpt-4-0125-preview`, `gpt-4`, `gpt-4-32k`, `gpt-4o`,
    `gpt-4o-2024-05-13`, `gpt-4o-2024-11-20`, `gpt-4.1`,
    `gpt-4.1-2025-04-14`
  - HTTP 400 Zod validation: `gpt-5.2/xhigh`, `gpt-5.4/xhigh`
  - The cluster LiteLLM config currently exposes only
    `TANA_LLM_PROXY_RESPONDING_MODELS`; keep the full RE list in
    `model_registry.py` so this can be re-probed without rediscovering the
    model registry.

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
