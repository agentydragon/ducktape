# haku-ui image webhook (operator-owned)

The **operator-owned** half of haku-ui's image automation: a generic Flux `Receiver` that
Forgejo `package` (image publish) and `push` (git push) webhooks — both provisioned by
`tf/gitops/haku-state` — hit, so Flux reconciles the whole deploy chain immediately instead
of stacking 5m polls: ImageRepository scan, ImageUpdateAutomation tag-bump commit, and the
GitRepository fetch that feeds the `haku-state-workloads` apply.

**Why this isn't in Haku's haku-state** (where the rest of the automation lives —
its `k8s/haku-ui-image-automation/`): a `Receiver` can force-reconcile any
Flux resource cluster-wide (the notification-controller runs `--watch-all-namespaces` with no
`--no-cross-namespace-refs` guard). That's a cross-namespace primitive the constrained
`haku-state-reconciler` SA is designed to deny, so the Receiver stays operator-owned and just
references Haku's `haku-sandbox:haku-ui` ImageRepository. The bounded image/source CRDs are
Haku's.

Generic receiver (not gitea/generic-hmac): Flux has no Forgejo receiver type and Forgejo's
HMAC headers don't match generic-hmac; security is the unguessable `sha256(token)` path. If
the webhook never fires (Forgejo package webhooks are owner-scoped — a repo webhook may not
fire for `haku/ui`), the 5m ImageRepository poll is the safety net.

## TODO: consider moving the Receiver into Haku too

The only reason this is operator-owned is the cross-namespace force-reconcile primitive. If
the notification-controller ran with **`--no-cross-namespace-refs`** (cluster-wide), a
Haku-authored Receiver would be bounded to `haku-sandbox` resources — defanging the primitive
— and the Receiver could move into haku-state alongside the rest of the
automation, making the whole pipeline Haku-owned. That flag is a cluster-wide change (audit
existing cross-namespace Receivers first, e.g. the `github` one), so it's parked here as a
follow-up rather than done now.
