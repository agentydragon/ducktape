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
- **Rootless builder** (`docker:27-dind-rootless`, non-privileged) — a node escape would break
  the whole Haku perimeter, so no privileged docker-in-docker (unlike the parked `docker-ci`).
- **Repo-scoped runner** registered only to the `haku-state` repo, so it only ever runs Haku's
  jobs; `capacity: 1` (serial builds).
- **Scoped creds**: pushes only to the `haku/*` Forgejo package namespace; commits back only to
  `haku-state`. No cluster creds (`automountServiceAccountToken: false`).

Worst case is bounded: the design already treats Haku's UI as 100% agent-authored and
adversarial (containment = cross-origin iframe isolation + the capability gate + the openLink
scheme-gate). A CI-built image vs committed files doesn't widen that — it only changes _how_
Haku produces the image, and the runner can't escape its perimeter.

## ⚠️ Paving steps (not yet done — fill in during the iterate loop)

1. **Registration token (required — the pod stays pending without it).** After Forgejo Actions
   is live (#2556), generate a **repo-scoped** runner registration token for `haku-state`
   (Forgejo → the `haku/haku-state` repo → Settings → Actions → Runners → "Create new runner"),
   and provision it as Secret `haku-ci-runner-token` (key `token`) in `haku-ci` — as a
   SOPS-managed `runner-token.sops.yaml` here, or via `tf/gitops/haku-state`.
2. **Validate the builder.** Rootless `dind` on Talos is the **main risk** and may need
   securityContext/seccomp tuning, `/dev/fuse`, or a switch to **rootless buildkit** if the
   daemon won't start. Check `kubectl -n haku-ci logs deploy/haku-runner -c dind`, run a trivial
   `.forgejo/workflows/` build, and iterate here.

## What's here

| File                 | Role                                                                |
| -------------------- | ------------------------------------------------------------------- |
| `namespace.yaml`     | the `haku-ci` namespace                                             |
| `networkpolicy.yaml` | egress fence (DNS + registries/npm/pypi + in-cluster)               |
| `runner-config.yaml` | the act_runner `config.yaml` (labels, dind `DOCKER_HOST`, capacity) |
| `deployment.yaml`    | the act_runner + rootless `dind` sidecar                            |

`flux-kustomization.yaml` (root-wired) applies this dir; `wait: false` because the runner is
pending until the token lands.
