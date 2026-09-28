# Recurring wedge: provisioning a new forgejo_repository freezes the Terraform module

Seen twice (2026-07-11 haku/ducktape mirror; earlier agentydragon repos). Pinpoint + durable-fix
menu. Fix (one-shot import) + this note: **PR #3028**. Companion to the infra it destabilizes (haku-console + siblings gate on this module).

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
2. **Non-atomic create (the mirror case) — CONFIRMED from provider source**
   (`svalabs/terraform-provider-forgejo`, `internal/provider/repository_resource.go`). The
   Create flow: `MigrateRepo()` (creates the repo in Forgejo + starts the GitHub clone) →
   `resp.State.Set` at **line 1368**, immediately after a _successful_ migrate → then
   create-then-edit `EditRepo` → final State.Set. Because state is saved the instant migrate
   returns OK, an orphan (repo in Forgejo, absent from state) can only mean **`MigrateRepo`
   returned an error to the provider while Forgejo created + kept cloning the repo anyway** —
   the provider hits its error branch and returns _before_ line 1368, with no rollback and no
   adopt-on-conflict. (This disproves the earlier read-race/edit-failure guesses: both occur
   after 1368 and would still leave the repo in state.)

   Proven for this incident: repo created 02:44:00, fully cloned by 07:52 (515 MB, 30
   branches); state lacks it (live plan = `1 to add`). The exact 02:44 error text rotated out
   of logs, but the only code path that fits is a MigrateRepo failure — and the fitting cause
   for _this_ repo is a **timeout on the synchronous migrate call**, which is now **empirically
   proven** (2026-07-11, live migrate probes against this Forgejo): the migrate API blocks
   until the clone completes, duration scaling by repo size — octocat/Hello-World (~KB) in
   **3.5 s**, hashicorp/terraform (hundreds of MB) in **77.8 s**, both returning `empty: false`
   (fully cloned within the call). A 515 MB repo blocks longer; 77 s already brushes common
   60 s proxy/ingress defaults, so the likely tripped timeout is the gateway response timeout
   (or the runner context), not necessarily the SDK. If any such timeout is below the clone
   duration, the client errors while Forgejo finishes — the operator's "because it was still
   cloning." Not yet pinned: which specific timeout fired at 02:44 (needs the rotated logs or a
   size-matched repro). Aggravator seen the same
   night: sibling module `forgejo-props` failed Init with `could not connect to
registry.opentofu.org … Client.Timeout`, so the controller had flaky egress then. A
   permanent import block can't pre-guard a to-be-created repo, so today's fix is a one-shot
   import + CLEANUP tombstone (be3ca248).

State backend is `pg` (`tofu-state-db`), so this is not a lost-state-file problem — it's the
create/record window being non-atomic for migrate-backed repos.

## Durable fixes (ranked; pick one+)

1. **Blast-radius first — stop gating unrelated Kustomizations on this module's health.** A
   flaky repo-provision should never freeze haku-console. Either drop the Terraform health
   check from `flux-system/haku-state`, or split the volatile forgejo-repo provisioning into
   its own module/Kustomization off the critical path. Highest value, lowest risk.
2. **Isolate mirror repos** into a dedicated module, AND avoid the timeout entirely: either
   raise the migrate client timeout if the provider exposes it, or provision the mirror by a
   direct Forgejo migrate API call (Job) that's async/idempotent, then let TF only _read_ it.
   The svalabs create is structurally fragile for large mirrors — a synchronous migrate whose
   failure orphans the repo with no rollback.
3. **Convention + check:** every `forgejo_repository` that is _adopted_ carries a paired
   `import` block; every _created_ one is provisioned in an isolated module. Lint for a bare
   `forgejo_repository` on the critical-path module.

## Immediate unblock (done)

`be3ca248` adds a one-shot `import { to = forgejo_repository.ducktape_mirror, id =
"haku/ducktape" }` with a CLEANUP tombstone. Merge → apply reconciles the orphan into state →
the 5 dependent Kustomizations unfreeze → haku-console rolls to current. Remove the import
block after Ready.

## Provider bug catalogue (svalabs/forgejo, hit 2026-07-11)

Two distinct bugs in `svalabs/forgejo` surfaced provisioning one mirror repo:

1. **Synchronous migrate + no rollback → orphan on timeout** (the wedge above). `Create` runs
   `MigrateRepo`, which blocks until the GitHub clone finishes (measured: 3.5 s for a KB repo,
   77.8 s for a hundreds-of-MB repo). If a path timeout fires before it returns, the client
   errors while Forgejo keeps the repo — `resp.State.Set` (line 1368) never runs, no rollback,
   no adopt-on-conflict → permanent "already exists". Report upstream: creation of large mirror
   repos should be async/resumable or at least idempotent on re-apply.

2. **`mirror_interval` cannot be expressed** — the schema validator requires the regex
   `^(0|[1-9][0-9]*)h[1-5]?[0-9]m[1-5]?[0-9]s$` (so `0h10m0s` validates, `10m0s` does NOT), but
   `Apply` normalizes the stored value to Go-duration short form (`10m0s`) and Terraform core
   then rejects the mismatch: `Provider produced inconsistent result: was "0h10m0s", now
"10m0s"`. So **no literal survives both phases** — the only value the validator accepts is
   exactly the one the apply-time normalizer rejects. The repo API even round-trips it as `''`.
   Workaround (used here): omit the attribute entirely — it is `Optional + Computed`, so the
   config stops fighting the normalizer and the interval floats to the server value. Cost: the
   desired 10-min cadence can't be declared in TF; set it out-of-band in Forgejo if the default
   is too slow for haku-ci freshness. Report upstream: validator and apply-normalizer must agree
   on one canonical form.
