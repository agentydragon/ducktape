# LiteLLM `/v1/responses` does not emit Langfuse OTEL traces

**Date**: 2026-06-05
**Status**: Open upstream/LiteLLM issue. Cluster wiring is good for
chat-completions, but LiteLLM's Responses-to-chat bridge did not produce
Langfuse traces for the z.ai model path. Measured against `litellm:1.86.3`; the
cluster runs `1.90.2` today (<../k8s/litellm/app/deployment.yaml>) and the
finding has not been re-verified against it.

## Symptom

Cluster LiteLLM is configured with:

- deployment image: `litellm/litellm:1.86.3`
- `litellm_settings.callbacks: ["langfuse_otel"]`
- `LANGFUSE_OTEL_HOST=http://langfuse-web.langfuse.svc.cluster.local:3000`
- Langfuse public/secret keys reflected from `langfuse/langfuse-secrets` into
  the `litellm` namespace

`/v1/chat/completions` calls through `glm-4.6` write traces to Langfuse.
`/v1/responses` calls through the same model return `200 OK`, but write no
trace or observation rows.

## Live repro evidence

Chat-completions positive control:

- marker: `litellm-langfuse-smoke-20260604232418-e4b34cb8`
- Langfuse trace id: `fbec4da96b775979c44d03f63063f024`
- Langfuse observation id: `a43d63b4d609ad52`
- observation type: `GENERATION`
- provided model: `glm-4.6`

Responses negative control:

- marker: `litellm-responses-reprobe-f3928a0447e4`
- LiteLLM returned `POST /v1/responses HTTP/1.1" 200 OK`
- response body contained the marker
- Langfuse ClickHouse query for the marker returned `0` traces and `0`
  observations
- `default.traces` had `0` rows in the 10-minute window after the repro
- LiteLLM logs around the repro had no `LoggingError`, `LoggingWorker error`,
  `OpenTelemetry`, or Langfuse export attempt; only the successful
  `/v1/responses` access log line

## What is not broken

- Langfuse itself is accepting OTEL spans; chat-completions proves this.
- Reflected Langfuse credentials are present in the running LiteLLM process.
- `/active/callbacks` on the running LiteLLM process shows
  `LangfuseOtelLogger` registered in `litellm._async_success_callback`.
- ClickHouse, Langfuse worker, SeaweedFS, and Valkey are not implicated in this
  specific symptom because the chat path writes successfully.

## Root cause in LiteLLM 1.86.3

The z.ai `glm-4.6` route is configured as an OpenAI-compatible chat model with
`use_chat_completions_api = true` because z.ai does not expose a native
OpenAI Responses endpoint. In LiteLLM, `/v1/responses` therefore enters the
Responses-to-chat bridge instead of the provider-native Responses path.

The relevant deployed source path is:

1. `litellm/responses/main.py`
   - `aresponses()` wraps sync `responses()` in an executor.
   - `responses()` returns through
     `litellm_completion_transformation_handler.response_api_handler(...)`
     when `responses_api_provider_config is None` or
     `use_chat_completions_api is True`.
   - That return happens before the provider-native block that calls
     `litellm_logging_obj.update_from_kwargs(...)` for Responses logging.
2. `litellm/responses/litellm_completion_transformation/handler.py`
   - the async bridge calls `litellm.acompletion(**acompletion_args)` and
     forwards the same `litellm_logging_obj`/kwargs.
3. `litellm/utils.py`
   - the sync wrapper has early returns for async markers like
     `acompletion`, `aembedding`, `aimg_generation`, `atranscription`, and
     `aspeech`, but not `aresponses`.
4. `litellm/integrations/opentelemetry.py`
   - `_handle_success()` dedupes spans through `_emit_once(..., "success")`
     using request-local metadata on the shared kwargs.

The net result is that the bridged Responses request does not produce a clean
final Langfuse OTEL success span. The proxy's non-streaming return path does
not independently dispatch final success logging, so no Langfuse row is
created even though the model request succeeds.

## Newer version check

Checked upstream source on 2026-06-05:

- latest stable on PyPI/GitHub at check time: `v1.87.1`
- newest GitHub release candidate at check time: `v1.88.0-rc.3`

Both still have the same relevant structure:

- `responses/main.py` still returns through the Responses-to-chat bridge before
  native Responses `update_from_kwargs(...)`.
- `responses/litellm_completion_transformation/handler.py` still forwards the
  shared kwargs into `litellm.acompletion(...)`.
- `utils.py` still has no sync-wrapper early return for `"aresponses"` in the
  normal post-call branch.

So upgrading from `1.86.3` to `1.87.1` or `1.88.0-rc.3` should not be expected
to fix Langfuse tracing for this path.

Primary sources checked:

- <https://github.com/BerriAI/litellm/releases>
- <https://pypi.org/project/litellm/>
- local source clones:
  - `/tmp/litellm-v1.87.1`
  - `/tmp/litellm-v1.88.0-rc.3`

## Implication for `props/`

Do not rely on Langfuse traces as durable history for `props/` calls that go
through LiteLLM `/v1/responses` until this is fixed.

The `props/` client can still send correlation fields in OpenAI metadata, but
those fields will not land in Langfuse on the current LiteLLM Responses bridge.
Use one of these instead:

- continue using `/v1/chat/completions` for calls that must be logged in
  Langfuse now
- patch/custom-build LiteLLM so the Responses-to-chat bridge emits a final
  Responses success event
- wait for an upstream LiteLLM fix and retest with the marker query above

## Candidate fix shape

The fix likely belongs upstream in LiteLLM, not in cluster manifests:

- either make the Responses-to-chat bridge mark inner completion calls as
  internal and emit exactly one final Responses success event after converting
  `ModelResponse` to `ResponsesAPIResponse`
- or ensure the shared logging object is updated for the bridged Responses
  request before the bridge returns, without consuming the OTEL emit-once guard
  on the inner chat-completion call

Any fix must be verified by sending a `/v1/responses` request with a unique
metadata/output marker and querying Langfuse ClickHouse `traces` and
`observations` for that marker.
