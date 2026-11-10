# Explore self-hosted BuildBuddy runners and executors

Recorded 2026-11-05. Explore using cluster capacity for both outer Bazel runs and
individual build/test actions, including reuse of expensive integration-test
infrastructure. This is future work; the experiments below have not been run.

## Roles and starting points

| Role                  | Runs                                                                    | Configuration to inspect                                                                                                |
| --------------------- | ----------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| Remote Bazel runner   | Git checkout, Bazel server, analysis and action dispatch                | [`.bbrc`](../../../.bbrc), [runner image](../images/runner/)                                                            |
| RBE action executor   | Individual compile, lint and test actions                               | [RBE platform](../../../BUILD.bazel), [executor HelmRelease](../../../cluster/k8s/buildbuddy-executor/helmrelease.yaml) |
| Dedicated VM executor | Docker-isolated actions requiring nested Kubernetes/KubeVirt privileges | [`kvm-vm` worker](../kvm/worker/executor.yaml), [KVM test routing](../../../agentplane/system_tests/k3s/defs.bzl)       |

The outer runner currently selects hosted Firecracker execution. Changing an
inner action's pool does not move that outer runner. The dedicated `kvm-vm`
executor runs inside a KubeVirt VM but uses Docker action isolation and explicitly
disables Firecracker; the self-hosted KVM test properties disable recycling.

[PR #14471](https://github.com/agentydragon/ducktape/pull/14471) proposes an opt-in
ordinary-test pilot on OCI executors, placed on Linux/amd64 worker nodes outside
the control plane. It is open at the time of these notes. Check its current merge
and rollout state before building on it. Reuse the existing GitOps owner when
changing the pod pool.

## Questions to investigate

- Can we run the outer Remote Bazel runner on our cluster with the existing
  runner image, working Git authentication, Nix tools and a warm Bazel server?
  Compare container recycling with a dedicated Firecracker-capable pool; identify
  the host capabilities, executor flags and persistent storage each needs.
- Can selected ordinary tests use self-hosted executors while other actions stay
  hosted? Start with the explicit routing pilot. Ask BuildBuddy whether they
  support automatic placement/overflow across our capacity and their managed
  infrastructure. Current public configuration selects one backend/pool per
  action; matching pool names does not establish a combined pool.
- What is our actual organization concurrency entitlement, and which limits or
  cache-transfer charges apply to self-hosted workloads? Verify it rather than
  sizing from the remembered 40-vCPU limit or current public plan advertising.
- Which infrastructure should be retained between integration tests: databases,
  nested Kubernetes, image caches, or other expensive daemons? Keep application
  state and fixture identities fresh even when infrastructure is reused.

## Recycling options

Self-hosting does not inherently prevent runner recycling. BuildBuddy documents
`recycle-runner` alongside its supported self-hosted isolation runtimes; the
[executor implementation](https://github.com/buildbuddy-io/buildbuddy/blob/master/enterprise/server/remote_execution/runner/runner.go)
requires authenticated tasks for recycling. Capability in upstream code is not
proof of reuse on our deployed executor version.

| Option                      | Reuse model                                                        | What to establish                                                                                                                                                     |
| --------------------------- | ------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| OCI/Docker runner recycling | Retain and pause a container on one executor between actions       | Daemon processes survive; health checks and reset work; measure idle RAM and eviction behavior. Container reuse does not transfer live processes to another node.     |
| Self-hosted Firecracker     | Preserve VM memory, processes and disk; optionally share snapshots | Working KVM/host configuration, local versus remote snapshot settings, restore health and publication/transfer costs. Our Docker VM worker is not already this setup. |
| Managed cluster Service     | Several concurrent test actions connect to one running daemon      | Explicit ownership, network reachability, per-test namespaces/databases, cleanup and concurrency isolation. This would be our service design.                         |

For recyclable environments, use a versioned `test.runner-recycling-key` for the
retained infrastructure. Compatible targets need matching effective execution
properties. Rebuild on a cold start or health/version mismatch; correctness must
survive eviction. Review `preserve-workspace` separately from process reuse.
Remote persistent workers are another mechanism for tools implementing Bazel's
worker protocol, rather than a general integration-service manager.

Keep snapshot policy decisions with the existing
[snapshot reuse plan](snapshot-reuse/README.md), including its provider questions
and unresolved daemon-health observations. Avoid introducing forced snapshot
saves as part of a self-hosting trial.

## Suggested experiments

1. Establish a baseline with a small ordinary test and the existing hosted outer
   runner. Record the actual TestRunner worker ID and effective execution
   properties to prove routing. Keep the default CI configuration unchanged for
   the first opt-in trial.
2. Choose one heavy daemon and opt its test actions into container recycling on
   a self-hosted executor. Compare an uncached cold run with a second compatible
   run and a different compatible target. Record a daemon identity and health
   check so a passing test alone cannot be mistaken for warm reuse.
3. Exercise version-key invalidation, concurrent actions, executor restart and
   unhealthy retained state. Require fresh per-test data, complete cleanup and a
   working cold fallback. Expect multiple runners under concurrency; runner
   recycling does not provide a single daemon shared across the fleet.
4. Try a self-hosted outer runner independently of inner action placement. Verify
   checkout, analysis reuse, cancellation, artifact/log publication and that
   inner actions still reach their intended hosted or self-hosted pools.
5. If container reuse is insufficient, compare a Firecracker trial with an
   explicitly managed shared service. Measure setup and action wall time, queue
   time, idle/peak CPU and memory, disk use, cache traffic and snapshot costs.
   Size against live worker headroom and existing workloads, excluding control
   planes and roaming nodes initially. Retain the hosted path for comparison and
   fallback while deciding whether to expand the cohort.

## Upstream references

- [Execution properties and self-hosted runtimes](https://www.buildbuddy.io/docs/rbe-platforms/)
- [Daemon reuse across Firecracker test actions](https://www.buildbuddy.io/docs/rbe-microvms/#preserving-microvm-state-across-actions)
- [Remote runner recycling and snapshots](https://www.buildbuddy.io/docs/remote-runner-introduction/)
- [Executor configuration, including snapshot-sharing flags](https://www.buildbuddy.io/docs/config-all-options/)
