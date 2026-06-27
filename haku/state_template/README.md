# haku/state_template — first-run starter for haku-state

Starter content Haku copies into its `haku-state` repo on first run, **only if
absent**. Haku has the ducktape checkout, so the "seed" is a copy Haku does itself —
no operator Job or Terraform provisioner, and nothing here is applied from ducktape.
It is **not** in `haku/base/` and is **not** baked into Haku's image; it's a
copy-source Haku reads at run time, so `haku-state` stays Haku-authored.

It is a **skeleton with placeholders**, not descriptions — the durable contract for
each dir lives in `haku/base/instructions.md` (linked from the stubs). Layout mirrors
`haku-state`'s root:

| Dir                 | Starter content                                                          | After seed                                                   |
| ------------------- | ------------------------------------------------------------------------ | ------------------------------------------------------------ |
| `items/`            | empty (`.gitkeep`)                                                       | Haku writes one `<id>.yaml` per item                         |
| `intake/processed/` | empty (`.gitkeep`)                                                       | operator feedback lands in `intake/`; reduced → `processed/` |
| `log/`              | empty (`.gitkeep`)                                                       | per-day run journal `log/YYYY-MM-DD.md`                      |
| `memory/`           | placeholder stubs (operator model, situational awareness, base-sync pin) | Haku's to restructure freely                                 |
| `ui/`               | the ported item UI (React SPA + FastAPI backend + Dockerfile)            | Haku's own UI service, CI-built (below)                      |
| `.forgejo/`         | the `build-ui` Forgejo Actions workflow                                  | Haku's CI: build image → push registry (Flux bumps the tag)  |
| `k8s/`              | the `haku-ui` workload (Deployment + Service for the CI-built image)     | Haku's GitOps workload dir (below)                           |

There is intentionally **no `dashboard/`** — the console renders its own dashboard from
`items/` at request time. The `ui/` app is Haku's **separate** item UI (embedded in the
console's Free-form UI iframe), which Haku owns and evolves.

## `ui/` — Haku's own item UI service (CI-built)

The **ported** item UI (from `haku/console/`) Haku runs in `haku-sandbox`, embedded in
the console's Free-form UI iframe. Full read+write: it reads `items/` and writes operator
intent — `clicks/<item-id>/<action-id>` and `intake/<ts>-feedback[-<id>].md`, the
conventions Haku reduces on its next run. It is **starter source only**: the build
artifact is a container image produced by **Forgejo CI**, never a committed `dist/`. Haku
adopts it into `haku-state`, **watches its Forgejo CI builds, fixes broken builds, tends
the deployment, and evolves the UI** freely. Full detail: [`ui/README.md`](ui/README.md).

Build-via-CI flow: Haku commits `ui/` + the `.forgejo/workflows/build-ui.yaml` workflow
→ the contained, repo-scoped Forgejo runner (`cluster/k8s/haku-ci`) builds + pushes
`git.allegedly.works/haku/ui:main-<utc>-<sha>` → **Flux image automation** (operator-owned,
in ducktape `cluster/k8s/...`) writes the newest tag into `k8s/haku-ui/deployment.yaml` at
its `{"$imagepolicy": ...}` marker → Flux reconciles `haku-state` `k8s/` → the `haku-ui`
Deployment rolls the new image. CI never edits a manifest. (No Bazel/BuildBuddy —
`haku-state` may hold private operator info, so its source never leaves the cluster.)

## `k8s/` — Haku's GitOps workload dir

`state_template/k8s/` seeds haku-state's `k8s/`, reconciled by an **operator-owned**
Flux `Kustomization` (in ducktape `cluster/k8s/...`) that runs `kustomize build ./k8s`
from haku-state under a **constrained impersonation ServiceAccount** — it may apply
only Deployments/Services/ConfigMaps/Jobs/CronJobs/PVCs in `haku-sandbox`, and Kyverno
denies any Gateway-API route. So Haku gets a GitOps path for persistent workloads (not
just ad-hoc `kubectl apply`) without being able to widen its own perimeter.

The starter holds one workload, `haku-ui` — the Deployment + Service for the CI-built
image (`git.allegedly.works/haku/ui:<tag>`, the tag Flux image automation writes), served behind the
operator-owned, Authentik-gated `haku-ui.allegedly.works` route and embedded in the
console iframe (see `haku/console/plans/free_form_ui_iframe.md`). It pulls via the
`haku-forgejo-registry-pull` imagePullSecret (operator-provisioned) and mounts the
`haku-state-git-write` secret for the backend's git access. Haku evolves the UI and adds
more workloads as sibling dirs under `k8s/`.
