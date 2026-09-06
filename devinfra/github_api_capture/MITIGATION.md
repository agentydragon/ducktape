# Temporary cloud GitHub polling mitigation

`ducktape.githubApiProxy.blockCloudGithubBatch` is an explicit, default-off host
option. It is enabled on wyrm2 and rugged. Only proxied `POST` requests to
`https://claude.ai/v1/code/github/batch-branch-status` are answered locally with
HTTP 429 and `Retry-After: 3600`; caller query parameters do not bypass the match.
Other routes and hosts pass through. Branch/PR status UI updates can therefore
be unavailable or stale. This is containment, not a repair of the upstream client.

The proxy appends private metadata to
`~/.local/state/github-api-proxy/cloud-github-block-events.jsonl`. Startup,
shutdown, 30-second heartbeats, and each blocked request record the process start
timestamp and its cumulative blocked count. Counts reset for each process start;
the append-only log preserves prior lifetimes. No credentials, request identifiers,
query parameters, or payloads are logged. Service `UMask=0077` protects new files.
Missing heartbeats are a capture/mitigation coverage gap, not evidence of inactivity.

Evaluate containment over seven days of ordinary use with no GraphQL exhaustion,
checking account quota/reset history alongside these heartbeats and blocked counts.
Local metadata is not automatically a centrally scraped metric: collection and
alerts must also be verified. Unproxied browser/mobile/cloud callers are outside
this control, as are hosts where configuration has not been activated.

To disable, set `blockCloudGithubBatch = false` and activate that host's reviewed
configuration. Do not delete the raw flow files or metadata. Any temporary generated
service override must also be removed after verifying its ownership, then reload
the user service manager and restart the proxy with append-safe capture. Merely
closing Desktop's window may leave its background process running.
