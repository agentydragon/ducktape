# Wayback proxy: date-clamped web access for contestants

Status: **in progress** (direction approved 2026-06-10). W1 landed:
`cluster/k8s/wayback-cache/` (ClusterIP-only — the gateway is public-only and
an open IA relay is undesirable; dev access via `kubectl port-forward`). W2
partially landed: per-agent proxy + demo compose + e2e at
<../wayback_proxy/>; Inspect/gym wiring still pending. The proxy is the
mini-implementation, not a WaybackProxy fork — upstream's selection is
nearest-capture (can serve post-date content within `DATE_TOLERANCE`), its
in-band settings page can change the date at runtime from the client side,
CONNECT is fake-accepted, and the upstream host is hardcoded; hardening all
that approximated writing the clamped proxy directly.

## Goal

Contestant agents run in a Docker sandbox with `network_mode: none` — the
as-of discipline is physical, but the agent only sees the dossier we land.
This plan upgrades "no web" to **"the archived web as of the task's
`as_of`"**: open-ended research with the same physical leak-safety. The agent
gets HTTP **only** through a proxy; the proxy answers every URL with the
newest Wayback capture at-or-before `as_of`; the proxy is backed by a shared
pull-through cache so the Internet Archive is never hammered.

## Shape — compose from existing pieces

Three layers: `agent → per-agent proxy → shared cache → web.archive.org`.

1. **Per-agent date-clamping proxy** ("time machine"). A plain HTTP proxy
   configured with one task's `as_of`. For any requested URL it resolves the
   newest capture ≤ `as_of` (CDX `to=`), serves the `id_` raw bytes (no IA
   banner/rewriting), follows IA's 302 timestamp-canonicalization internally,
   and returns 404 when no pre-`as_of` capture exists. Existing piece:
   [WaybackProxy](https://github.com/richardg867/WaybackProxy) is exactly
   this shape (built so retro browsers can browse the web of a configured
   date). Run it as the sidecar with its date pinned per instance and its
   upstream pointed at our cache — or write the ~150-line equivalent if
   patching upstream-override + clamp-hardening into it is awkward; decide at
   build time.
   - Defense in depth: also clamp explicit wayback-style URLs the agent might
     construct — reject `/web/<ts>…/` paths with `ts > as_of`.
2. **Shared pull-through cache** (cluster service `wayback-cache`). Dumb
   two-tier nginx: tier 1 is a PVC-backed `proxy_cache` (snapshot content at
   a fixed 14-digit timestamp is immutable → effectively infinite validity,
   `proxy_cache_lock` collapses concurrent misses); misses forward to a
   loopback-only tier 2 where **every request is by construction a cold
   miss**, so a `limit_req` token bucket there paces exactly the traffic IA
   sees while cache hits are never throttled. Contact User-Agent on the
   upstream hop. Draft manifests exist (namespace / pvc / configmap-generated
   nginx.conf / deployment / service / flux-kustomization), parked pending
   go-ahead; the config essence:

   ```nginx
   proxy_cache_path /cache keys_zone=wayback:64m max_size=18g inactive=3650d;
   limit_req_zone $server_name zone=ia_upstream:1m rate=30r/m;
   server {  # tier 1: cache
       listen 8080;
       location / { proxy_cache wayback; proxy_cache_valid 200 302 3650d;
                    proxy_cache_lock on; proxy_pass http://127.0.0.1:8081; }
   }
   server {  # tier 2: loopback-only; all traffic here is a cache miss
       listen 127.0.0.1:8081;
       server_name wayback-upstream;
       location / { limit_req zone=ia_upstream burst=60;  # delays, then 503
                    proxy_pass https://web.archive.org; … }
   }
   ```

3. **Sandbox wiring (Inspect)**. The task compose gains an internal-only
   network: the agent container has no default route — its only reachable
   peer is the proxy sidecar (`http_proxy`/`HTTP_PROXY` env point at it);
   `as_of` flows into the sidecar via env from the `Sample`. "Per-agent
   spin-up" is the natural compose shape: one sidecar per task container; the
   cluster cache is shared by all agents plus harvest tooling.

## HTTPS (open decision; default = http-only)

CONNECT tunneling would bypass the proxy's rewriting. Default: only plain
HTTP egress to the proxy; agent tooling is told to use `http://` URLs
(`https://` fails fast, agent retries as http; IA serves the same snapshot
regardless of the original scheme). If that proves high-friction, the
alternative is a MITM CA baked into the sandbox image (mitmproxy pattern).

## Reproducibility and pinning

The cache is an accelerator, **not** the evidence record. The per-agent proxy
logs every `(url, capture_ts, sha256, size)` it serves; the harness attaches
that manifest to the run payload uploaded to S3, so a scored run's full
evidence set is pinned and auditable even if IA later removes a snapshot.
(Optional later: copy served bytes into the `loom-gym` bucket keyed by
sha256.)

## Relation to `Task.evidence`

Curated evidence tuples `(archived url, capture date ≤ as_of, title)` stay in
the task prompt as starting points; with the proxy the agent can actually
fetch them — and browse outward from them — rather than only reading titles.

## Phasing

- **W1** ✅: deploy `wayback-cache` (`cluster/k8s/wayback-cache/`).
- **W2** (proxy + demo compose ✅; Inspect wiring pending): per-agent proxy
  image (mini-implementation, `loom/wayback_proxy/`) + Inspect compose wiring
  - `as_of` env plumbing; mockllm e2e test on RBE that fetches a known
    snapshot through the full chain.
- **W3**: served-evidence manifest → run payload → S3.
- **W4** (optional): HTTPS MITM; text-extraction convenience endpoint for
  dossier-style consumption.
