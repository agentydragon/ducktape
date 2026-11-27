# CDviz: stored GitHub CI webhooks

The cluster stores Ducktape's GitHub `workflow_run`, `workflow_job` and `pull_request`
webhooks in TimescaleDB, from 2026-11-06 with 90-day retention. Component docs:
[`cluster/cdk8s/cdviz/README.md`](https://github.com/agentydragon/ducktape/blob/devel/cluster/cdk8s/cdviz/README.md).
Use it before paging the Actions API: one query covers a week of exact-head clocks
without the 1,000-result cap or rate limits.

## Access

Read-only queries with your own kubeconfig against the CNPG primary. Long scans
on the replica fail with `canceling statement due to conflict with recovery`.

```bash
PRIMARY=$(kubectl -n cdviz get cluster.postgresql.cnpg.io db -o jsonpath='{.status.currentPrimary}')
q() { kubectl -n cdviz exec "$PRIMARY" -c postgres -- psql -d cdviz -Atc "$1"; }
csv() { kubectl -n cdviz exec "$PRIMARY" -c postgres -- psql -d cdviz -c "\copy ($1) to stdout with csv header"; }
```

## Data model

`cdviz.cdevents_lake` holds one CDEvent per transition:

| Column / path                      | Meaning                                                                             |
| ---------------------------------- | ----------------------------------------------------------------------------------- |
| `timestamp`                        | The transition clock GitHub reported (e.g. `workflow_run.updated_at`), not receipt. |
| `imported_at`                      | Database insert time ≈ webhook receipt (collector batches ≤1 s).                    |
| `predicate`                        | `queued` / `started` / `finished` / `created` / `updated`.                          |
| `payload #> '{customData,github}'` | The webhook body's `action`, `sender`, `repository` and object.                     |

The stored `workflow_job` keeps `head_sha`, `run_id`, `run_attempt`, `conclusion`,
`created_at`/`started_at`/`completed_at`, `runner_name`, `labels` and `steps`.
`workflow_run` keeps `event`, `head_sha` and `pull_requests`, which GitHub leaves
empty for fork heads. Views `cdviz.pipelinerun`/`cdviz.taskrun` normalize runs/jobs.
The webhook does not subscribe to `check_run`, `check_suite` or `status` events, so
the live BuildBuddy commit status and its early-red clock are absent.

## Recipes

### Per-head feedback

`cdviz.ci_feedback(p_from, p_to)` returns one row per exact PR head occurrence:
arrival (`PR head event` = GitHub's `opened`/`synchronize` clock, else `Workflow
creation proxy`), first red/green, `state` and `missing_gates`. It reads events
only up to `p_to`, so a cut at the window end censors late verdicts. Query
per day with trailing events and keep that day's arrivals:

```bash
for d in 20 21 22; do
  csv "select * from cdviz.ci_feedback('2026-11-${d}T00:00Z', '2026-11-$((d+1))T06:00Z')
       where arrived_at >= '2026-11-${d}T00:00Z' and arrived_at < '2026-11-$((d+1))T00:00Z'"
done
```

**Fork heads:** a fork PR's skipped `CI` `pull_request` run outranked the trusted-fork
result until migration `003_ci_feedback_absent_gates.sql` (#20523, live 2026-11-27).
The view recomputes from raw events, so older windows are corrected too. To classify
forks, use `workflow_run.head_repository`: every PR triggers the trusted-fork workflow,
so its presence does not identify a fork.

### Webhook delivery lag

Job completion to receipt in the cluster, per required gate:

```sql
with j as (
  select payload #>> '{customData,github,workflow_job,name}' n,
         (payload #>> '{customData,github,workflow_job,completed_at}')::timestamptz c, imported_at i
  from cdviz.cdevents_lake
  where timestamp >= '2026-11-20' and timestamp < '2026-11-27' and predicate = 'finished'
    and payload #>> '{customData,github,workflow_job,name}'
        in ('bazel-ci / Test & Build', 'Build artifacts + imports check', 'Pre-commit checks'))
select n, count(*), percentile_disc(array[0.5, 0.9, 0.99]) within group (order by extract(epoch from i - c))
from j group by n;
```

20–26 Nov 2026: p50/p90/p99 ≈ 2.9/5.7/7.4 s across ~19,700 completions (one 2,178 s
outlier). This is receipt by our collector, not an agent's inbox or read clock.

### Failed gates that later passed on the same head

A reliability lead, not a flake verdict: confirm the tested inputs and the
failing target before counting it.

```sql
with j as (
  select payload #>> '{customData,github,workflow_job,head_sha}' sha,
         payload #>> '{customData,github,workflow_job,name}' job,
         payload #>> '{customData,github,workflow_job,conclusion}' conclusion,
         (payload #>> '{customData,github,workflow_job,completed_at}')::timestamptz completed_at
  from cdviz.cdevents_lake
  where timestamp >= '2026-11-20' and timestamp < '2026-11-27' and predicate = 'finished'
    and payload #>> '{customData,github,workflow_job,name}'
        in ('bazel-ci / Test & Build', 'Build artifacts + imports check', 'Pre-commit checks'))
select f.job, count(distinct f.sha) failed_heads,
       count(distinct f.sha) filter (where exists (
         select 1 from j s where s.sha = f.sha and s.job = f.job
           and s.conclusion = 'success' and s.completed_at > f.completed_at)) later_green_heads
from j f where f.conclusion = 'failure' group by f.job;
```

Add `run_id`/`run_attempt` to separate whole-job reruns from a new push of the same tree.

## Limits

- `pull_request.updated_at` stands in for a `synchronize` push; it is not the push time.
- Missed job webhooks are not repaired: the 15-minute REST poll restores run summaries only.
- A successful webhook response is not durable storage; a hard collector failure can drop events.
- Job completion clocks have one-second resolution.
