# Wayback proxy — date-clamped web access for sandboxed agents

The per-agent "time machine" from the [wayback proxy plan](../plans/wayback_proxy.md):
a forward proxy that answers every URL with the newest Internet Archive capture
at-or-before `WAYBACK_AS_OF`, served as raw `id_` bytes. Normal URLs resolve via
the Wayback Availability API first, with CDX as a fallback for cases Availability
does not represent cleanly (for example archived non-200 captures). No capture at
or before the cutoff → 404. Explicit `web.archive.org/web/<ts>/…` requests are
clamped (`ts ≤ as_of`), CDX queries get their `to=` bound clamped, and redirect
hops are re-clamped (IA canonicalizes toward the _closest_ capture, which can
walk forward in time). Every served response is logged as a JSONL evidence line
`{kind: "served", url, capture_ts, sha256, size}` on stdout; an unexpected
upstream failure (IA/cache HTTP ≥400 that isn't an archived error page) is logged
on the same stream as `{kind: "upstream_error", request_url, status, body}` (body
truncated) so a degraded run is diagnosable rather than collapsing into an opaque 502.

It runs as an embedded [mitmproxy](https://mitmproxy.org/) (a
[`WaybackAddon`](addon.py) answers every flow from the archive), so **agents
use their natural `http://` and `https://` URLs** — mitmproxy intercepts the
TLS with its own CA and the request reaches the same clamping resolver either
way. No URL rewriting, no `http://` downgrade. The agent trusts the proxy's CA
through the standard `SSL_CERT_FILE` / `CURL_CA_BUNDLE` / `REQUESTS_CA_BUNDLE` /
`NODE_EXTRA_CA_CERTS` contract; the CA cert is generated on first run at
`<WAYBACK_CONFDIR>/mitmproxy-ca-cert.pem`.

`compose.yaml` is the demo the gym sandbox builds on: the `agent` container
sits on an `internal: true` network with **no internet route** — its only
reachable peer is the proxy sidecar — and the proxy is the only thing that ever
speaks to the (archived) web.

## Demo

```bash
bazelisk run //loom/wayback_proxy:load   # build + docker-load wayback-proxy:latest
cd loom/wayback_proxy
docker compose up -d --wait

# Browse the web as of 2020-06-01 (the default WAYBACK_AS_OF), over https:
docker compose exec agent python -c '
import urllib.request
with urllib.request.urlopen("https://example.com/") as r:
    print(r.status, r.headers["X-Wayback-Timestamp"])
    print(r.read()[:200])
'

# Plain http works identically (IA serves the same snapshot either way):
docker compose exec agent python -c '
import urllib.request
with urllib.request.urlopen("http://example.com/") as r:
    print(r.status, r.headers["X-Wayback-Timestamp"])
'

# Direct egress is physically blocked (no route off the internal network):
docker compose exec agent python -c '
import socket
socket.create_connection(("1.1.1.1", 80), timeout=5)
'

docker compose down -v
```

Knobs (compose env interpolation): `WAYBACK_AS_OF` (ISO date, default
`2020-06-01`), `WAYBACK_UPSTREAM` (replay/CDX upstream, default
`https://web.archive.org`), `WAYBACK_AVAILABILITY_UPSTREAM` (Availability API
upstream; defaults to `https://archive.org` in direct-IA mode and to
`WAYBACK_UPSTREAM` when a shared cache is configured). `WAYBACK_CONFDIR`
(default `~/.mitmproxy`) is where the CA is generated; the demo points it at a
volume shared read-only with the agent.

## Using the shared cluster cache

`cluster/k8s/wayback-cache/` runs a two-tier nginx pull-through cache
(ClusterIP only) so repeated lookups never re-hit IA and the upstream hop is
rate-limited. Point the proxy at it through a port-forward:

```bash
# --address 0.0.0.0: host.docker.internal resolves to the docker bridge
# gateway, so the forward must listen beyond loopback.
kubectl -n wayback-cache port-forward --address 0.0.0.0 svc/wayback-cache 8080:8080 &
WAYBACK_UPSTREAM=http://host.docker.internal:8080 docker compose up -d --wait
```

In-cluster consumers use `http://wayback-cache.wayback-cache.svc.cluster.local:8080`.
For a stable CA across pod restarts, mount a CA into `WAYBACK_CONFDIR` from a
Secret (`mitmproxy-ca.pem` = cert+key) rather than letting each pod generate
its own ephemeral CA.

## Tests

```bash
bbr test //loom/wayback_proxy:test_proxy        # addon semantics vs canned fake IA
bbr test //loom/wayback_proxy:test_compose_e2e  # the compose demo, end to end (Docker)
```

`fake_ia.py` pins the IA contract the proxy relies on (Availability JSON, CDX
header-row JSON, scheme-insensitive urlkey matching, empty CDX body on no
matches, `Memento-Datetime` on replays, 302 timestamp canonicalization, captured
live-web redirects).
`test_compose_e2e.py` proves both http and https fetches resolve to the clamped
capture while direct egress stays physically blocked.
