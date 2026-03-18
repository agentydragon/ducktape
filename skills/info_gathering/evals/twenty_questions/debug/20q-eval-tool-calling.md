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

## Fixes Applied

| File                                                | Fix                                                | Issue                         |
| --------------------------------------------------- | -------------------------------------------------- | ----------------------------- |
| `cluster/k8s/litellm/generate_litellm.py`           | Add `api_key: "ollama"` for openai-chat models     | LiteLLM openai-chat 500 error |
| `cluster/k8s/litellm/proxy-config.yaml`             | Regenerated                                        | (derived from above)          |
| `cluster/k8s/ollama/nginx-auth-proxy.conf.template` | Escape `${OLLAMA_DIRECT_TOKEN}` as `$${}` for Flux | Ollama direct auth 401        |

## To Deploy

These fixes must be merged to `devel` and reconciled by Flux:

1. Merge this branch to `devel`
2. Flux reconciles `ollama` and `litellm` kustomizations (10m interval)
3. After reconciliation, the `openai-chat` models should support tool calling
4. Ollama direct access should also work

## Running the Eval

Once deployed, run:

```bash
bazel run //skills/info_gathering/evals/twenty_questions:twenty_questions_bin -- \
  --variant states \
  --model openai/gpt-oss-20b-128k-openai-chat \
  --base-url https://litellm.allegedly.works/v1 \
  --api-key "$(kubectl get secret litellm-master-key -n claude-sandbox -o jsonpath='{.data.api-key}' | base64 -d)" \
  --thinking-budget 0
```

Or via direct Ollama (after auth fix):

```bash
bazel run //skills/info_gathering/evals/twenty_questions:twenty_questions_bin -- \
  --variant states \
  --model openai/gpt-oss:20b \
  --base-url https://ollama.allegedly.works/v1 \
  --api-key "$(kubectl get secret ollama-direct-token -n claude-sandbox -o jsonpath='{.data.token}' | base64 -d)" \
  --thinking-budget 0
```
