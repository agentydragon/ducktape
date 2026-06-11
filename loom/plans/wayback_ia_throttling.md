# Wayback cache vs. Internet Archive rate-limiting — findings & plan

Status as of **2026-06-11**. Handoff for whoever next works on making the
`loom/gym` eval's archived web access reliable. Companion to
<wayback_proxy.md> (the proxy design), <archive_org_apis.md> (API notes), and
<../gym/TODO.md>.

## TL;DR

The `loom/gym` eval now runs end-to-end in `claude-sandbox` (it was fully hung;
see "What shipped" below). The remaining quality problem is **archive.org
failing a large fraction of our cold CDX-index queries** — typically ~900-950
HTTP `502`/`504` per 33-task run — which makes agents loop on failed fetches and
burn their message budget, yielding ~8-13 `nan` (no-answer) samples per run.

We added Prometheus metrics and proved that the static mitigations we tried
(bigger keepalive pool, longer CDX TTL, lower concurrency) **shuffle the failure
mode without reducing the failure volume**. The next moves are _not_ more static
tuning: **(1) route the timestamp clamp off the rate-limited CDX endpoint onto
the availability API; (2) capture IA's own rate-limit signal and back off
adaptively.** Details below.

## What shipped this session (all merged to `devel`)

The eval went from hung to working via:

- **docker-ci reachable in-cluster**: eval Job targets
  `docker-ci.docker-ci.svc.cluster.local:2376`; docker-ci mTLS PKI moved to
  cert-manager `cluster-internal-ca` (#2074, #2081).
- **augur-evidence clone**: `finance/evidence/checkout.py` uses a
  `RemoteCallbacks.certificate_check` that accepts the cluster mitmproxy's MITM
  cert (libgit2 ignores `SSL_CERT_FILE`); mTLS client certs mounted `0444` so the
  non-root eval can read them (#2096).
- **litellm reached by in-cluster svc name** `http://litellm.litellm.svc.cluster.local:4000`
  via `--base-url` (the model client `httpx` doesn't trust the MITM cert; the svc
  name is `NO_PROXY`-excluded so it goes direct) (#2099).
- **`--max-samples 8`** caps concurrent sandboxes under docker-ci's ~31-network
  pool (#2104).
- **CDX cache TTL 7d → 365d** (#2108) and **upstream keepalive 8 → 64 + structured
  access log** (#2111) on the wayback-cache.
- **Prometheus metrics** via a `prometheus-nginxlog-exporter` sidecar (#2113).

## The core finding: static tuning doesn't move the needle

Three full 33-task runs (`glm-4.5`, `--task-filter manifold-`):

| run   | keepalive | max-samples | CDX TTL | wall  | mean proper-loss | nan/33 | TCP refusals | IA HTTP-502    |
| ----- | --------- | ----------- | ------- | ----- | ---------------- | ------ | ------------ | -------------- |
| cbv26 | 8         | ~20         | 7d      | 34:50 | 1.113            | 10     | ~874         | (≈all refused) |
| cnmcw | 8         | 8           | 7d      | 19:32 | 0.754            | 8      | ~930         | (≈all refused) |
| wdfzh | 64        | 8           | 365d    | 29:02 | 1.125            | 13     | **0**        | **~951**       |

Reading:

- **keepalive 64 eliminated TCP connection refusals entirely** (`connect()
failed (111: Connection refused)` → `0`). That hypothesis (from #2111) is
  confirmed: IA was refusing _new_ TCP connections per source-IP, and reusing
  pooled connections sidesteps it.
- **But total IA failures are unchanged (~900 → ~951).** IA now accepts the
  reused connection and returns **HTTP `502`** instead of refusing the socket.
  Same wall, different paint.
- **Eval quality did not improve** (1.125 / 13 nan, the worst of the three). The
  per-run loss/nan differences are mostly stochastic noise, not attributable to
  the config knobs.
- Content cache hit rate is healthy (~66%); the `502`s are concentrated almost
  entirely on **cold CDX-index queries** (`/cdx/search/cdx`), which are 3× the
  volume of content fetches and the least cacheable (keyed by `url` + `to=as_of`).

Conclusion: the bottleneck is **IA failing our cold-CDX volume regardless of how
politely we hold the socket**. Levers that can actually move it must _reduce or
reroute that cold volume_, or _react_ to IA's failures — not adjust connection
mechanics.

## What we learned about archive.org's behavior

- **We get `502`/`504`, not `429`.** Those are "IA's backend is overloaded /
  timed out", _not_ a deliberate rate-limit response — so they likely carry **no
  `Retry-After`**. IA's clean rate-limit path (`429`) may not be one we're even
  hitting. (Unconfirmed for the CDX path — see plan item 2.)
- **IA emits a custom `x-rl` rate-limit header.** Observed on
  `archive.org/wayback/available` responses: `x-rl: 0` (0 = not currently
  limited; also `x-na`, `x-app-server`, `x-ts`, `x-tr`). This is IA's own
  signal — non-standard, not `X-RateLimit-*`/`Retry-After`. We do **not** capture
  it on the cache yet.
- **`web.archive.org` hostname-blocks some egress IPs**: from the Anthropic agent
  egress, every `web.archive.org` request returns `403 x-block-reason:
hostname_blocked` (a hard block, distinct from the cluster's
  rate-limited-but-not-blocked `502`/`504`). So you **cannot probe IA's CDX
  rate-limit behavior from an agent shell** — you must observe it from the
  cluster's wayback-cache egress.
- The **availability API** (`archive.org/wayback/available?url=…&timestamp=…`)
  was reachable (`200`) and does the same "nearest capture ≤ timestamp" job the
  CDX clamp does — on a different host (`archive.org`, not the blocked/hammered
  `web.archive.org/cdx`).

## Next plans (prioritized)

### 1. Availability-API clamp — highest leverage

Replace the CDX `/cdx/search/cdx` timestamp clamp in `loom/wayback_proxy/` with
`archive.org/wayback/available?url=<url>&timestamp=<YYYYMMDDhhmmss>`. This
sidesteps the rate-limited, mostly-failing CDX endpoint for the common path (the
dominant source of the `502`s). CDX should remain as a clamped passthrough and
fallback for cases Availability cannot represent cleanly, such as archived
non-200 captures. Implementation lives in `loom/wayback_proxy/proxy.py` /
`addon.py` (the date-clamp logic) + tests (`test_proxy.py`, `fake_ia.py`).

Verify before committing:

- the availability API returns the closest snapshot **≤** `as_of`, and respects
  the `DATE_TOLERANCE` semantics the current clamp relies on (it returns a single
  closest capture, not the full CDX list — confirm that's sufficient for the
  proxy's selection logic);
- the returned snapshot URL is the same `/web/<ts>id_/<url>` form we then fetch
  for content (content fetches still go to `web.archive.org` and still benefit
  from the cache);
- its rate limits / `x-rl` behavior under the eval's volume (it may have its own
  limits — measure, don't assume).

Implementation note: the intended shape is Availability-first for normal URLs,
strict timestamp validation in the proxy, and CDX fallback only when
Availability is unavailable/future or cannot preserve current semantics (for
example archived non-200 captures).

### 2. Capture IA's signal headers on the cache (cheap; do before #3)

Add `$upstream_http_x_rl`, `$upstream_http_retry_after`, `$upstream_http_x_na` to
the wayback-cache nginx access log (a `log_format` addition) and/or the exporter
(`cluster/k8s/wayback-cache/config/nginxlog-exporter.hcl` `relabel`). Roll
**after** an eval finishes (rolling the cache mid-run drops in-flight fetches).
Then one run tells us definitively whether IA sends a retry/limit signal on the
`502`/CDX path we actually fail on. This is the "measure before tuning" step.

Implementation note: the cache log/exporter should split metrics by `origin`
(`archive.org` vs `web.archive.org`) and include the low-cardinality signal
headers so the post-rollout eval can distinguish Availability pressure from
replay/CDX pressure.

### 3. Adaptive backoff / respect-signal (after #2's data)

- **If `x-rl`/`Retry-After` are present**: respect them — pause IA fetches
  accordingly, and propagate a `503 + Retry-After` down to the per-sample
  `wayback_proxy`, which **enforces** the wait before returning to the agent (an
  LLM can't hammer through a proxy that sleeps).
- **For the `502`/`504` overload (no clean signal)**: replace the static
  `limit_req` constants (`30 r/m` / `60 r/m`) with a feedback loop. Stock nginx
  `limit_req` is static; options: an OpenResty/Lua AIMD limiter (slow-increase,
  halve-on-5xx, like TCP congestion control), or front IA with an egress Envoy
  using the `adaptive_concurrency` filter (gradient-based, zero magic constants),
  or a small adaptive egress proxy. Also **stop retrying into the fire**: the
  tier-2 server currently does `proxy_next_upstream ... http_502 http_503
http_504` with `tries=3`, which _amplifies_ load when IA is failing — consider a
  circuit breaker that backs off when the `502` rate spikes.

### 4. Self-hosted shard (biggest, removes the dependency)

`pywb` + `OutbackCDX` over a WARC store, bootstrapped once from IA for the eval's
URL universe → zero IA egress at run time, fully reproducible. Highest effort;
revisit if the eval becomes a recurring reproducibility-critical artifact.
Note: we cannot bulk-mirror IA (no open replication protocol; `web.archive.org`
crawl WARCs aren't bulk-downloadable), so the shard is still bootstrapped by
fetching the specific captures we need. Common Crawl is the only genuinely-open
bulk web corpus but is periodic snapshots, not arbitrary-timestamp replay.

## Metrics & observability runbook

The wayback-cache now exports Prometheus metrics (sidecar
`prometheus-nginxlog-exporter`, scraped by a ServiceMonitor):

- **Endpoint**: `wayback-cache.wayback-cache.svc.cluster.local:4040/metrics`.
  ClusterIP-only; scrape it from a throwaway `claude-sandbox` curl pod
  (`kubectl port-forward` is broken through the kube-api MITM — see gotchas).
- **`wayback_http_response_count_total{cache,status,up}`**:
  - cache hit rate ≈ `{cache="HIT"}` / (`{cache="HIT"}` + `{cache="MISS",status="200"}`)
  - **TCP refusals** = `{status="502", up="-"}` (the #2111 target; now `0`)
  - **IA HTTP errors** = `{status="502", up="502"}` (the current dominant failure)
- **`wayback_http_response_time_seconds`** histogram (cache `HIT` ~4 ms vs `MISS`
  ~8 s — the cache's value, made visible).

### Cache size / object count (SeaweedFS S3-bucket metrics)

The PVC is exposed as a **SeaweedFS S3 bucket** (`/buckets/<pv-name>`), so cache
fullness is in Mimir under `SeaweedFS_s3_bucket_*` (not `kubelet_volume_stats_*`,
which this cluster does **not** collect):

- bucket name = the PV name:
  `kubectl get pvc -n wayback-cache wayback-cache-data-ovh -o jsonpath='{.spec.volumeName}'`
  (currently `pvc-24949750-19f0-43b3-b43b-fe97d7806980`).
- `SeaweedFS_s3_bucket_object_count{bucket="<pv>"}`,
  `SeaweedFS_s3_bucket_size_bytes` (logical),
  `SeaweedFS_s3_bucket_physical_size_bytes` (= 2× logical; replication factor 2).
- As of writing: **~1,052 objects / ~17 MiB logical** — ~0.1% of the 18 GB
  `max_size` cap, so no eviction pressure; the cache warms across runs.

### Querying Mimir

No standalone Prometheus — Alloy scrapes → Mimir. Query via the gateway:

```
POST http://mimir-gateway.monitoring.svc.cluster.local/prometheus/api/v1/query
Header: X-Scope-OrgID: anonymous       # the no_auth_tenant
Body:   query=<promql>   (urlencoded)
```

### Where the cache lives

nginx `proxy_cache` at `/cache` (`levels=1:2`, key `$request_uri`, `max_size=18g`,
content kept ~10y / CDX 1y) on PVC `wayback-cache-data-ovh` (20 Gi,
`storageClassName: seaweedfs-ovh`, RWO) → SeaweedFS volume servers →
`local-path-ovh` disks on the always-on OVH nodes. Survives pod rolls (warming
compounds); resets only if the PVC is deleted. Single RWO replica.

## Operational gotchas (the "unexplained" knowledge)

- **Run the eval**: `kubectl apply -f loom/gym/k8s/eval-job.yaml` into
  `claude-sandbox` — an on-demand Job (not Flux), `backoffLimit: 0` (a run is not
  idempotent, never auto-retried). Needs docker-ci + wayback-cache + litellm up.
  ~20-35 min. Inspect prints the score summary only at the **end**; watch the
  `eval` container log. Delete the prior Job before re-applying.
- **Cluster access from an agent session**: the session kubeconfig token may be
  expired. Rebuild from the SOPS-encrypted rotated JWT
  (`secrets/claude-web-k8s-jwt.yaml`, decrypt with `SOPS_AGE_KEY`) →
  `https://kubeapi.allegedly.works`; see `devinfra/k8s/kubeconfig.py`.
  `BUILDBUDDY_API_KEY` for `bbr` is in `secrets/buildbuddy.yaml`.
- **`kubectl exec` and `port-forward` are BROKEN** through the kube-api MITM
  (SPDY/streaming upgrade dies — bare `Error from server:`). Use `kubectl run`
  throwaway pods + `kubectl logs` for any in-cluster probing (curl to a svc,
  docker API to docker-ci, etc.). This is why all diagnostics here use pods.
- **docker-ci network pool**: ~31 networks max. A _killed_ eval leaves orphaned
  compose containers/networks on the shared DinD (k8s deleting the eval pod does
  not clean up the daemon), which can exhaust the pool → `all predefined address
pools have been fully subnetted`. `--max-samples 8` keeps one clean run under
  the cap; if exhausted, clean via the docker API (`docker rm -f` the `inspect-*`
  containers + `docker network prune`), since you can't restart the deployment
  (read-only RBAC in `docker-ci`).
- **Eval failure signature**: agents that drown in IA `502`s exhaust their
  80-message budget without emitting valid JSON → `value=nan`,
  `submission_error=JSONDecodeError`. The `nan` count ≈ how badly IA failed that
  run, _not_ a model/scaffold bug.
- **Flux source lag**: wayback-cache config changes merge to `devel` but the Flux
  `GitRepository` source can lag minutes; you can't force the source (RBAC), only
  `kubectl annotate kustomization … reconcile.fluxcd.io/requestedAt=…` to nudge
  the kustomization once the source has the commit. `reloader.stakater.com/auto` +
  hash-suffixed `configMapGenerator` auto-roll the pod on config change.
