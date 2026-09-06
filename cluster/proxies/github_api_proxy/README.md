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
also fail closed. The checked numeric address is pinned before socket creation to
prevent DNS rebinding; this deliberately uses the first answer rather than retrying
alternate addresses. The upstream TLS identity is preserved. Deployment egress
policy adds another boundary; it does not replace these checks. There is no runtime
option for private origins. Synthetic tests alone redirect validated public IPs to
loopback fixtures.

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
