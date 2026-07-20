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
- **Repo-scoped runner** registered only to the `haku-state` repo, so it only ever runs Haku's
  jobs; `capacity: 1` (serial builds).
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
| `deployment.yaml`    | the act_runner + rootless `dind` sidecar (+ Bazel-cache init/mount)                     |
| `pvc.yaml`           | persistent Bazel cache (output base + `--disk_cache`), bind-mounted into job containers |

The registration-token Secret (`haku-ci-runner-token`) is provisioned by `tf/gitops/haku-state`
(a `hashicorp/http` GET of the repo's runner registration-token API, written to the Secret) —
not committed here.

`flux-kustomization.yaml` (root-wired) applies this dir; `wait: false` because the runner stays
pending until that token Secret lands.
