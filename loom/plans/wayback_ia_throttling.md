# Wayback cache vs. Internet Archive rate-limiting — findings & plan

Status as of **2026-06-12**. Handoff for whoever next works on making the
`loom/gym` eval's archived web access reliable. Companion to
<../wayback_proxy/README.md> (the proxy implementation),
<../docs/archive_org_apis.md> (API notes), and <../gym/TODO.md>.

## TL;DR

The `loom/gym` eval now runs end-to-end in `claude-sandbox` (it was fully hung;
see "What shipped" below). The original nginx/PVC cache failure was
**archive.org failing a large fraction of cold CDX-index queries** — typically
~900-950 HTTP `502`/`504` per 33-task run — which made agents loop on failed
fetches and burn their message budget, yielding ~8-13 `nan` (no-answer) samples
per run.

The Rust write-through archive replacement is now merged/deployed and changed
the failure shape, but did **not** improve the cold eval outcome yet. The
2026-06-12 run completed in 2:22:15 with 15/33 submissions, 18/33 no-answer
samples, mean proper-loss 1.218 over submitted samples, and 860 upstream-error
records in the driver summary (`855x 503`, `5x 403`). The old direct IA
TCP-refusal / `502` mode is gone; the visible failure is now cache-side `503`
backpressure/acquisition miss delay. The next PR fixes the stuck/leaked CDX
limiter path, adds queue-timeout/upstream-failure visibility, and verifies proxy
`503 + Retry-After` retry behavior. After it deploys, rerun with the merged
1000-turn + Inspect compaction config.

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

## Rust archive cold-run result (2026-06-12)

After replacing the nginx/PVC cache with `loom/wayback_archive` and deploying it
under the existing `wayback-cache` service, the first full eval was:

- model/config: `glm-4.5`, `--task-filter manifold-`, `--max-samples 8`,
  `message_limit=80` (the run predated the merged 1000-turn + compaction config);
- job: `claude-sandbox/loom-gym-eval`, completed successfully;
- wall time: 2:22:15;
- submitted answers: 15/33;
- no-answer / `nan`: 18/33;
- mean proper-loss over submitted samples: 1.218;
- total model usage: 15,044,226 tokens
  (`1,163,969` input, `13,505,106` cache read, `375,151` output);
- served fetch mentions in the driver summary: 1,084;
- upstream-error records in the driver summary: `855x 503`, `5x 403`.

Worst upstream-error samples:

| sample                                | result | upstream errors     |
| ------------------------------------- | ------ | ------------------- |
| `scotus-upholds-trump-tariffs`        | nan    | `65x 503`           |
| `labor-wins-2025-australian-election` | nan    | `53x 503`, `1x 403` |
| `trump-2025-nobel-peace-prize`        | scored | `46x 503`           |
| `russia-ukraine-ceasefire-aug-2025`   | scored | `43x 503`, `1x 403` |
| `verstappen-2024-f1-title`            | nan    | `36x 503`, `1x 403` |
| `afd-beats-spd-2025`                  | nan    | `35x 503`           |
| `uk-wealth-tax-2025`                  | scored | `35x 503`           |
| `capital-one-discover-merger`         | nan    | `34x 503`           |
| `mangione-murder-conviction-2025`     | nan    | `33x 503`           |
| `us-govt-shutdown-2025`               | nan    | `32x 503`           |

Interpretation:

- This is worse than the prior nginx/PVC runs on wall time and no-answer rate.
  Do not expect cache warmth alone to fix this: agents branch to new URLs, so a
  second run only partially reuses the first run's fills.
- The no-answer samples are still non-submissions after exhausting the agent
  loop, not evaluator crashes. Partial structured logs showed every no-answer
  sample available at that point had exactly 80 messages and an empty answer
  (`JSONDecodeError`); final logs show the same empty-answer signature for the
  additional no-answer samples.
- The failure mode moved from direct IA connection refusals / IA `502` to
  archive-service `503` backpressure while acquisitions are slow or backing off.
  That is the right _shape_ operationally, but it still consumes too much agent
  budget.
- A post-merge metrics scrape showed no active limiter backoff, with limits
  `availability=16`, `cdx=1`, `replay=8` and one in-flight CDX/replay request at
  scrape time. CDX being pinned at `1` during/after the run is consistent with
  the service protecting IA but serializing enough cold misses to hurt eval
  throughput. Follow-up live probes showed the sharper issue: with
  `limit=1` and `in_flight=1`, fresh CDX misses wait the 60s queue budget and
  return `503 + Retry-After` without ever proving the URL has no capture.

Follow-up diagnosis on the highest-error URLs:

- The failures were mostly **not missing captures**. For
  `scotus-upholds-trump-tariffs`, the eval only fetched six unique sites
  (`reuters`, `apnews`, `bbc`, `wsj`, `abajournal`, `foxnews`) but recorded
  `65x 503`. The archive DB has Availability `200` rows and replay `200` rows
  at or before `2026-01-10T23:59:59` for all six.
- A live probe for the exact SCOTUS CDX query
  `url=http://www.reuters.com/&to=20260110235959&output=json&limit=-1`
  returned our `503` after about 60s with body
  `archive acquisition is backing off`, while the corresponding Availability
  query returned `200` in about 0.3s with capture `20260110235953`.
- Partial structured samples show the same pattern. `afd-beats-spd-2025`
  served 50 captures; its 35 upstream errors were our backing-off `503`s
  (`31` CDX, `4` replay). `verstappen-2024-f1-title` had 36 backing-off CDX
  `503`s plus one real CDX `403` for an IA-auth-gated query.
- The durable fix should come before another comparison run: limiter permits are
  now cancellation-safe/RAII so a dropped or wedged acquisition cannot leak
  `in_flight`, and archive acquisition failures now emit counters/logs split by
  endpoint, reason, and upstream status (`single_flight_queue_timeout`,
  `limiter_queue_timeout`, `upstream_retry_after`,
  `upstream_transient_status`, `upstream_fetch_timeout`,
  `upstream_fetch_error`). The 1000-turn + compaction config is still useful,
  but it should not mask a stuck CDX limiter.

Next comparison should use the merged eval config from #2141:
`--message-limit 1000` plus `--compaction-threshold-tokens 115000` for GLM-4.5.
Run it after deploying the limiter/telemetry PR so we compare against a healthy
online-fill service rather than a CDX queue that has stopped admitting new work.
Track: submissions, wall time, served fetches, archive `503` by reason/status,
IA transient classifications, limiter limits/in-flight, queue wait expirations,
and second-run hit rate as a secondary signal.

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
