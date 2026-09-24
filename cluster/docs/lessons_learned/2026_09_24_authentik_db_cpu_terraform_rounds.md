# Authentik DB CPU during tofu-controller rounds (2026-09-24)

**Status**: The perpetual Terraform drift is fixed (#7855, verified). The DB still reaches
2–3.6 cores for each round's refresh reads, and rounds follow every `devel` commit. No
query-level data yet (§ Not observed).

## Symptom

Approving agentplane actions felt slow. Each approval exchanges the operator's login token
at Authentik (JWT-bearer grant), uncached. Earlier the same day the Authentik server hit its
500m CPU limit during Terraform rounds, and some exchanges hit the app's 10 s timeout;
#7774 lifted the limit. This note covers what remained: the DB primary spiking during the
same rounds.

## Setup

- `authentik-db-ovh`: CNPG, PostgreSQL 18.1, 2 instances. Primary `authentik-db-ovh-4`
  runs on `ovh-ns104952` (8 CPUs; also a control plane, shared with
  `tofu-state-db-ovh-4` and seven other CNPG instances). The Cluster sets no `resources`,
  so the pod is `BestEffort`. `shared_preload_libraries` is empty: no
  `pg_stat_statements`.
- Authentik 2026.8.2 runs one server and one worker, and no Redis
  (`authentik.redis.host: ""`). Its task queue (`authentik.tasks.middleware`) therefore
  runs in Postgres.
- The Terraform objects `sso-providers`, `agent-machine-access`, `gatus-sso` and
  `alloy-otlp-bearer-token` use `interval: 15m`, and their source is the `ducktape`
  GitRepository, which polls `devel` every 1m.

## What a round is

tofu-controller starts a run on two triggers:

- **Interval (15m):** `detectDrift` plan. If it finds drift: plan, then apply.
- **New source revision:** plan and apply for all four objects in the same second, with
  no drift step. Every new `devel` revision of the GitRepository artifact triggers one.
  Between 15:39 and 17:27Z, 17 rounds started all four objects within 10 s, over a span
  that had room for 7 interval rounds. Each source-revision round also re-aligns the four
  interval timers. Before 15:39 the objects started up to 2 min apart; after it, within
  10 s.

Until #7855, `sso-providers`, `agent-machine-access` and `gatus-sso` found changes on
every run and applied them. Each round PUT all 22 OAuth2 providers that have
`allowed_redirect_uris`, plus the `allegedly.works` brand. For `agent-machine-access` and
`gatus-sso` this started with the move to provider 2026.8.0 (#6580, 2026-09-14 06:00Z).
`sso-providers`' `Plan` condition had last transitioned on 2026-04-24. The cause was
server-defaulted attributes (`redirect_uri_type`, `branding_logo`, `branding_favicon`) that
the provider diffs against omitted config. The redirect-URI rule is in <../sso.md>.

Terraform requests per four-module round (Authentik server log):

| Round                         | Requests | PUTs | Duration |
| ----------------------------- | -------: | ---: | -------: |
| Source revision, before #7855 |      234 |   23 |  21–27 s |
| Interval, before #7855        |  399–468 |   23 |  34–55 s |
| Any round, after #7855        |      185 |    0 |  20–38 s |

A PUT emits a `model_updated` Event, and every `view_key`/`view_private_key` read emits a
`secret_view` Event (7–26 per round). Each Event enqueues one `event_trigger_dispatch` and
four `event_trigger_handler` tasks. The worker ran up to 74 dispatches and 294 handlers
in one minute.

## CPU

Measured with kubelet `/metrics/resource` counters. Each value is the mean over one
counter interval of 10–20 s.

| Container                 | Quiet     | Round, before #7855 | Round, after #7855 |
| ------------------------- | --------- | ------------------- | ------------------ |
| DB primary `postgres`     | 0.02–0.05 | 2.0–3.0             | 2.0–3.6            |
| `authentik-server`        | 0.02–0.07 | 0.8–1.1             | 0.6–1.2            |
| `authentik-worker`        | 0.04–0.13 | 0.6–0.8             | 0.25–0.31          |
| node `ovh-ns104952` (all) | 1.4–1.6   | 5.4–5.6             | 4.5–5.1            |

Over 17:14–17:28Z (seven rounds, mostly before #7855, about 1,290 Terraform requests),
the DB primary used 390 CPU-seconds, the server 184 and the worker 142. That is about 0.28
DB CPU-seconds per Terraform request. After subtracting each container's quiet baseline,
it is 2–2.5× what the server spends issuing the same requests.

By endpoint, `GET /api/v3/core/applications/<slug>/` accounts for the most server-side
time: 169 requests, a mean of 1.7 s and a minimum of 0.78 s. Next is
`GET /api/v3/flows/instances/` at a mean of 1.0 s. Other reads average 0.2–0.4 s.
Authentik 2026.8.2's `ApplicationViewSet` queries through
`ApplicationQuerySet.with_provider()`. That adds a `select_related` for every `Provider`
subclass, plus each subclass's `application` and `backchannel_application`, and a
`prefetch_related` of each subclass's `property_mappings`. Whether that query's planning
or execution is what the DB spends its CPU on has not been measured.

## Latency

Server-side `runtime` from the Authentik server log:

- **Terraform requests in rounds:** p50 0.16–0.40 s, p90 1.0–2.1 s, max 4.1 s.
- **Probe:** `GET /application/o/agentplane/.well-known/openid-configuration` once per
  second (a DB-backed read), 17:14–17:30Z.
  - In rounds: p50 75 ms, p90 237 ms, p99 648 ms, max 820 ms (n=162).
  - Outside rounds: p50 61 ms, p90 91 ms, p99 169 ms, max 388 ms (n=633).
  - After #7855 (17:37–18:01Z), in rounds: p50 68 ms, p90 278 ms, p99 617 ms, max
    798 ms (n=219).
  - After #7855, outside rounds: p50 62 ms, p90 93 ms, p99 170 ms, max 315 ms (n=679).
- **Agentplane token exchanges** (`POST /application/o/token/`, `python-httpx2`),
  14:14–17:30Z: 23 requests, none during a round. p50 154 ms, p90 410 ms, max 809 ms.

No approval landed inside a round, so approval latency during a round is still
unobserved. During rounds the probe's p90 is 2.6–3× higher and its p99 3.6–3.8× higher,
but it stays under 1 s. #7855 did not change that. Rounds are frequent: 11 ran in
17:37–18:01Z, covering about a fifth of the wall time.

## Cause, as far as the evidence reaches

- **Volume.** Every `devel` commit triggers a refresh of all four modules at once, on top
  of the 15m interval. Until #7855, the perpetual drift gave every interval run a second
  plan pass and an apply, and added 23 writes, their Events and about 115 tasks per
  round.
- **Cost per request.** Terraform's reads cost the DB 2–2.5× the CPU they cost Django.
  The application retrieve is the slowest endpoint. The query-level cause is not
  measured.

## Options

- **#7855 (merged): stop the perpetual drift.** Verified from 17:26Z: every run logs
  `found drift: false`, with no PUTs, no `model_updated` Events and no second pass. A
  round is now one refresh (185 requests). The DB still peaks at 2–3.6 cores per round.
- **Measure the query-level cost.**
  - Tools:
    - `log_min_duration_statement`: reload only, readable with `kubectl logs`.
    - `pg_stat_statements` with `track_planning`: CNPG adds the preload, which means a
      restart/switchover.
    - `pods/exec` for `psql`.
  - Effect: names the queries, and settles whether the application retrieve's plan or its
    execution is the cost.
  - Cost: a GitOps change to the Cluster.
- **Stop `devel` commits outside `tf/gitops/` from re-running the modules.**
  - Effect: removes most rounds (11 ran in 17:37–18:01Z).
  - Cost: a larger change. tofu-controller v0.16.5's `sourceRef` takes only a
    `GitRepository`, `Bucket` or `OCIRepository`. It would need one whose revision changes
    only when `tf/gitops/` does, e.g. an `OCIRepository` pushed on change.
- **Longer interval for these four objects** (e.g. `1h`).
  - Effect: 4× fewer interval rounds; source-revision rounds are unchanged.
  - Cost: out-of-band Authentik edits are reverted within 1 h instead of 15 min.
- **Stagger the four objects.** Would lower peak concurrency, but config cannot hold it:
  every source-revision round re-aligns them.
- **Give the DB `resources.requests.cpu`** (it is `BestEffort`).
  - Effect: a fair CPU share when `ovh-ns104952` is contended. It peaked at 5.6 of 8
    cores.
  - Cost: a scheduler reservation, and it does not reduce DB CPU.
- **Cache the agentplane token exchange.** Approval latency would become independent of
  Authentik load. It is a design change: the exchange is uncached on purpose.

## Not observed

- **Per-query DB cost.** `pg_stat_statements` is not loaded, and `pods/exec` in
  `authentik` is denied to the agent identity (a grant request was left pending).
- **Mimir history.** `services/proxy` in `monitoring` is denied, so all CPU figures come
  from live kubelet samples taken 16:49–17:52Z.
- **Plan diffs.** `storeReadablePlan` was `none`, so the drift fix was verified from its
  effect, not from the diff. #7872 stores readable plans for the three objects.
- **An approval during a round.**
- **Incidental:** the CNPG exporter's default queries fail on this cluster every scrape
  (`database "app" does not exist`, about 400 per hour), so the CNPG `PodMonitor` series
  that need them are missing.
