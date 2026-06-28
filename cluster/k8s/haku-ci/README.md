# haku-ci — Forgejo Actions runner for Haku's image builds

Operator-owned, contained CI runner that builds **Haku's own UI image** from `haku-state`
source, entirely in-cluster. `haku-state` may hold private operator data, so its builds must
never touch BuildBuddy/RBE or any external CI — this runner keeps everything inside the
cluster (it pulls source + pushes the image to the in-cluster Forgejo registry). See
`haku/PLAN.md` and `haku/console/plans/free_form_ui_iframe.md`.

## Trust model — this is agent-controlled compute

The runner executes **Haku-authored** build steps (the `.forgejo/workflows/` + `Dockerfile`
in `haku-state`). A prompt-injected Haku could author a hostile build, so the runner is
contained to roughly Haku's existing sandbox blast radius:

- **Not in `haku-sandbox`.** It lives in its own namespace where Haku has **no RBAC**, so Haku
  can't tamper with the runner pod or its registry/git push creds — but it is **egress-fenced**
  like haku-sandbox (`networkpolicy.yaml`: DNS + base-image registries/npm/pypi + in-cluster
  only).
- **Rootful dind in a privileged pod** (`docker:27-dind`, `privileged: true`). Using rootful
  (not -rootless) so Docker uses standard veth interfaces that Cilium's DNS proxy can see —
  rootless dind's slirp4netns networking bypasses the normal stack, so Cilium FQDN policies
  don't apply and Docker Hub image pulls fail. The privileged pod's blast radius is bounded by
  this namespace being operator-only (no Haku RBAC), pinned **off the control planes**, and
  egress-fenced.
- **Repo-scoped runner** registered only to the `haku-state` repo, so it only ever runs Haku's
  jobs; `capacity: 1` (serial builds).
- **Scoped creds**: pushes only to the `haku/*` Forgejo package namespace; commits back only to
  `haku-state`. No cluster creds (`automountServiceAccountToken: false`).

Worst case is bounded: the design already treats Haku's UI as 100% agent-authored and
adversarial (containment = cross-origin iframe isolation + the capability gate + the openLink
scheme-gate). A CI-built image vs committed files doesn't widen that — it only changes _how_
Haku produces the image, and the runner can't escape its perimeter.

## ⚠️ Paving — validate a real build

Switched from rootless to rootful dind to fix Cilium FQDN tracking (rootless slirp4netns
bypasses DNS proxy → Docker Hub blocked). What's left is confirming the end-to-end build
works: trigger a `.forgejo/workflows/` build on the runner and confirm it builds + pushes
an image. Check `kubectl -n haku-ci logs deploy/haku-runner -c dind` (daemon up) and the
runner's job logs.

## What's here

| File                 | Role                                                                                     |
| -------------------- | ---------------------------------------------------------------------------------------- |
| `namespace.yaml`     | the `haku-ci` namespace                                                                  |
| `networkpolicy.yaml` | egress fence (DNS + registries/npm/pypi + in-cluster)                                    |
| `config.yaml`        | the act_runner config (labels, dind `DOCKER_HOST`, capacity), via a `configMapGenerator` |
| `deployment.yaml`    | the act_runner + rootful `dind` sidecar                                                  |

The registration-token Secret (`haku-ci-runner-token`) is provisioned by `tf/gitops/haku-state`
(a `hashicorp/http` GET of the repo's runner registration-token API, written to the Secret) —
not committed here.

`flux-kustomization.yaml` (root-wired) applies this dir; `wait: false` because the runner stays
pending until that token Secret lands.
