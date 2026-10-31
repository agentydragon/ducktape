# Provider reasoning continuation spike

Manual request-validity probes against CLIProxyAPI's native `/v1/responses` and
`/v1/messages` routes, using `gpt-6-luna` and `claude-haiku-5-5`. A seed response
supplies genuine complete reasoning and a synthetic function call. Subsequent
requests replay selected parts of that response. No model tool is executed.

The seed, normal tool-result continuation and whole-response abandonment must
complete. Other scenarios record acceptance or rejection; a passing test does not
mean those requests were all accepted. The seed must actually contain complete
opaque reasoning and a tool call, so a model skipping thinking fails explicitly.
The report contains opaque-field sizes/hashes, validation errors and usage
attribution, without credentials or the opaque reasoning payloads themselves.

## Run

Use an existing CLIProxyAPI client key in a mode-`0600` file. The test runs locally
to read that file; build actions retain remote execution and caching. Inherit the
host's configured certificate trust if needed by Bazel's Python runtime.

```bash
export CLIPROXY_BASE_URL=https://cli-proxy-api.allegedly.works/v1
export CLIPROXY_KEY_FILE=/path/to/client-key
export SPIKE_PROVIDER=gpt  # repeat with haiku
bazelisk test //agentplane/x/provider_continuation:test_reasoning.live \
  --strategy=TestRunner=local \
  --test_env=SSL_CERT_FILE \
  --test_env=CLIPROXY_BASE_URL \
  --test_env=CLIPROXY_KEY_FILE \
  --test_env=SPIKE_PROVIDER \
  --test_tag_filters=live_openai_api --nocache_test_results --test_output=all
```

An independently configured BuildBuddy `CLIPROXY_CLIENT_KEY` secret also supports
remote execution; omit `CLIPROXY_KEY_FILE` in that mode. This spike does not create
or synchronize secrets. Each run makes at most seven requests, with no client
retries, a `4096` output-token ceiling per request and a `30s` socket timeout.
CLIProxyAPI may perform its configured upstream bootstrap retries.

The report is `<provider>.json` in Bazel's undeclared test outputs. This is a
continuation-shape experiment using a completed seed stream, not a process-kill
or sampler failover test. Provider acceptance through a proxy does not prove
that every supplied block reached the upstream unchanged.

## Observed 2026-10-31

Direct endpoint: `https://cli-proxy-api.allegedly.works/v1`. Deployed proxy image:
`git.allegedly.works/ducktape-ci/cli-proxy-api:devel-20261028113038-7a7e793`.
Luna used `reasoning.effort=high`; Haiku used adaptive thinking, high effort and
`block_binding.prefix_mismatch_behavior=error`.

| Request after the seed                            | Luna                                        | Haiku                                                    |
| ------------------------------------------------- | ------------------------------------------- | -------------------------------------------------------- |
| Full response plus matching synthetic tool result | `200`, completed                            | `200`, completed                                         |
| Complete reasoning only, at context tail          | `200`, completed                            | `400`: final assistant block cannot be `thinking`        |
| Complete reasoning followed by recovery notice    | `200`, completed                            | `200`, completed                                         |
| Incomplete reasoning representation               | `200`, completed; retention not established | `200`, completed; unsigned thinking is filtered by proxy |
| Full response with unanswered tool call           | `400`: missing tool output                  | `400`: missing immediately following tool result         |
| Discard response and append recovery notice       | `200`, completed                            | `200`, completed                                         |

For Luna, the complete reasoning item had `2788` encrypted bytes. The normal
continuation, reasoning-only tail and reasoning-plus-notice requests each
attributed `157` input tokens to that exact reasoning item. The incomplete case
used the original `response.output_item.added` item; it attributed only `6`
input tokens to that item and generated fresh reasoning. Acceptance of that
shape does not establish restoration of its complete reasoning.

For Haiku, the complete thinking signature had `2088` bytes. The signed-thinking
plus notice request reported no upstream input transformations. The unsigned
case removed the signature from that same block. The pinned proxy's
[signature sanitizer](https://github.com/agentydragon/CLIProxyAPI/blob/0f5c54cd6a1393447484eee02149bf7fab2e4784/internal/signature/claude_messages_sanitize.go#L122)
drops incompatible thinking blocks before its
[streaming upstream call](https://github.com/agentydragon/CLIProxyAPI/blob/0f5c54cd6a1393447484eee02149bf7fab2e4784/internal/runtime/executor/claude_executor_stream.go#L213).
Thus `input_transformations: []` cannot establish that unsigned thinking survived
the proxy.

Live invocations:

- [Luna](https://app.buildbuddy.io/invocation/7e20e72d-1566-4511-a3f4-efcccaf65d52): passed, seven requests.
- [Haiku](https://app.buildbuddy.io/invocation/bb28f441-0fbf-478c-8dd6-ca5e0422fba1): passed, seven requests.

An initial medium-effort Luna seed returned only a function call and failed the
non-vacuity check. The successful run explicitly selected high effort.

## Recovery implications

Whole-response abandonment can exclude an interrupted response, retain its
observations for History, and append a recovery notice. Both tested routes
accepted that request shape. Earlier committed responses, matching tool outputs
and already executed effects remain intact, including earlier steps in the same
agent turn. The abandoned response's tool calls were never executed.

Retaining a completed reasoning item from an unfinished response requires
provider-specific request construction. A complete signed Haiku thinking block
cannot simply become the context tail. Reasoning completeness alone also does
not settle tool-call/result pairing. Any selective retention must preserve opaque
provider fields, valid ordering and required call/results, and report the actual
continuation disposition through `ConversationReconciled`.
