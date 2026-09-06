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
