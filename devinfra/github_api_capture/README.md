# GitHub request metadata

`github-api-proxy-report` reads saved mitmproxy flows without replaying requests.
The addon can also be loaded into a live mitmproxy process. It emits JSONL only
for `api.github.com`, retaining timestamps, method, path without query parameters,
user agent, exact query SHA-256, status, GitHub request ID, GraphQL error type/code,
explicit nominal GraphQL cost, and account rate-limit headers. Request duration
can be derived from `started_at` and `completed_at` when completion is available.

It omits auth headers, variables, query text, response payloads, error messages,
and unrelated destinations. User agent is a client hint, not process identity.
Paths can still identify private repositories; review metadata before publication.
Transport failures remain visible without their potentially sensitive error text.

`nominal_graphql_cost: null` means unknown, including responses that do not select
`rateLimit.cost`. It is never zero-filled or inferred from `x-ratelimit-used`.
That header describes the shared account bucket: concurrent callers, reset
boundaries, and [timeout penalties](https://github.blog/changelog/2025-07-21-including-timeouts-in-primary-rate-limits/)
prevent interpreting its difference as this request's cost. An accounting residual
alone cannot prove bypass traffic. Error metadata is retained even on HTTP 200.

The source `github.flows` remains a raw capture containing sensitive unrelated
application traffic. This metadata report does not make the source safe to publish.

## Cloud-mediated GitHub calls

The report also includes three exact `claude.ai` routes: GitHub batch branch status,
compare refs, and installation status. These records include the endpoint, bounded
`caller` tag, exact request-body fingerprint, batch cardinalities, completion/status,
and transport-failure indicator. They exclude session/repository/branch values,
auth headers, other query parameters, and response payloads. Missing cardinalities
remain unknown; an explicitly empty list has cardinality zero.

These are calls to Claude's backend, not observations of its upstream GitHub
requests. The reporter cannot assign GitHub GraphQL cost or query fingerprints
to them. HTTP 200 likewise does not prove every upstream GitHub operation succeeded.

## Optional incremental session WebSocket metadata

`session_ws_metadata.py` is a **default-off, unwired addon**. It is not enabled by
the capture service or report command. Explicit startup opt-in requires both
`record_cloud_session_ws=true` and `cloud_session_ws_events=<private JSONL path>`
when loading this script with mitmproxy. Changing these options requires restart;
this is not an instruction to restart an existing capture.

It observes only `claude.ai` GET `/v1/sessions/ws/:session/subscribe`, discarding the
session path segment and query. It appends start/message/end records immediately,
plus a 30-second heartbeat with active-flow and cumulative message/parse/write-failure
counts. Each process lifetime has its own start timestamp; each observed connection
gets a locally generated ID. Existing JSONL is appended, not truncated; files are
restricted to 0600 and newly created immediate parent directories to 0700.

Records contain direction, message timestamp, byte length, text/binary kind, and
fixed structural counters. They never contain payloads, commands, tool outputs,
headers, original identifiers, arbitrary keys, or unknown type/tool names. Binary,
over-64-KiB text, invalid JSON, analysis-limit, and unknown-schema messages have an
explicit status and unknown (`null`) structural counts. Recognized envelopes with
unrecognized content blocks count those blocks separately. Write failures leave
traffic untouched, emit only a generic errno log, and appear in cumulative counts
if a later append succeeds. Persistent output failure requires checking the proxy
log and missing heartbeats; there is no independent delivery channel.

These are observed message shapes, not unique or executed tool calls. Streamed
tool starts and full assistant messages can describe the same call; the addon does
not retain IDs to deduplicate, reassemble input deltas, or inspect commands. It
cannot identify which tools issued GitHub requests or assign GraphQL cost. Mitmproxy
reassembles fragmented WebSocket frames before this hook; control frames are not
message observations. Connections already open before addon startup are not covered.

The loopback integration test holds one real WebSocket open across multiple text
and binary exchanges and reads its metadata before close. It also flushes mitmproxy's
Save stream and verifies the open flow is absent, then verifies the completed raw
flow after close/shutdown. Appending closes the metadata file each time for immediate
reader visibility; this is not an fsync/power-loss durability guarantee. Neither
this addon nor its tests activate live capture, modify messages, or contact an account.
