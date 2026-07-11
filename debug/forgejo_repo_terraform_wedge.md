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
2. **Non-atomic create (the mirror case, new).** What is **proven** (2026-07-11): the repo was
   created in Forgejo at 02:44:00 and fully cloned (515 MB, 30 branches, mirror synced 07:52),
   yet it is absent from the pg state backend — the live plan shows `1 to add`, so TF tries to
   create it and Forgejo answers "already exists". So: created in Forgejo, never recorded in
   state. The exact create-time failure is **not in retained controller logs** (rotated), so
   the record-failure mechanism is a **hypothesis, not confirmed**: the svalabs provider's
   Create for `mirror = true` calls Forgejo's migrate endpoint (repo row created immediately),
   then Reads computed fields (`default_branch`, `size`, … all "known after apply"); if that
   Read raced the in-progress clone and errored on a not-yet-populated field, Create returns an
   error → TF doesn't persist the resource → orphan. Alternative (not distinguishable from
   surviving evidence): a controller apply-timeout / runner interruption between the migrate
   call and state write. To confirm next time: capture the runner logs at first-create, or
   repro against a scratch mirror repo. A permanent import block can NOT pre-guard this (import
   of a not-yet-created repo errors), so today's fix is a one-shot import + CLEANUP tombstone
   (be3ca248).

   Environmental aggravator seen the same night: a sibling module (`forgejo-props`) failed Init
   with `could not connect to registry.opentofu.org … Client.Timeout` — the controller has
   flaky egress to the provider registry, which makes mid-operation interruptions more likely.

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
