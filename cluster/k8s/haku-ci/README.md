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
  through the **same `haku-squid`** that fences haku-sandbox: `ccnp-force-proxy-egress.yaml`
  permits only DNS, cluster-internal (the in-cluster Forgejo git + registry), and
  `haku-squid:3128`. All external egress (base images, npm/pypi, Bazel/toolchain,
  Forgejo actions) flows through the proxy's allowlist
  (`agents/haku-squid/cnp-haku-cloud-api-egress.yaml`), where Squid caches immutable
  build-dep GETs. The proxy CA is trusted by the runner + dind (Deployment env/mounts)
  and injected into job containers via `config.yaml`.
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

## ⚠️ Paving — validate a real build

The dind daemon now starts (privileged-pod rootless, after the non-privileged path dead-ended
on the mount masks). What's left is an end-to-end check: trigger a `.forgejo/workflows/` build
on the runner and confirm it builds + pushes an image. Check
`kubectl -n haku-ci logs deploy/haku-runner -c dind` (daemon up) and the runner's job logs.

## What's here

| File                           | Role                                                                                                             |
| ------------------------------ | ---------------------------------------------------------------------------------------------------------------- |
| `namespace.yaml`               | the `haku-ci` namespace                                                                                          |
| `ccnp-force-proxy-egress.yaml` | egress fence: DNS + in-cluster + `haku-squid:3128` only (all external egress via the proxy)                      |
| `config.yaml`                  | the act_runner config (labels, dind `DOCKER_HOST`, capacity, job-container proxy/CA), via a `configMapGenerator` |
| `deployment.yaml`              | the act_runner + rootless `dind` sidecar (proxy env + haku-squid CA mount)                                       |

The registration-token Secret (`haku-ci-runner-token`) is provisioned by `tf/gitops/haku-state`
(a `hashicorp/http` GET of the repo's runner registration-token API, written to the Secret) —
not committed here.

`flux-kustomization.yaml` (root-wired) applies this dir; `wait: false` because the runner stays
pending until that token Secret lands.
