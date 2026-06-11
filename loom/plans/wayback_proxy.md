# Wayback proxy: date-clamped web access for contestants

Status: **W1–W4 all landed** (direction approved 2026-06-10). W1:
`cluster/k8s/wayback-cache/` (ClusterIP-only — the gateway is public-only and
an open IA relay is undesirable; dev access via `kubectl port-forward`). W2:
per-agent proxy + demo compose + e2e at <../wayback_proxy/>, and the gym's
Inspect harness generates a per-`as_of` sandbox compose (agent on an
internal-only network whose sole peer is the proxy; `WAYBACK_AS_OF` baked as
a literal) with a mockllm e2e fetching a clamped page end to end on RBE. W3:
served-evidence manifest read back into `Score.metadata`. W4: HTTPS MITM (see
below). The proxy is the mini-implementation, not a WaybackProxy fork —
upstream's selection is nearest-capture (can serve post-date content within
`DATE_TOLERANCE`), its in-band settings page can change the date at runtime
from the client side, CONNECT is fake-accepted, and the upstream host is
hardcoded; hardening all that approximated writing the clamped proxy
directly.

A live agent harness (`loom/gym/agent_eval.py`, `//loom/gym:agent_eval_bin`)
runs a real model against the full chain: the model client runs host-side
against the cluster LiteLLM (Anthropic-shaped), the react agent's tools
execute in the Docker sandbox whose only route is the date-clamped proxy, and
`--wayback-upstream` points at the in-cluster authed pull-through cache
(`WAYBACK_UPSTREAM_AUTH` carries the `Bearer` token). See "Live eval findings"
below for the first end-to-end smoke.

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
   newest capture ≤ `as_of` through the Wayback Availability API first, falls
   back to clamped CDX when Availability cannot represent the case cleanly,
   serves the `id_` raw bytes (no IA banner/rewriting), follows IA's 302
   timestamp-canonicalization internally, and returns 404 when no pre-`as_of`
   capture exists. Existing piece:
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
   Availability/CDX metadata is cached with long TTLs, and
   `proxy_cache_lock` collapses concurrent misses); misses forward to a
   loopback-only tier 2 where **every request is by construction a cold
   miss**, so `limit_req` token buckets pace exactly the traffic IA sees while
   cache hits are never throttled. `/wayback/available` routes to
   `archive.org`; replay/CDX routes to `web.archive.org`; the origin class is
   part of the cache key. Contact User-Agent on the upstream hop. Draft
   manifests exist (namespace / pvc / configmap-generated nginx.conf /
   deployment / service / flux-kustomization), parked pending go-ahead; the
   config essence:

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

## HTTPS (resolved: MITM)

Resolved in favor of the MITM CA approach so agents never rewrite URLs. The
proxy runs as an embedded mitmproxy (`loom/wayback_proxy/{server,addon}.py`); a
`WaybackAddon` sets `flow.response` from the clamped archive for every flow, so
both `http://` and `https://` reach the same resolver and the agent never
touches the live web. mitmproxy MITMs TLS with its own CA; the sandbox trusts
it via the standard `SSL_CERT_FILE` / `CURL_CA_BUNDLE` / `REQUESTS_CA_BUNDLE` /
`NODE_EXTRA_CA_CERTS` contract — the same contract the in-cluster
`agents-mitmproxy` already injects and that the scraper's `http_fetch` honors.
IA serves the same snapshot regardless of the original scheme, so clamping is
scheme-agnostic.

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
- **W2** ✅: per-agent proxy image (mini-implementation,
  `loom/wayback_proxy/`) + Inspect compose wiring with per-`as_of` plumbing
  (`loom/gym/inspect_harness.py`); mockllm e2e on RBE fetches a known
  snapshot through the full chain.
- **W3** ✅ (sandbox side): the proxy writes its served-evidence manifest to
  `WAYBACK_MANIFEST_PATH`; the gym scorer reads it back from the proxy
  sandbox per sample into `Score.metadata["served_evidence"]`. Evidence
  leads land as `/data/sources.txt` in the agent container — files the
  agent chooses to read, never prompt content. Remaining: surfacing the
  manifests in uploaded run payloads.
- **W4** ✅: HTTPS MITM — the proxy is an embedded mitmproxy; agents use
  `https://` (and `http://`) URLs unmodified, trusting the proxy CA via
  `SSL_CERT_FILE` & friends (the gym sandbox mounts the CA and drops the
  "rewrite to http" instruction). Remaining (optional): text-extraction
  convenience endpoint for dossier-style consumption.

## Live eval findings (first end-to-end smoke, 2026-06-10)

`agent_eval_bin` was run against the in-cluster authed `wayback-cache`
(`--wayback-upstream https://wayback-cache.allegedly.works`, glm-4.5 via
cluster LiteLLM) on an open-ended, no-starting-URL market. The full chain
works: host-side model client → LiteLLM (through the claude-web egress MITM,
TLS verified via the host CA bundle) → react agent in the Docker sandbox →
embedded mitmproxy → authed cluster cache. The model does genuine open-ended
archive research — for "did New Glenn reach orbit on its first launch?" it
fetched Wikipedia (article + REST summary API), blueorigin.com, SpaceNews,
Spaceflight Now, NASASpaceflight (its 2024-12-30 launch roundup), Ars Technica,
and a Google-cache search, all as `https://` through the clamped proxy with no
URL rewriting, and the served-evidence manifest came back in
`Score.metadata["served_evidence"]` (the W3 read-back works with a live model).
Findings to act on:

- **`connection_strategy` should be `lazy`.** mitmproxy defaults to eager: for
  every flow it opens an upstream TLS connection to the _original_ host before
  our addon runs. That connection egresses through the claude-web TLS-inspecting
  proxy and fails cert verification (`self-signed certificate in certificate
chain`), logging a warning per request. It's harmless — the addon sets
  `flow.response` from the cache so the eager connection is discarded — but
  wasteful and noisy. Set `connection_strategy=lazy` in `server.py` so the
  upstream socket is never opened.
- **CN-hosted models refuse politically sensitive prompts.** glm-4.5 rejects
  the China/Taiwan invasion market at the API (`invalid_request_error` code
  `1301`, "potentially unsafe or sensitive content"). That market is now
  commented out of `market_seed_tasks.py`; the panel is otherwise unaffected,
  but cross-model runs must tolerate per-model task refusals rather than
  aborting the whole eval.
- **Cache returns 5xx under IA pressure.** Live CDX/content lookups returned a
  mix of `403/502/503/504` from `wayback-cache` (IA throttling/erroring the
  cold-miss tier-2 hop), so many of the agent's fetches failed. The clamp and
  manifest logic are correct; this is an operational tuning matter for the
  cache's `limit_req` pacing and IA retry/backoff, not a proxy bug.
- **Submission must tolerate a malformed answer.** glm-4.5 researched the task
  but its final `submit` carried an empty/non-JSON payload, so scoring raised
  `JSONDecodeError` and the sample scored `value=nan` (no probability). A
  well-formed run is the next thing to confirm, but the harness should also
  degrade gracefully — record the parse failure as a scored miss with the
  served-evidence manifest intact (already captured), not lose the whole sample.
