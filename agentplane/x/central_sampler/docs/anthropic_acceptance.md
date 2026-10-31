# Anthropic acceptance and rollout

The [SPEC](../SPEC.md) defines direct organization API access, native conversation storage,
durable exchanges and on-demand compaction. The implementation landed in
[PR #13085](https://github.com/agentydragon/ducktape/pull/13085), native storage in
[PR #13142](https://github.com/agentydragon/ducktape/pull/13142), and the API package split in
[PR #13145](https://github.com/agentydragon/ducktape/pull/13145).

The initial [compatibility probe](https://github.com/agentydragon/ducktape/blob/b7aadd3711566013daffb1a01ddacacf1696db08/agentplane/debug/2026-10-30-anthropic-compaction-compatibility.md)
observed LiteLLM 1.103.0 dropping the top-level on-demand `compaction` field in its optional-parameter
filter. Direct API access bypasses that path. Any future proxy route must verify that the actual
upstream request retains this field; preserving the beta header alone is insufficient.

## Automated evidence

The [real Haiku run](https://app.buildbuddy.io/invocation/c21dd227-b37f-4f58-88e3-a7d660d91d93)
passed all three cases of
[`test_live_anthropic_provider.py`](../test_live_anthropic_provider.py) against
`claude-haiku-5-5` through the sampler service on BuildBuddy:

- Strict JSON `exec` and nested shell execution; signed thinking retained across manual
  compaction, sampler restart, fact recall and another tool call.
- Prompt-cache creation and reads, including a read after changing effort, with diagnostics
  linked to the previous provider message.
- Automatic compaction followed by resumed sampling and recall.

The tests inspect provider usage and native request/response records persisted in PostgreSQL.
They do not infer cache hits from latency or merely from sending `cache_control`.

**Cache coverage:** that historical run added a stable instruction prefix of `"copper "`
repeated 3,072 times, establishing reuse for padded inputs. The current cache case uses the
ordinary sampler instructions and tools with no custom instructions or padding. It retains
positive `cache_creation_input_tokens` on the first turn and positive `cache_read_input_tokens`
on the next two turns, including after the effort change, and checks unchanged system/tool
input and preserved message prefixes. The separate staging evidence below uses unpadded input;
neither case establishes a fleet-wide hit rate.

The [recursive sampler run](https://app.buildbuddy.io/invocation/89794c99-f6f1-4c7c-b5a5-e5dedce11344)
passed both native Go suites and all five Python service integration suites. That run exposed
root Go test references missed during extraction; after those test-only corrections, the
[root Go integration suite passed](https://app.buildbuddy.io/invocation/6d5b9c26-ffb7-4c40-90db-6e901af3c051).
[PR #13145's final CI](https://github.com/agentydragon/ducktape/actions/runs/41141668255)
also passed. Coverage includes real PostgreSQL, Gateway/History, executor processes, migration,
recovery, streaming failures and context replacement. Database fault injection verifies that
failed request journaling prevents dispatch and failed response journaling leaves the pending
effect unresolved. [Chart/catalog validation](https://app.buildbuddy.io/invocation/1e3edcd3-2d1c-4ac7-b72d-4559fc323fa3)
passed for the initial feature.

## Staging observation

Checked on 2026-10-31 at 00:03 UTC:

- `agentplane-staging/central-sampler` ran
  `devel-20261030233921-7c9aa1f`, whose source commit includes all three merged changes above.
  The Deployment observed its current generation, with one ready replica and zero container
  restarts. Its `--initialize` database container completed with exit code zero.
- Flux's `ducktape-flux/agentplane-staging` Kustomization was Ready and Healthy, with applied
  origin revision `203f7bed972f84878b8c36c87490a66d96e8363e`. The sampler's gRPC endpoint was ready
  and serving.
- Live settings declared Opus 5.5, Sonnet 5.5, Haiku 5.5 and Fable 5.1 routes using
  `https://api.anthropic.com`, with caching, cache diagnostics, per-message effort, strict/eager
  tools, thinking-block validation and ordinary compaction enabled.
- The ready ExternalSecret supplied the organization key from `litellm/litellm-anthropic-key`;
  the sampler mounted it as a credential file. Direct DNS/TLS egress to `api.anthropic.com`
  was present. Direct requests use the organization API account and bypass LiteLLM virtual-key
  budgets.

This observation establishes rollout and configuration. It did not submit a new staging LLM
conversation or measure staging cache reuse. The automated tests above run separate service
instances on BuildBuddy.

## Unpadded staging cache reuse

On 2026-10-31 at approximately 00:27 UTC, a disposable Sandbox and Session exercised the
staging Dispatcher → Gateway → central sampler → direct organization Anthropic API path.
The sampler ran `devel-20261030233921-7c9aa1f`; the model was `claude-haiku-5-5`.
Session `bdc24ec2-bef4-4423-9120-961f16582cb9` retains the native requests, responses and usage
in the sampler database.

The Session used no custom instructions or padding: the system text exactly matched the
21,757-character bundled base instructions, with the ordinary generated `exec` tool definition
and environment context. Three short prompts asked the model to remember and recall a phrase;
no model tool calls were requested. All three turns completed; both recall replies returned the
correct phrase, `amber heron`.
The following counts came from persisted provider response usage:

| Turn | Effort | Cache created | Cache read | Uncached input | Output |
| ---- | ------ | ------------: | ---------: | -------------: | -----: |
| 1    | low    |         9,870 |          0 |              4 |      4 |
| 2    | low    |            35 |      9,870 |              4 |      6 |
| 3    | high   |            45 |      9,905 |              4 |      6 |

Stored wire requests matched their decoded request bodies. System text, tool definitions and
headers remained identical, and each request retained the preceding request's message prefix.
The effort change appended an empty system control message with `output_config.effort: high`;
the top-level effort stayed `low`, preserving the cached prefix. The request's
`diagnostics.previous_message_id` linked each continuation to its preceding provider response.
Provider `message_start` diagnostics were null; positive usage counts establish the reads.
All three HTTP responses were 200 and retained terminal `message_stop` events. The Session
finished idle, unblocked and at high effort.

Provider request IDs, in turn order:

- `req_011CgZNDvDnWNyc1fWjtJPX5`
- `req_011CgZNE1PbF3JEQPCPa7j61`
- `req_011CgZNE8XDF3W3H23oEZ4LK`

The disposable Sandbox was stopped and deleted after inspection; retained sampler evidence
was preserved. This demonstrates reuse for this ordinary unpadded staging conversation,
including across an effort change. It does not measure a fleet-wide hit rate, long-idle expiry,
changed tool definitions or compaction effects on the cache.

## Credentials and remaining work

Bounded live acceptance used the operator-authorized organization key through per-invocation
redacted secret headers. Temporary credential rc files were removed after the runs.
[Persistent BuildBuddy secret provisioning](https://github.com/agentydragon/ducktape/issues/13063)
remains deferred.

An earlier fixture uploaded the inline organization key in a test artifact. Both affected
invocations were deleted and the fixture now keeps credential files outside uploaded outputs.
Deletion does not establish that cached artifact bytes were purged; rotation of the exposed
key has not been verified in this session.

Background compaction, dynamic tool definitions and optional Langfuse export remain deferred.
None is required by the implemented ordinary compaction contract.
