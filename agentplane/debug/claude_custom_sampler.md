# Custom Claude Messages sampler evidence

2026-10-27 PDT; experimental worker `/root/central_codex_spike`.

A custom Messages loop completed a streamed tool/continuation exchange through the pinned
CLIProxyAPI server against a scripted upstream, while a separate executor process performed
the file append. This establishes a local model-wire path for this pin. No real Anthropic
provider, OAuth credential or subscription was exercised.

## Reproduction and scope

The [experiment](../native/claude/x/custom_sampler/README.md) has two runnable binaries: a
165-line sampler and a 60-line executor (225 runtime lines); tests and fixtures add 363 lines.
The sampler uses `aiohttp` and the standard library, without the Anthropic SDK or Claude CLI.
The executor adds Pydantic for its request boundary. Test dependencies are pytest, the existing
scripted SSE/endpoint helpers and the real pinned CLIProxyAPI Go server. No dependencies or
upstream patches were added.

The proxy is built from the repository's isolated `@ducktape_cli_proxy_api` Bazel module,
whose fork pin is `0f5c54cd6a1393447484eee02149bf7fab2e4784`
([module](../../third_party/cli_proxy_api/MODULE.bazel)). It receives a fake API-key credential
with an explicit `fingerprint-profile: claude-code-cli`, a loopback upstream and local embedded
model catalogs. Model label: `claude-sonnet-4-6`. All credentials and returned content are
synthetic. The executor is a separate process with an explicit minimal environment and workspace;
the fixture checks its PID and absence of the sampler credential from its environment. These
processes still share a test worker and failure domain.

```sh
bbr test //agentplane/native/claude/x/custom_sampler:test_sampler \
  //agentplane/native/claude/x/custom_sampler:sampler \
  //agentplane/native/claude/x/custom_sampler:executor --test_output=errors
```

[RBE invocation](https://app.buildbuddy.io/invocation/fb4b6e03-250c-4ccc-a87a-dfaaba28ba66):
12 cases passed, none skipped; test action 14.856 seconds, pytest 6.407 seconds. Both changed
libraries were named explicitly so Ruff and mypy ran. Gazelle's package diff and Ruff also passed.

## Observations

| Scenario                                         | Observed result                                                                                                                                                                                                                                                                                                                                             |
| ------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Direct Messages loop                             | Scripted thinking plus `append_file` produces a real `hello\n` append in the separate executor, then a `tool_result` continuation and final answer.                                                                                                                                                                                                         |
| Proxy profile and tool names                     | The caller sends no CLI User-Agent, X-App or native metadata. The proxy adds `claude-cli/2.1.280 (external, cli)`, `X-App: cli`, Claude beta headers, billing/identity system blocks and `metadata.user_id`. `append_file` becomes an MCP-shaped upstream alias, returns to the sampler as `append_file`, and uses the same alias in upstream continuation. |
| Thinking and continuation                        | Thinking text and a syntactically valid synthetic signature survive the proxy and return unchanged in the next upstream request. The signature is fixture data, not a real provider signature. Synthesized device/account/session identity and the system prefix remain equal across both requests.                                                         |
| Cache shape                                      | The injected system identity block and first user text have ephemeral `1h` markers. The continuation's final `tool_result` also has a marker. Tools are unmarked. These are wire observations; there is no actual cache-hit or cost measurement.                                                                                                            |
| Opaque complete blocks, direct route             | Both `redacted_thinking` and an unknown future block retain their fields and nested extension data in continuation. Unknown incremental delta formats fail explicitly; their reconstruction is not guessed.                                                                                                                                                 |
| Provider failures, direct route                  | HTTP 503, an SSE error, and EOF after a complete tool block but before `message_stop` all stop without executing the partial tool or retrying inference.                                                                                                                                                                                                    |
| Interrupt while sampling, direct route           | Cancelling after partial tool input closes the provider exchange; no file is created and no partial assistant message is committed.                                                                                                                                                                                                                         |
| Interrupt after an executor effect, direct route | A test relay waits after the real append but before returning its receipt. Cancellation leaves `once\n` on disk and the assistant tool call without a result. Reusing that sampler is rejected; no operation or model request is replayed.                                                                                                                  |
| Executor paths                                   | Traversal, an absolute path and a symlink escape receive HTTP 403. The fixture does not claim protection against concurrent symlink replacement.                                                                                                                                                                                                            |

The first run incorrectly expected a tool cache marker. Captures showed system/user markers;
the assertion was corrected to the observed contract before the successful run.

## Captures

The invocation's undeclared outputs include `test_tool_continuation[profile]/provider.json`:
both upstream request bodies and selected identity headers. The second body contains the
thinking signature, aliased tool call and executor receipt. The direct route's counterpart
shows the caller-shaped baseline. Opaque-block and failure/cancellation captures are named
after their cases; process logs are under `native/`. Authentication headers are never captured.

The capture was inspected after the passing run. Its system prefix has the Claude identity
and ephemeral `1h` marker; the second request retains that prefix and identity and contains
`tool_use_id: toolu_append`, `appended: 6`, and the executor PID. The test checks the final
answer and sampler-side tool name as well as this upstream continuation.

## Compatibility boundary and recommendation

The [earlier source investigation](claude_execution_placement.md#a-custom-sampler-in-front-of-cliproxyapi)
describes native detection and cloaking. This executable slice demonstrates that the custom
sampler need not satisfy native CLI detection to complete the scripted proxy exchange when
the credential explicitly enables the CLI fingerprint. It does not demonstrate that a
provider will accept the synthesized identity, or that any account is eligible for subscription
inference. API-key fingerprint policy and genuine OAuth lifecycle remain separate contracts.
No native CLI/SDK-shaped caller variant was needed for this scripted slice.

Allowed-route availability checks did not establish a usable live probe: LLM ingress
`GET /agentplane/model-config` returned HTTP 502, and the allowed cheap LiteLLM model listing
did not return before cancellation. There is no direct authorized Claude/CLIProxyAPI route in
the current egress rules. No inference request, account change or staging mutation was made.

Keep a custom Messages facade as a viable experiment alongside the native-tool investigation.
The bounded wire loop is small, but this size excludes the harness contracts that dominate a
replacement: instruction discovery and scoping, edits and processes, permission hooks,
compaction, multi-turn input, durable state, ownership/fencing and effect-aware recovery.
Persistence is in memory only. There is no Postgres implementation in this slice, no replica
handoff, no executor kill/reconnect test, no authenticated Gateway, no network-denial proof,
no remote failure-domain isolation and no measured latency or provider spend.

The file executor has no authentication and no cancellation protocol. A cancelled HTTP request
does not fence a file operation already running in its thread. The sampler is deliberately
single-use and makes no exactly-once guarantee; a new process cannot infer an old operation's
outcome. A durable sampler must record intent and reconcile unknown effects before continuing.
These requirements remain open, as do actual provider acceptance and any desired Claude built-in
semantics. No runtime cutover follows from these results.
