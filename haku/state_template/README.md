# haku/state_template — first-run starter for haku-state

Starter content Haku copies into its `haku-state` repo on first run, **only if
absent**. Haku has the ducktape checkout, so the "seed" is a copy Haku does itself —
no operator Job or Terraform provisioner, and nothing here is applied from ducktape.
It is **not** in `haku/base/` and is **not** baked into Haku's image; it's a
copy-source Haku reads at run time, so `haku-state` stays Haku-authored.

It seeds both placeholders **and** Haku's starting **method** — the procedures it runs, the
UI it serves, and one example working format (the "items" board). Base (`haku/base/`) holds
the durable job and judgment, **item-agnostic**; the concrete method is documented here and
is Haku's to evolve or replace. Layout mirrors `haku-state`'s root:

## Principle: a generic starter, not a personal backup

This directory is the **starter a brand-new haku instance scaffolds from** — it must read as
**person-agnostic**, usable by any operator. As Haku's method evolves in a live `haku-state`,
the worthwhile evolutions get carried back here, but with a hard filter:

- **DO seed the generic, structural, high-level method** — the architecture (Haku owns and
  evolves the **whole** multi-surface UI, not a fixed board), the surfaces every instance wants
  (the **items board** + the **Improvements** self-backlog), the CI/deploy pipeline, the generic
  procedures, the item format (validated by `ui/backend/models.py`), and the `k8s` workload starter.
- **NEVER seed the operator's personal specifics** — their actual `items/`, the _content_ of
  their `memory/` (operator model, situational awareness, finances, bookmarks), their logs, or
  **surfaces Haku built around one operator's particular life/accounts** (e.g. the live instance's
  household-inventory board around their pantry/grocery stack, or a one-off decision page hardcoding
  their name and a personal financial event). Those stay in that operator's `haku-state`; here they'd be noise at
  best and leaked PII at worst. Seed the _pattern_ ("Haku builds bespoke surfaces per the operator's
  life"), documented in prose — not the personal instance of it.

The test for any change: **would it help an arbitrary new operator, with no edit?** If yes, seed
it; if it only makes sense for this person, leave it in their `haku-state`.

| Dir                    | Starter content                                                                            | After seed                                                    |
| ---------------------- | ------------------------------------------------------------------------------------------ | ------------------------------------------------------------- |
| `memory/`              | placeholder stubs (operator model, situational awareness, base-sync pin) + `improvements/` | Haku's to restructure freely                                  |
| `log/`                 | empty (`.gitkeep`)                                                                         | per-day run journal `log/YYYY-MM-DD.md`                       |
| `intake/processed/`    | empty (`.gitkeep`)                                                                         | operator feedback lands in `intake/`; reduced → `processed/`  |
| `responses/`           | empty (`.gitkeep`)                                                                         | operator affordance answers Haku reduces (below)              |
| `runs/`                | `README.md`                                                                                | per-run propagation manifests `runs/<date>/<HHMMSSZ>.md`      |
| `procedures/`          | the starter passes (README + topical files)                                                | Haku's playbook — read + grow (below)                         |
| `ui/`                  | the starter multi-surface UI (React SPA + FastAPI backend + Dockerfile)                    | Haku's own UI service, CI-built (below)                       |
| `items/`               | `README.md` (the example "items" model) + `.gitkeep`                                       | Haku writes one `<slug>.md` per item, if it keeps this format |
| `memory/improvements/` | starter self-backlog (`<id>.md` content collection) → the 💡 tab                           | Haku's capability ideas + run friction, gardened each run     |
| `tools/`               | `validate_state.py` (frontmatter/manifest validator, run by CI)                            | yours to extend as the state model grows                      |
| `.forgejo/`            | the `build-ui` Forgejo Actions workflow                                                    | Haku's CI: build image → push registry (Flux bumps the tag)   |
| `k8s/`                 | the `haku-ui` workload (Deployment + Service for the CI-built image)                       | Haku's GitOps workload dir (below)                            |

The **item model** (`items/`, validated by `ui/backend/models.py` + `tools/validate_state.py`)
and the procedures are **one example
implementation**, not a contract — Haku may redefine or discard them. There is intentionally
**no `dashboard/`**: the trusted console no longer renders items; Haku's own `ui/` service
does, reading this repo at request time, and the console just embeds `ui/` in its Free-form
UI iframe. Everything Haku presents lives here and is Haku's to own and evolve.

## `ui/` — Haku's own UI service (CI-built, multi-surface)

The UI Haku runs in `haku`, embedded in the console's Free-form UI iframe. It is a
**multi-surface app Haku owns and evolves** — not a fixed board. The starter ships two
**person-agnostic** surfaces: the **Inbox** (the items board) and **Improvements** (Haku's
self-backlog, from `memory/improvements/`). It reads `items/*.md` + `memory/improvements/*.md`
through a generic content proxy and writes operator intent — `responses/<scope>/<field>.yaml` and
`intake/<ts>-feedback[-<id>].md`, the conventions Haku reduces on its next run. **Operator-specific
surfaces are not seeded here**:
in a live instance Haku adds bespoke tabs for its operator's life (a kitchen/shopping board, a
one-off decision page, …) — those live in that operator's `haku-state`, per the _generic
starter_ principle above.

It is **starter source only**: the build artifact is a container image produced by **Forgejo
CI**, never a committed `dist/`. Haku adopts it into `haku-state`, **watches its Forgejo CI
builds, fixes broken builds, tends the deployment, and evolves the UI** freely. Full detail:
[`ui/README.md`](ui/README.md).

Build-via-CI flow: Haku commits `ui/` + the `.forgejo/workflows/build-ui.yaml` workflow
→ the contained, repo-scoped Forgejo runner builds + pushes
`forgejo.example.com/haku/ui:main-<utc>-<sha>` → **Flux image automation** (operator-owned,
in ducktape `cluster/k8s/...`) writes the newest tag into `k8s/haku-ui/deployment.yaml` at
its `{"$imagepolicy": ...}` marker → Flux reconciles `haku-state` `k8s/` → the `haku-ui`
Deployment rolls the new image. CI never edits a manifest. (No Bazel/BuildBuddy —
`haku-state` may hold private operator info, so its source never leaves the cluster.)

## `k8s/` — Haku's GitOps workload dir

`state_template/k8s/` seeds haku-state's `k8s/`, reconciled by an **operator-owned**
Flux `Kustomization` (in ducktape `cluster/k8s/...`) that runs `kustomize build ./k8s`
from haku-state under a **constrained impersonation ServiceAccount** — it may apply
only Deployments/Services/ConfigMaps/Jobs/CronJobs/PVCs in `haku`, and Kyverno
denies any Gateway-API route. So Haku gets a GitOps path for persistent workloads (not
just ad-hoc `kubectl apply`) without being able to widen its own perimeter.

The starter holds one workload, `haku-ui` — the Deployment + Service for the CI-built
image (`forgejo.example.com/haku/ui:<tag>`, the tag Flux image automation writes), served behind the
operator-owned, Authentik-gated public route (e.g. `haku-ui.example.com`) and embedded in the
console iframe (see `haku/console/docs/containment.md`). It pulls via a registry-pull
imagePullSecret (operator-provisioned) and mounts the `haku-state-git-write` secret as the
backend's Forgejo API credentials. Haku evolves the UI and adds more workloads as sibling dirs
under `k8s/`.

The manifests and CI are seeded in a **functioning state** with generic defaults — they parse
and wire together as-is. To deploy in your own cluster, change the handful of placeholder values:
the public host (`forgejo.example.com` / `haku-ui.example.com` → yours), and, if they differ from
these defaults, the namespace (`haku`), the Forgejo Actions runner label (`haku-ci`), the
internal Forgejo service (`forgejo-http.forgejo`), the registry-pull + `haku-state-git-write`
secret names, and the image repo (`forgejo.example.com/haku/ui`). Everything else references
those consistently.
