# Recurring wedge: provisioning a new forgejo_repository freezes the Terraform module

Seen twice (2026-07-11 haku/ducktape mirror; earlier agentydragon repos). Pinpoint + durable-fix
menu. Companion to the infra it destabilizes (haku-console + siblings gate on this module).

## Symptom

Adding a `forgejo_repository` resource → first apply creates the repo in Forgejo but the
apply does not record it in state → every later apply fails:

```text
Error: Unable to create repository
  Repository with name "ducktape" already exists
```

The `tofu-controller` Terraform CR stays `Apply: False / Ready: Unknown (InProgress)`; its
Flux `Kustomization` (`flux-system/haku-state`) times out its **Terraform health check** (10m)
and never goes Ready — which freezes **every Kustomization that depends on it**: 2026-07-11
that was `haku-console`, `haku-workloads`, `haku-agent-worker`, `forgejo-token-rotation`,
`haku-ui-image-webhook`, all stuck "dependency haku-state is not ready" on stale images.

## Why state isn't recorded (two triggers, same symptom)

1. **Adoption** — the repo pre-existed outside TF (manually, or another module). First apply
   hits "already exists". Fixed durably by a **permanent `import` block** — this is why
   `tf/gitops/forgejo-agentydragon-repos/main.tf` pairs `forgejo_repository.ducktape` /
   `.gaffer_private` each with an `import {}`. Idempotent; correct.
2. **Non-atomic create (the mirror case, new)** — `ducktape_mirror` has `mirror = true`, so the
   svalabs provider's Create calls Forgejo's **repo-migrate** endpoint: it creates the repo row
   AND kicks off a full clone of ducktape from GitHub. The repo exists in Forgejo the instant
   migrate is accepted, but the provider's Create only returns (and TF only writes state) once
   its call completes. If that window is interrupted — controller apply-timeout, runner pod
   restart, or the provider erroring on the slow mirror sync — the repo is orphaned: exists in
   Forgejo, absent from the pg state backend. Next apply: "already exists". A permanent import
   block can NOT pre-guard this (import of a not-yet-created repo errors), so today's fix is a
   one-shot import + CLEANUP tombstone (commit be3ca248).

State backend is `pg` (`tofu-state-db`), so this is not a lost-state-file problem — it's the
create/record window being non-atomic for migrate-backed repos.

## Durable fixes (ranked; pick one+)

1. **Blast-radius first — stop gating unrelated Kustomizations on this module's health.** A
   flaky repo-provision should never freeze haku-console. Either drop the Terraform health
   check from `flux-system/haku-state`, or split the volatile forgejo-repo provisioning into
   its own module/Kustomization off the critical path. Highest value, lowest risk.
2. **Isolate mirror repos** into a dedicated small tofu module with a generous apply/operation
   timeout, so a multi-minute GitHub clone can't interrupt-orphan or wedge sibling resources.
3. **Convention + check:** every `forgejo_repository` that is _adopted_ carries a paired
   `import` block; every _created_ one is provisioned in an isolated module. Lint for a bare
   `forgejo_repository` on the critical-path module.

## Immediate unblock (done)

`be3ca248` adds a one-shot `import { to = forgejo_repository.ducktape_mirror, id =
"haku/ducktape" }` with a CLEANUP tombstone. Merge → apply reconciles the orphan into state →
the 5 dependent Kustomizations unfreeze → haku-console rolls to current. Remove the import
block after Ready.
