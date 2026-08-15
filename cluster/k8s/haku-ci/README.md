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

- **Not in `haku-sandbox`.** It lives in its own namespace where Haku has only the generated
  read-only logs/configmaps binding, so Haku can't tamper with the runner pod or its
  registry/git push creds — but it is **egress-fenced**
  like haku-sandbox (`networkpolicy.yaml`: DNS + base-image registries/npm/pypi + in-cluster
  only).
- **Rootless daemon in a privileged pod** (`docker:27-dind-rootless`, `privileged: true`). The
  dockerd still runs **rootless** (UID 1000), so it's strictly better than classic rootful
  dind — but `privileged: true` is the documented requirement for dind-rootless (it provides
  `/dev/net/tun` and disables the mount masks RootlessKit needs). The non-privileged path was
  attempted and cleared seccomp + `user.max_user_namespaces` but couldn't get past the masks
  (see the paving section). The privileged pod's blast radius is bounded by this namespace
  being operator-only for writes, pinned **off the control planes**, and egress-fenced.
- **Repo-scoped, single-use runners** registered only to the `haku-state` repo, so they only ever
  run Haku's jobs. Each pod registers ephemerally under its own Kubernetes pod name (so concurrent
  pods cannot collide), runs exactly one job, and exits — a hostile build cannot outlive its own
  job or observe the next one. KEDA creates up to four pods, only while matching jobs queue.
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

| File                           | Role                                                                                                                           |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------ |
| `namespace.yaml`               | the `haku-ci` namespace                                                                                                        |
| `ccnp-force-proxy-egress.yaml` | egress fence (DNS + in-cluster + haku-egress-proxy only)                                                                       |
| `config.yaml`                  | the forgejo-runner config (labels, dind `DOCKER_HOST`, capacity, job-container `-v` mounts)                                    |
| `scaledjob.yaml`               | KEDA `ScaledJob`: the one-job runner pod + rootless `dind` native sidecar, the Forgejo queue trigger, and token authentication |

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

### Why `ScaledJob` and not `ScaledObject`

**The trigger counts QUEUED jobs, so a job that has started reads as zero demand.** That is
correct for the metric and fatal for a Deployment. Under the original `ScaledObject`, the same
number that scaled up also scaled down: the moment a runner picked a job up the metric returned
to zero, and once `stabilizationWindowSeconds` (600s) elapsed the HPA deleted a pod — with no
way to know which pod was mid-build. Only `bazel-ci / image` was long enough to be exposed, and
it died on essentially every ducktape repin.

Two settings were added to make a reaped build survivable — `runner.shutdown_timeout: 30m` and
`terminationGracePeriodSeconds: 2100` — and **they did not work**. On PR #98 the runner pod was
deleted at 23:44:36Z and the job failed at 23:44:57Z, twenty-one seconds later, with both
settings live. The reason is that `dind` was an ordinary container: ordinary containers are
SIGTERMed **in parallel**, so dockerd died alongside the runner and destroyed the build
containers underneath it. The runner then waited its full graceful 30 minutes on a job that no
longer had anywhere to run. (It also explains the silent failure: the `if: failure()` log-publish
step needs Docker to start a container, and Docker was what died.)

Under a `ScaledJob` the queued-jobs metric is only ever a **create** signal. KEDA turns "N jobs
queued" into "create N Kubernetes Jobs", never deletes a running one, and each pod ends its own
life when its single CI job finishes — so there is no scale-down decision to get wrong. The
failure mode is structurally absent rather than mitigated. This is also the shape the scaler is
[documented for](https://keda.sh/docs/2.20/scalers/forgejo/).

`dind` is correspondingly a **native sidecar** (an `initContainer` with `restartPolicy: Always`),
which is now doing two jobs: it fixes the termination ordering above, and it is what lets the
Job ever complete — an ordinary `dind` container would never exit, so the Job would hang forever.

### Ephemeral registration

Each pod runs `forgejo-runner register --ephemeral` and then `one-job --wait`. `--ephemeral`
instructs Forgejo to delete the registration once the runner has run one job; it requires Forgejo
15+ (this instance is 15.0.3) and is refused outright by older servers, so it fails loudly rather
than drifting.

The old Deployment's comment claimed its per-pod registration was "ephemeral", but the flag was
absent, and nothing ever deregistered. By 2026-08-09 `haku/haku-state` had accumulated **529
runner registrations, 525 of them offline**. Those pre-existing dead entries are harmless (the
scaler matches on labels, not runner identity) but should be swept once.

### What this costs: Bazel cache locality

The `emptyDir` Bazel cache used to warm **successive** jobs on a surviving runner pod. One job
per pod means it now only spans the Bazel invocations **within** a CI job — `bazel-ci`'s `Test`
step then its `Build` step, which is still most of the benefit. It was already discarded whenever
KEDA scaled to zero (`cooldownPeriod: 600`), so bursty runs more than ten minutes apart were
already cold; this makes that the norm rather than the common case. `runner.timeout` was raised
30m → 1h to absorb it, since `bazel-ci / image` had been observed at 27m03s against the old 30m
ceiling.

If that proves too slow, the fix is a shared `--disk_cache` (content-addressed and safe for
concurrent readers, unlike the output base) on a real volume rather than reverting the
architecture.

### Upgrading the runner image

The image is pinned (`12.13.2`) rather than floating on `:6` as before. That jump crosses
**8.0.0, which added strict schema validation of workflow files** — a workflow that ran on 6.x
but does not parse will now be **refused**, not run. Validate before bumping:

```bash
forgejo-runner exec --event unknown --workflows .forgejo/workflows/bazel-ci.yaml
```

8.0.0 also changed default label resolution and falls back to `sh` when `bash` is absent; neither
affects this runner, which sets its image explicitly via the `haku-ci:docker://...` label.
