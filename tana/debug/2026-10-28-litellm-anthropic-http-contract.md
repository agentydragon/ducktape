# Tana LiteLLM Anthropic HTTP contract

The runtime probe in `//tana/litellm_proxy:test_proxy_runtime` serves the pinned LiteLLM ASGI app, sends requests with the Anthropic SDK, and replaces only the Firebase/Tana HTTP transport with `httpx.MockTransport`. The selected image dependency is LiteLLM 1.103.0. No Tana credential or live model endpoint is used.

The normal-usage path succeeds end to end: `/v1/messages` streams a Tana `tool_use`, then accepts the assistant tool call and user `tool_result` in a non-streaming continuation. The streamed request includes system text blocks, including one with Anthropic `cache_control`; the provider receives the system text but not its cache marker. A strict expected failure checks for the marker only after the tool-result continuation and its request assertions have passed, so it will become an XPASS if the marker starts arriving.

A separate strict expected failure covers a Tana continuation response without `usage`. It requires an Anthropic HTTP 500 whose body identifies the missing `ModelResponse.usage` attribute before reporting the known failure. The provider accepts this response (`TanaChatResult.usage` is optional) and builds a `ModelResponse` without a `usage` attribute. The RBE traceback points to line 1553 of the installed `translate_openai_response_to_anthropic` adapter. The public v1.103.0 tag has the failing `getattr(response, "usage")` at [`transformation.py:1429`](https://github.com/BerriAI/litellm/blob/v1.103.0/litellm/llms/anthropic/experimental_pass_through/adapters/transformation.py#L1428-L1430); its streaming adapter uses a default for missing usage at [line 1567](https://github.com/BerriAI/litellm/blob/v1.103.0/litellm/llms/anthropic/experimental_pass_through/adapters/transformation.py#L1567-L1576). The probe leaves this boundary unfixed and does not synthesize token counts. If the response adapter later supports absent usage, the current `pytest.raises` check fails and the diagnostic should be updated; the independent cache-marker case strict-XPASSes when that marker is preserved.

The cache marker is lost somewhere in the pinned `/v1/messages` translation before the custom provider request. The probe does not add a compatibility rewrite because the observed request does not establish which representation LiteLLM intends the custom handler to receive.

## Chat Completions comparison

A follow-up local probe serves the same pinned app and uses the OpenAI SDK's
`/v1/chat/completions` route, replacing only Firebase/Tana HTTP transport. All four
streaming/non-streaming × reported/absent-usage cases pass on RBE:
[invocation 48334d3f-d77b-4cea-9ae7-cf76d5447fe1](https://app.buildbuddy.io/invocation/48334d3f-d77b-4cea-9ae7-cf76d5447fe1).
`test_chat_completions_preserves_cache_and_optional_usage` retains these four
cases as HTTP regressions. `test_chat_completions_stream_tool_roundtrip_without_usage`
also exercises streaming tool arguments, then sends an assistant tool call and tool
result through the same HTTP endpoint with a usage-less Tana continuation response.

In every case the system content block's `cache_control` reaches the mocked Tana
endpoint as `providerOptions.anthropic.cacheControl`. Without upstream usage,
non-streaming returns the answer with SDK `usage=None`. Streaming with
`stream_options.include_usage=true` also returns the answer, but LiteLLM supplies
counts (15 prompt, 1 completion tokens for this fixture) despite Tana reporting
none. Those are locally generated counts, not upstream-reported measurements.
When Tana supplies 7 input and 6 output tokens, both modes preserve those totals.
These probes do not establish live Tana behavior.

## Runtime target budget

The expanded runtime suite sometimes exceeded the small target's 60 seconds after
reporting 9 passed and 2 expected failures. Instrumented RBE runs measured
14.6–23.0 seconds before pytest, plus 19.7–30.5 seconds of reported test work.
Thread dumps showed normal pytest capture cleanup and `gc_collect_harder`, with
only the main thread and the diagnostic daemon remaining; no executor or ASGI
shutdown waiter was observed. Three instrumented small runs passed in 46.1–52.9
seconds ([RBE](https://app.buildbuddy.io/invocation/e82ffdc9-7422-437a-a875-eb1a3e0e158c));
a medium probe exited naturally ([RBE](https://app.buildbuddy.io/invocation/d9fe630d-64cc-4359-9300-725bcce5f092)).
Target history also contained passing durations around 36 and 44 seconds.

The target is now medium to accommodate the measured import, integration, and GC
work. The exact original timeout stack was not captured, so these observations
support a budget change rather than establish a repaired shutdown leak. Temporary
watchdog instrumentation is not part of the test suite.
