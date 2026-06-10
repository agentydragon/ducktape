# Wayback proxy — date-clamped web access for sandboxed agents

The per-agent "time machine" from the [wayback proxy plan](../plans/wayback_proxy.md):
a plain-HTTP forward proxy that answers every URL with the newest Internet
Archive capture at-or-before `WAYBACK_AS_OF`, served as raw `id_` bytes. No
capture at or before the cutoff → 404. Explicit `web.archive.org/web/<ts>/…`
requests are clamped (`ts ≤ as_of`), CDX queries get their `to=` bound
clamped, redirect hops are re-clamped (IA canonicalizes toward the _closest_
capture, which can walk forward in time), and CONNECT is refused — agents use
`http://` URLs (IA serves the same snapshot regardless of the original
scheme). Every served response is logged as a JSONL evidence line
`{url, capture_ts, sha256, size}` on stdout.

`compose.yaml` is the demo the gym sandbox builds on: the `agent` container
sits on an `internal: true` network with **no internet route** — its only
reachable peer is the proxy sidecar.

## Demo

```bash
bazelisk run //loom/wayback_proxy:load   # build + docker-load wayback-proxy:latest
cd loom/wayback_proxy
docker compose up -d --wait

# Browse the web as of 2020-06-01 (the default WAYBACK_AS_OF):
docker compose exec agent python -c '
import urllib.request
with urllib.request.urlopen("http://example.com/") as r:
    print(r.status, r.headers["X-Wayback-Timestamp"])
    print(r.read()[:200])
'

# https fails fast by design (501 from the proxy; retry as http://):
docker compose exec agent python -c '
import urllib.request
urllib.request.urlopen("https://example.com/")
'

# Direct egress is physically blocked (no route off the internal network):
docker compose exec agent python -c '
import socket
socket.create_connection(("1.1.1.1", 80), timeout=5)
'

docker compose down -v
```

Knobs (compose env interpolation): `WAYBACK_AS_OF` (ISO date, default
`2020-06-01`), `WAYBACK_UPSTREAM` (default `https://web.archive.org`).

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

## Tests

```bash
bbr test //loom/wayback_proxy:test_proxy        # proxy semantics vs canned fake IA
bbr test //loom/wayback_proxy:test_compose_e2e  # the compose demo, end to end (Docker)
```

`fake_ia.py` pins the IA contract the proxy relies on (CDX header-row JSON,
empty body on no matches, `Memento-Datetime` on replays, 302 timestamp
canonicalization, captured live-web redirects).
