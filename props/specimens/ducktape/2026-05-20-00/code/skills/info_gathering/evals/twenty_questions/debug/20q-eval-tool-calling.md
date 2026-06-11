# 20 Questions Eval: Tool Calling Investigation

**Date**: 2026-03-18

## Goal

Run the 20 Questions eval against the local `gpt-oss:20b` model via
Ollama/LiteLLM. The eval requires real tool calling (not text parsing) for
the simulator to answer yes/no and signal correct guesses.

## Infrastructure

- **Ollama** 0.18.0 on wyrm2 (2x RTX 5090), model `gpt-oss:20b`
- **LiteLLM** proxy 1.82.1 in-cluster, exposes models via OpenAI-compatible
  API at `https://litellm.allegedly.works/v1`
- Two model routes per variant:
  - `*-openai-chat` — LiteLLM uses OpenAI SDK → Ollama `/v1` endpoint
  - `*-ollama-native` — LiteLLM uses native Ollama provider → Ollama native API

## Findings

### 1. `ollama-native` models: tool calls silently stripped

| Metric              | Value        |
| ------------------- | ------------ |
| HTTP status         | 200          |
| `finish_reason`     | `stop`       |
| `content`           | `""` (empty) |
| `tool_calls`        | `null`       |
| `completion_tokens` | 12           |

The model generates 12 tokens (likely reasoning/thinking tokens) but the
response has no content and no tool calls. This happens consistently across
`gpt-oss-20b-128k-ollama-native` and `gpt-oss-120b-128k-ollama-native`.

**Root cause**: LiteLLM's Ollama provider (version 1.82.1) does not properly
surface tool calls from the Ollama native API response. The 12 tokens are
consumed (visible in `completion_tokens`) but the tool call output is lost
in the provider's response translation. This is a known category of issues
with LiteLLM's Ollama provider.

The model CAN generate text normally (tested without tools: "What is 2+2?"
→ "Four", 36 tokens).

### 2. `openai-chat` models: API key misconfiguration

```
litellm.AuthenticationError: The api_key client option must be set either
by passing api_key to the client or by setting the OPENAI_API_KEY
environment variable.
```

**Root cause**: The LiteLLM proxy config defines `openai-chat` models with
`model: openai/gpt-oss:20b` and `api_base: http://ollama.../v1`. LiteLLM's
OpenAI provider creates an OpenAI SDK client for this backend, which requires
an `api_key` even though Ollama doesn't need one. No `api_key` was configured
in the model entry.

**Fix**: Added `api_key: "ollama"` to all openai-chat model entries in
`generate_litellm.py`. Ollama accepts any API key value.

### 3. Direct Ollama access: auth proxy broken

`https://ollama.allegedly.works` returns 401 for all requests, even with the
correct `ollama-direct-token` secret.

**Root cause**: The nginx auth proxy template contains `${OLLAMA_DIRECT_TOKEN}`.
The Flux kustomization for Ollama has `postBuild.substituteFrom`, which
processes all `${...}` patterns in rendered YAML. Since `OLLAMA_DIRECT_TOKEN`
is not in the `cert-manager-issuer-config` ConfigMap, Flux replaces it with
empty string. The nginx template becomes `set $expected "Bearer ";` — matching
nothing.

**Fix**: Escaped the variable as `$${OLLAMA_DIRECT_TOKEN}` in the nginx
template. Flux outputs `${OLLAMA_DIRECT_TOKEN}` literally, and nginx's
envsubst then substitutes the actual token value at container startup.

### 4. `gpt-oss-120b`: insufficient memory

```
model requires more system memory (5.6 GiB) than is available (3.2 GiB)
```

The 120B model doesn't fit in available system memory alongside the 20B model.

### 5. LiteLLM ConfigMap auto-update failure (`subPath` mount)

**Symptom**: After Flux reconciled a ConfigMap change (adding `api_key: ollama`),
the LiteLLM pod's config file still had the old version without the key.

**Root cause**: Kubernetes `subPath` volume mounts don't auto-update when the
underlying ConfigMap changes. The pod was using `subPath: config.yaml` to mount
a single file. Stakater Reloader annotations were present but the ConfigMap
change was too fast for Reloader to detect.

**Fix**: Changed `deployment.yaml` to use a directory mount (`mountPath: /etc/litellm`,
`readOnly: true`) instead of `subPath`. K8s updates directory-mounted ConfigMaps
automatically (~1-2 min propagation).

### 6. OpenAI Harmony format noise in function names

**Symptom**: Model returns function names like `answer<|channel|>commentary`
instead of `answer`.

**Root cause**: OpenAI Harmony format artifact from the model's training.
The `<|channel|>` suffix is part of the model's internal token structure
that leaks into function call names.

**Fix**: Added `_clean_function_name()` to strip `<|...` suffixes from
function names before validation.

### 7. litellm Python client: unreliable tool calling for non-Anthropic models

**Symptom**: The eval consistently failed at turn 4 with 0 tool calls across
all 5 retries, even with `tool_choice="required"`. This happened both via
the LiteLLM proxy and when bypassing it (pointing litellm client directly
at Ollama's `/v1` endpoint). Meanwhile, equivalent `curl` requests to both
endpoints worked perfectly.

**Investigation**: Source analysis of litellm v1.82.0:

- **`litellm/utils.py:3805`**: The `ollama` native provider explicitly drops
  `tool_choice` from requests with comment: "causes ollama requests to hang".
  Tools are also stripped and added to the prompt as text instead.

- **`litellm/utils.py:3062-3066`**: The `openai` provider (used by `openai/`
  prefix models) passes all parameters through without modification:
  `optional_params = non_default_params`. So `tool_choice` should NOT be
  dropped for `openai/` prefix models.

- **`litellm/constants.py:724-779`**: `openai_compatible_providers` list does
  NOT include `"openai"` — `openai` is a first-class provider, not an
  openai-compatible one.

**Conclusion**: While `tool_choice` is correctly passed through for `openai/`
prefix models in theory, the litellm client still fails in practice for
multi-turn conversations. The failure is deterministic (5/5 retries fail at
the same turn), suggesting litellm modifies the message format or response
parsing in a way that causes the model to not produce tool calls. Direct
`curl` with identical conversation structure works perfectly.

**Fix**: Switched `LLMClient` to use `openai.AsyncOpenAI` directly for all
non-`anthropic/` models, bypassing litellm entirely. litellm is kept only
for Anthropic models where it handles provider-specific parameters (thinking
budgets, `max_tokens` vs `max_completion_tokens`).

### 8. Retry logic for malformed tool call arguments

**Symptom**: Model returns `"is it a state?"` as the `response` field instead
of one of the enum values `"yes"/"no"/"sort_of"`, causing Pydantic
`ValidationError`.

**Fix**: Added tenacity retry (5 attempts, `wait_fixed(0)`) around the
simulator call + parse, retrying on both `ValueError` and `ValidationError`.
The `before_sleep` callback logs retry attempts with the error.

## Fixes Applied

| File                                                | Fix                                                | Issue                         |
| --------------------------------------------------- | -------------------------------------------------- | ----------------------------- |
| `cluster/k8s/litellm/generate_litellm.py`           | Add `api_key: "ollama"` for openai-chat models     | LiteLLM openai-chat 500 error |
| `cluster/k8s/litellm/proxy-config.yaml`             | Regenerated                                        | (derived from above)          |
| `cluster/k8s/ollama/nginx-auth-proxy.conf.template` | Escape `${OLLAMA_DIRECT_TOKEN}` as `$${}` for Flux | Ollama direct auth 401        |
| `cluster/k8s/litellm/deployment.yaml`               | Directory mount instead of `subPath`               | ConfigMap auto-update         |
| `skills/.../harness.py`                             | OpenAI SDK for non-anthropic; remove `usage` field | litellm tool_choice issue     |
| `skills/.../twenty_questions.py`                    | tenacity retry + harmony format cleanup            | Malformed tool calls          |

## Running the Eval

Via LiteLLM proxy:

```bash
LITELLM_KEY=$(kubectl get secret litellm-master-key -n litellm \
  -o jsonpath='{.data.api-key}' | base64 -d)

bazel run //skills/info_gathering/evals/twenty_questions:twenty_questions_bin -- \
  --variant states \
  --model openai/gpt-oss-20b-128k-openai-chat \
  --base-url https://litellm.allegedly.works/v1 \
  --api-key "$LITELLM_KEY" \
  --thinking-budget 0
```

Via direct Ollama (bypassing LiteLLM proxy):

```bash
OLLAMA_TOKEN=$(kubectl get secret -n ollama ollama-direct-token \
  -o jsonpath='{.data.token}' | base64 -d)

bazel run //skills/info_gathering/evals/twenty_questions:twenty_questions_bin -- \
  --variant states \
  --model openai/gpt-oss:20b \
  --base-url https://ollama.allegedly.works/v1 \
  --api-key "$OLLAMA_TOKEN" \
  --thinking-budget 0
```

## curl Verification

Both endpoints confirmed working with multi-turn tool calling via curl:

```bash
# Via LiteLLM proxy (4-turn conversation with tool_choice=required)
curl -s https://litellm.allegedly.works/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-oss-20b-128k-openai-chat",
    "messages": [
      {"role": "system", "content": "Judge. Secret: New Mexico."},
      {"role": "user", "content": "Is it a place?"},
      {"role": "assistant", "content": null, "tool_calls": [...]},
      {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
      {"role": "user", "content": "Is it in the western half?"}
    ],
    "tools": [...],
    "tool_choice": "required"
  }'
# Result: tool_calls present, finish_reason: "tool_calls" ✓
```

Both LiteLLM proxy and direct Ollama return correct `tool_calls` with
`finish_reason: "tool_calls"` for multi-turn conversations via curl.
The Python litellm client is the only path that fails.

## Eval Run Results (2026-03-18)

### Via LiteLLM proxy (openai SDK, post-fix)

Reached turn 12 (vs turn 4 pre-fix), then failed with 5/5 retries exhausted
(0 tool calls from simulator). Intermittent tool call failures at turns 7, 9,
11, 12 — the model sometimes doesn't produce tool calls even with
`tool_choice="required"`. Some turns recovered via retry; turn 12 did not.

Also saw `ExecInput` validation errors at turn 5 — the scratch `exec` tool
requires `cwd`, `env`, `user`, `timeout_ms` fields that the model doesn't
provide. This is a pre-existing schema issue, not a transport problem.

### Via direct Ollama (openai SDK)

`APITimeoutError` — the model takes too long when processing the full
conversation history with scratch tool schema. Default openai SDK timeout
(600s) exceeded. This is a model inference speed issue, not a tool-calling
problem.

### Assessment

The tool-calling infrastructure is working end-to-end. The remaining failures
are model reliability issues (intermittent tool call omission, slow inference)
rather than transport/client problems. The switch from litellm to openai SDK
resolved the deterministic failure at turn 4.

## Eval Run Results (2026-03-19) — with sim retry logic

Re-added tenacity retry (5 attempts) around the sim call after the
DirectToolProvider refactoring removed it. The eval now runs to completion.

### Via LiteLLM proxy (openai SDK, `gpt-oss-20b-128k-openai-chat`)

Result: **Timeout after 20 turns** (did not guess "New Mexico").

Retry log:

- Turn 9: 4 retries (mix of "no tool call" and "invalid action"), succeeded on attempt 5
- Turn 12: 3 retries ("no tool call"), succeeded on attempt 4

The model went down wrong paths (Northeast → Connecticut, then reset to South).
`ExecInput` validation errors persist (model doesn't provide required scratch
tool fields) but are handled by the agent's `resolve_tool_calls` loop.

### Assessment

The retry fix makes the eval robust against intermittent sim tool call failures.
The remaining issue is model quality — `gpt-oss:20b` is not reliably good at
20 Questions with the current SKILL.md prompt.

## OpenAI Responses API (`/v1/responses`) — Verified Working

Tested 2026-03-18. LiteLLM proxies the OpenAI Responses API to Ollama
successfully. Both text completion and tool calling work:

```bash
# Text completion
curl -s -X POST https://litellm.allegedly.works/v1/responses \
  -H "Authorization: Bearer $LITELLM_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "openai/gpt-oss:20b", "input": "What is the capital of France?"}'
# → output includes message with text "Paris" ✓

# Tool calling
curl -s -X POST https://litellm.allegedly.works/v1/responses \
  -H "Authorization: Bearer $LITELLM_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openai/gpt-oss:20b",
    "input": "Use the lookup_capital tool to find the capital of France.",
    "tools": [{
      "type": "function", "name": "lookup_capital",
      "description": "Look up the capital city of a country.",
      "parameters": {"type": "object", "properties": {"country": {"type": "string"}},
                     "required": ["country"], "additionalProperties": false},
      "strict": true
    }]
  }'
# → output includes function_call with name "lookup_capital",
#   arguments '{"country":"France"}' ✓
```

This means `agent_core` (which uses the Responses API via `openai.AsyncOpenAI`)
can work with the Ollama/LiteLLM stack. A smoke test exists at
`agent_core/test_ollama_tool_calling.py` (mock target passes; live target
requires `OPENAI_API_KEY` + `OPENAI_BASE_URL` + `OPENAI_MODEL` env vars).

## Running the 20Q Eval with gpt-oss via LiteLLM

The eval uses Chat Completions API (via `LLMClient`), not the Responses API.

### Via LiteLLM proxy

```bash
LITELLM_KEY=$(kubectl get secret litellm-master-key -n litellm \
  -o jsonpath='{.data.api-key}' | base64 -d)

bazel run //skills/info_gathering/evals/twenty_questions:twenty_questions_bin -- \
  --variant states \
  --model openai/gpt-oss-20b-128k-openai-chat \
  --base-url https://litellm.allegedly.works/v1 \
  --api-key "$LITELLM_KEY" \
  --thinking-budget 0
```

### Via direct Ollama (bypassing LiteLLM proxy)

```bash
OLLAMA_TOKEN=$(kubectl get secret -n ollama ollama-direct-token \
  -o jsonpath='{.data.token}' | base64 -d)

bazel run //skills/info_gathering/evals/twenty_questions:twenty_questions_bin -- \
  --variant states \
  --model openai/gpt-oss:20b \
  --base-url https://ollama.allegedly.works/v1 \
  --api-key "$OLLAMA_TOKEN" \
  --thinking-budget 0
```

### Notes

- Use `--thinking-budget 0` for non-reasoning models (gpt-oss doesn't support thinking).
- Use `--variant wide` for the broader domain variant (25-turn limit).
- Results are saved to `eval_results/` with timestamped directories.
- The `openai-chat` model suffix is required for LiteLLM — the `ollama-native` variant
  drops tool calls (see finding #1 above).
- The eval requires Docker for the scratch container (agent's exec tool).

### agent_core Responses API smoke test

Separate from the eval — tests `agent_core` (Responses API) against Ollama/LiteLLM:

```bash
LITELLM_KEY=$(kubectl get secret litellm-master-key -n litellm \
  -o jsonpath='{.data.api-key}' | base64 -d)

# Mock test (no cluster needed)
bazel test //agent_core:test_ollama_tool_calling.mock

# Live test (requires Ollama/LiteLLM)
OPENAI_API_KEY=$LITELLM_KEY \
  OPENAI_BASE_URL=https://litellm.allegedly.works/v1 \
  OPENAI_MODEL=openai/gpt-oss:20b \
  bazel test //agent_core:test_ollama_tool_calling.live
```
