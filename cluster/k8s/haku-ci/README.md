# haku-ci — Forgejo Actions runner for Haku's image builds

Operator-owned, contained CI runner that builds **Haku's own UI image** from `haku-state`
source, entirely in-cluster. `haku-state` may hold private operator data, so its builds must
never touch BuildBuddy/RBE or any external CI — this runner keeps everything inside the
cluster (it pulls source + pushes the image to the in-cluster Forgejo registry). See
`haku/PLAN.md` and `haku/console/docs/containment.md`.

## Trust model — this is agent-controlled compute

The runner executes **Haku-authored** build steps (the `.forgejo/workflows/` + `Dockerfile`
in `haku-state`). A prompt-injected Haku could author a hostile build, so the runner is
contained to roughly Haku's existing sandbox blast radius:

- **Not in `haku-sandbox`.** It lives in its own namespace where Haku has **no RBAC**, so Haku
  can't tamper with the runner pod or its registry/git push creds — but it is **egress-fenced**
  like haku-sandbox (`networkpolicy.yaml`: DNS + base-image registries/npm/pypi + in-cluster
  only).
- **Rootless daemon in a privileged pod** (`docker:27-dind-rootless`, `privileged: true`). The
  dockerd still runs **rootless** (UID 1000), so it's strictly better than classic rootful
  dind — but `privileged: true` is the documented requirement for dind-rootless (it provides
  `/dev/net/tun` and disables the mount masks RootlessKit needs). The non-privileged path was
  attempted and cleared seccomp + `user.max_user_namespaces` but couldn't get past the masks
  (see the paving section). The privileged pod's blast radius is bounded by this namespace
  being operator-only (no Haku RBAC), pinned **off the control planes**, and egress-fenced.
- **Repo-scoped runners** registered only to the `haku-state` repo, so they only ever run Haku's
  jobs; each has `capacity: 1`. KEDA adds up to four pods only while matching jobs queue.
  Each pod registers under its own Kubernetes pod name so concurrent replicas cannot collide.
- **Scoped creds**: pushes only to the `haku/*` Forgejo package namespace; commits back only to
  `haku-state`. No cluster creds (`automountServiceAccountToken: false`).

Worst case is bounded: the design already treats Haku's UI as 100% agent-authored and
adversarial (containment = cross-origin iframe isolation + the capability gate + the openLink
scheme-gate). A CI-built image vs committed files doesn't widen that — it only changes _how_
Haku produces the image, and the runner can't escape its perimeter.

## Validated build path

The dind daemon runs as privileged-pod rootless Docker (after the non-privileged path
dead-ended on the mount masks). Its Docker Hub pulls go through `oci-cache` with Docker's
Hub-only `--registry-mirror`; rootless dind listens on `tcp://127.0.0.1:2375`, not
`/var/run/docker.sock`.

The current end-to-end validation is a real haku-state `validate-state` workflow on Forgejo.
As of 2026-07-05, the default branch is green after a push-triggered run (`/haku/haku-state`
Actions run 336). A quick daemon-side mirror smoke test is:

```bash
kubectl -n haku-ci exec deploy/haku-runner -c dind -- \
  docker -H tcp://127.0.0.1:2375 pull --platform=linux/amd64 catthehacker/ubuntu:act-latest
```

If this regresses, check the job log first. Two known failure signatures from 2026-07-05:
`no basic auth credentials` against the `oci-cache` mirror means Zot auth is on the internal
listener instead of only the public proxy; Docker Hub timeouts after a mirror rejection mean
dockerd fell back to direct `https://registry-1.docker.io/v2/`. Schema2 child-manifest
rejections are handled by `oci-cache`'s Zot `http.compat: ["docker2s2"]` setting.

## What's here

| File                 | Role                                                                                    |
| -------------------- | --------------------------------------------------------------------------------------- |
| `namespace.yaml`     | the `haku-ci` namespace                                                                 |
| `networkpolicy.yaml` | egress fence (DNS + registries/npm/pypi + in-cluster)                                   |
| `config.yaml`        | the act_runner config (labels, dind `DOCKER_HOST`, capacity, job-container `-v` mounts) |
| `deployment.yaml`    | the KEDA-scaled act_runner + rootless `dind` sidecar (+ pod-local Bazel cache)          |
| `scaledobject.yaml`  | KEDA Forgejo queue trigger (0–4 runner pods) and token authentication                   |

The registration-token Secret (`haku-ci-runner-token`) is provisioned by `tf/gitops/haku-state`
(a `hashicorp/http` GET of the repo's runner registration-token API, written to the Secret) —
not committed here.

`flux-kustomization.yaml` (root-wired) applies this dir; `wait: false` because the runner stays
pending until that token Secret lands.

## Queue autoscaling

Forgejo's Prometheus `/metrics` endpoint does **not** expose Actions queue depth. Instead, KEDA
2.20.2's native `forgejo-runner` scaler calls Forgejo's authenticated,
repo-scoped `/api/v1/repos/haku/haku-state/actions/runners/jobs?labels=haku-ci` endpoint. Its
response is the set of jobs waiting for this runner label, so KEDA scales one pod per queued job,
from zero to the hard cap of four.

`haku-forgejo-tea` is minted by `forgejo-token-rotation` and reflected from `haku-sandbox` into
this operator-only namespace. The runner pod does not mount that Secret; only KEDA reads it.
Each runner uses an `emptyDir` cache, so it is warm for consecutive jobs on that pod but is
intentionally discarded when KEDA scales down.

**Gotcha: the trigger counts QUEUED jobs, so a running job reads as zero demand.** Nothing tells
the HPA which pod is busy, so once `stabilizationWindowSeconds` (600s) elapses it will delete a
pod that is mid-build. Two settings make that survivable, and **both are required** — either one
alone still drops the job:

| setting                                            | where             | value |
| -------------------------------------------------- | ----------------- | ----- |
| `runner.shutdown_timeout`                          | `config.yaml`     | 30m   |
| `spec.template.spec.terminationGracePeriodSeconds` | `deployment.yaml` | 2100  |

The first tells act_runner to finish the running job on SIGTERM; the second stops the kubelet
SIGKILLing it 30 seconds in (the default, and the reason `bazel-ci / image` died on every
ducktape repin — the only builds long enough to outlive the window). Keep `shutdown_timeout` at
or below the grace period, and both at or above `runner.timeout`.

This makes a reaped job survive; it does not stop the reaping. The metric would have to count
in-progress jobs as well as queued ones for the HPA to stop wanting the pod gone.
