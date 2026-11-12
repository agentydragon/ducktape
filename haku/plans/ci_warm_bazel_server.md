# Haku CI: cross-job Bazel caching

**Status: deferred design.** The current haku-ci runner creates an ephemeral, single-job Pod;
its rootless dind sidecar and `emptyDir` cache end with that Pod. KEDA can run multiple jobs
concurrently. The current runner topology and security boundary are documented in
<../../cluster/cdk8s/haku_ci/README.md> (§ Queue autoscaling and Bazel cache locality).
`haku-state` source must remain in-cluster.

A previous design proposed creating a long-lived builder container inside a runner Pod's dind.
That cannot keep a Bazel server warm across jobs with the current one-job-per-Pod lifecycle.
Do not deploy it without choosing an independently managed lifetime and revisiting its isolation.

## When to investigate

Measure current queue, startup, loading/analysis and execution times for representative Haku jobs.
Investigate cross-job caching if cold analysis or repeated execution causes material delay at the
observed job volume. Do not use an old per-run estimate as the trigger. Investigate isolation
independently if the runner ever accepts work from a different author or trust domain.

## Options to compare

- **Shared content-addressed cache:** retain ephemeral runners, but give them a concurrent-safe
  shared `--disk_cache` or an in-cluster `bazel-remote` with eviction. This can reuse action
  results, but does not keep the Bazel server's analysis graph in RAM. Size and place the cache
  for concurrent jobs, node movement and bounded storage.
- **Persistent builder:** host a separate in-cluster builder and workspace with a warm Bazel server,
  rather than tying it to a single-use runner Pod. Define job admission, workspace preparation,
  concurrency, cleanup, resource limits, image updates, failure recovery and lifecycle ownership.
  A shared Bazel output base cannot serve concurrent invocations without serialization or separate
  output bases; measure the resulting queue cost.
- **Stay ephemeral:** retain the within-job cache if measurement does not justify a shared service
  or its operational and security costs.

The persistent-builder option crosses the current per-job isolation boundary. Haku-authored builds
can be adversarial, so cleaning a checkout is not a complete defense against state or cache
poisoning. Define which jobs and credentials the builder can reach, how untrusted output enters
subsequent builds, and whether periodic replacement and cache eviction give an acceptable boundary.
Keep registry and Git push credentials out of the builder unless the design explicitly accounts for
their exposure. Do not weaken the existing runner's egress policy for convenience.

## Acceptance before adoption

- A representative sequence of jobs shows measured improvement over the single-use runner,
  including queue time, cold and warm analysis, execution, and resource use.
- Concurrent jobs cannot corrupt each other's workspace or output base; failed and interrupted jobs
  do not leave a later job using unreviewed state as authority.
- Source and generated artifacts remain in-cluster, credentials have the intended scope, cache
  retention is bounded, and a builder or cache replacement has a documented recovery path.
- For a persistent Bazel server, a warm no-op run demonstrates reuse in a Bazel profile; a shared
  action cache alone is not evidence that the analysis graph stayed warm.
