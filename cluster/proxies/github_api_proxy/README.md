# Central GitHub-observation HTTPS proxy

Runtime source only; deployment and secret ownership live under `cluster/k8s/`.
`main_bin --config /run/github-api-proxy/config.json` starts an HTTPS forward proxy
and a separate private HTTP metrics listener. The image entrypoint accepts the same
`--config` argument. Bazel targets: `image`, `load`, `tests`.

```json
{
  "proxy_hostname": "proxy.example.test",
  "credential_files": ["/run/clients/first.json", "/run/clients/second.json"],
  "proxy_tls_cert_file": "/run/outer-tls/tls.crt",
  "proxy_tls_key_file": "/run/outer-tls/tls.key",
  "interception_ca_cert_file": "/run/interception-ca/tls.crt",
  "interception_ca_key_file": "/run/interception-ca/tls.key",
  "confdir": "/var/lib/github-api-proxy/conf",
  "capture_path": "/var/lib/github-api-proxy/raw.flows",
  "session_ws_events": "/var/lib/github-api-proxy/sessions.jsonl"
}
```

Each credential file is a JSON object mapping client IDs to nonempty passwords.
IDs match `[a-z][a-z0-9_-]{0,31}`; duplicates across files are rejected and the
combined set is limited to 64 clients. Generate independent high-entropy passwords.
Credential and certificate changes take effect on restart. Mounted certificate/key
pairs are checked; the interception certificate must be a CA. Private working PEMs
are written under `confdir`; no new interception identity is generated.

Defaults: proxy `0.0.0.0:8080`, private metrics `0.0.0.0:9090`, exact cloud GitHub
batch POST block enabled. Override with `listen_host`, `listen_port`, `metrics_host`,
`metrics_port`, and `block_cloud_github_batch`. `upstream_ca_file` optionally supplies
an explicit upstream trust bundle; otherwise normal system trust applies. Outer
TLS always serves the dedicated proxy certificate, including for unexpected SNI;
only inner destination TLS uses the interception CA. No TLS verification is disabled.

The mitigation answers authenticated `POST` requests to
`https://claude.ai/v1/code/github/batch-branch-status` with HTTP 429 and
`Retry-After: 3600`; caller query parameters do not bypass the match. Other routes
remain unaffected. Set `block_cloud_github_batch=false` centrally to disable it;
host relays have no mitigation policy. Blocking can leave branch/PR status stale
and is containment, not a repair of the upstream poller.

CONNECT and absolute-form HTTP require Basic authentication inside outer TLS.
CONNECT caches only the validated client ID on that client connection; independent
HTTP requests authenticate separately. Missing/wrong/duplicate credentials and
plaintext transport are rejected before any upstream dial. Proxy authorization
headers and legacy `metadata.proxyauth` are removed before forwarding and again at
every raw serialization boundary, including errors and shutdown. Authenticated
`GET http://mitm.it/` is a constant readiness response with no upstream request or
CA content; the ordinary onboarding application is disabled.

Only public web-origin ports 80 and 443 can be dialed. Every DNS answer must be
globally routable and neither a special/transition address nor an address of the
proxy hostname; mixed public/private answer sets fail closed. Resolution failures
also fail closed. The `OriginLoop` resolves and validates a hostname in its public
`create_connection` boundary, selects one approved numeric address, and passes that
address to asyncio's normal socket implementation. The logical hostname remains in
mitmproxy's connection object for pooling and upstream TLS identity. Starting the
proxy without its guarded, correctly configured loop is rejected. Deployment egress
policy adds another boundary; it does not replace these checks. There is no runtime
option for private origins. Synthetic tests alone redirect validated public IPs to
loopback fixtures.

## Why the custom dial boundary exists

This is a small adapter for the pinned mitmproxy 12.2.3 API, not a general-purpose
asyncio policy. The security invariant is about the address of the actual outbound
TCP connect, while mitmproxy's `server_connect` hook receives a logical hostname
and is followed by `asyncio.open_connection(*server.address)`. The hook can reject
the request, but it cannot supply a separate, already-approved dial address. If
the hostname is resolved in the hook and resolved again by asyncio, DNS can return
a different address between validation and the connect. The loop therefore performs
the resolution, validation, and numeric substitution as one explicit operation at
the boundary that owns the dial. The relevant pinned implementation is
[`ProxyServer.open_connection`](https://github.com/mitmproxy/mitmproxy/blob/v12.2.3/mitmproxy/proxy/server.py)
and the pooling comparison is in
[`GetHttpConnection.connection_spec_matches`](https://github.com/mitmproxy/mitmproxy/blob/v12.2.3/mitmproxy/proxy/layers/http/__init__.py).

The alternatives each lose an important property:

- Validating only in `server_connect` leaves the hook-to-dial DNS race.
- Replacing `server.address` with the numeric result changes mitmproxy's logical
  connection-pool key and can prevent reuse of existing hostname connections; it
  also makes the logical host unavailable to TLS setup.
- Passing the validated addresses through a `ContextVar` and checking them from
  `sock_connect` couples correctness to a task-local implementation detail: the
  hook and dial must stay in the same task, and copied or missing context can make
  the check apply to the wrong connection or not apply at all.
- A cluster egress policy is useful defense in depth, but it cannot express this
  per-request hostname-to-resolved-address invariant and cannot preserve the
  proxy's logical connection identity.

The adapter has no ambient per-task state: it receives the hostname, validates the
current answer set, and immediately delegates the selected numeric address to
asyncio. Direct numeric upstream destinations are checked in `server_connect`
because the same event loop also accepts the proxy's inbound client sockets; the
client's loopback connection must not be mistaken for an upstream origin dial.
The production loop is created from the parsed proxy hostname, and `create_master`
rejects a default loop or a loop configured for another hostname. The corresponding
pytest-asyncio loop factory is test plumbing only.

This code can be deleted when mitmproxy exposes separate logical and dial addresses
or a pre-connect hook that receives and can replace the final numeric address while
leaving the logical hostname untouched. Until then, changing either the hook,
`Server.address`, or the loop boundary requires re-establishing all three properties:
validate every answer, dial only an approved numeric result, and preserve pooling
and TLS identity.

Raw flows and incremental session metadata append to private files without rotation
or deletion. `text/event-stream` responses forward headers and chunks immediately:
waiting for EOF would stall long-lived Claude subscriptions. Mitmproxy retains
streamed bodies in memory for the normal terminal-flow capture; this is not an
incremental SSE recorder, and interrupted streams may lack captured body data.
Raw capture remains sensitive application data despite proxy-password
redaction. Session metadata follows the limited schema in
`devinfra/github_api_capture/README.md`. A write failure increments
`github_api_proxy_capture_write_failures_total{channel}`, emits a fixed error message,
and makes `/healthz` and the authenticated readiness probe fail until restart;
failed raw flows are not queued indefinitely in memory. Inspect incomplete capture
data before restarting after a storage failure. This is an observation-loss alarm,
not forced session termination: readiness failure prevents new Service routing,
but existing CONNECT streams can continue with capture gaps. Metrics remain
available and the exact cloud endpoint block remains active.

`/metrics` exposes bounded configured-client/route/status request counters,
authentication outcomes, explicit observed GraphQL cost sums, and cost-observation
coverage. It never labels queries, URLs, headers, credentials, or unknown client names.
Only nonnegative integer `data.rateLimit.cost` contributes; rate-header differences
never do. Missing, invalid, compressed, unavailable, and over-1-MiB cost bodies are
explicit coverage gaps, not zero-cost observations. HTTP 200 and Claude cloud routes
do not prove upstream GitHub success or cost. Keep metrics/health private via the
deployment's network boundary. Request diagnostics are suppressed; retained captures
and bounded metrics are the observation channels.
