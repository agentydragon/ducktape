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
| `k8s/`              | the `haku-ui` workload starter                                           | Haku's GitOps workload dir (below)                           |

There is intentionally **no `dashboard/`** — the console renders the dashboard from
`items/` at request time, so Haku commits no dashboard page.

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
