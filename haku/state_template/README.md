# haku/state_template — k8s workload starter for haku-state

Starter manifests Haku copies into its `haku-state` repo on first run, **only if
absent**. Haku has the ducktape checkout, so the "seed" is a copy Haku does itself —
no operator Job or Terraform provisioner, and nothing here is applied from ducktape.
It is **not** in `haku/base/` and is **not** baked into Haku's image; it's a
copy-source Haku reads at run time, so `haku-state` stays Haku-authored.

Scope is **only `k8s/`**. The rest of `haku-state` (`items/`, `intake/`, `memory/`,
`log/`, `dashboard/`) has no template — Haku creates those itself, per
`haku/base/instructions.md`.

## `k8s/` — Haku's GitOps workload dir

`state_template/k8s/` seeds haku-state's `k8s/`, reconciled by an **operator-owned**
Flux `Kustomization` (in ducktape `cluster/k8s/...`) that runs `kustomize build ./k8s`
from haku-state under a **constrained impersonation ServiceAccount** — it may apply
only Deployments/Services/ConfigMaps/Jobs/CronJobs/PVCs in `haku-sandbox`, and Kyverno
denies any Gateway-API route. So Haku gets a GitOps path for persistent workloads (not
just ad-hoc `kubectl apply`) without being able to widen its own perimeter.

The starter holds one workload, `haku-ui` — a placeholder served behind the
operator-owned, Authentik-gated `haku-ui.allegedly.works` route and embedded in the
console iframe (see `haku/console/plans/free_form_ui_iframe.md`). Haku replaces the page
with its real UI and adds more workloads as sibling dirs under `k8s/`.
