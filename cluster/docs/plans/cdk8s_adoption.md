# Plan: cdk8s adoption for cluster manifests

**Status**: phase 1 landed. `cluster/k8s/litellm/app` is fully cdk8s-generated and
committed. How the current system works (conventions, mechanisms, constraints) is
documented in <../cdk8s.md>, not here — this file holds only what's still undecided
or unbuilt.

## Where to convert next

Not scheduled yet — litellm/app should run in production for a while first — but
recorded so the next candidate is picked by criteria, not arbitrarily:

- **No `dependsOn` rationale comments to lose is a green light, not a precondition.**
  Check with `grep -c '#' <dir>/flux-kustomization.yaml` before converting; most of
  the fleet's 278 Kustomizations have zero. A directory whose comments _aren't_
  droppable this way still needs the manifest-visible-rationale mechanism from the
  open questions below before it converts cleanly.
- **Live image automation is no longer a reason to defer.** The `image-pins/`
  Component pattern (<../cdk8s.md>) is verified end-to-end, and it's not a rare
  shape to plan around: 44 of the fleet's 278 Kustomization directories currently
  carry an `$imagepolicy` marker somewhere
  (`grep -rl '\$imagepolicy' cluster/k8s --include='*.yaml'`). A second real
  instance is still worth picking deliberately — to confirm the pattern holds for a
  directory with more than one `$imagepolicy`-carrying resource, which litellm/app's
  single-image case doesn't exercise — but it no longer blocks conversion.
- **Prefer small, boilerplate-heavy directories over large, bespoke ones.** Every
  `flux-kustomization.yaml` hand-repeats the same constant fields; a directory with
  one Deployment/Service, or a namespace-only Kustomization, converts with the least
  risk and starts amortizing `flux_constructs.py`'s shared helpers immediately.
- **A directory whose config already lives in Python is a natural fit.** litellm/app
  had `ProxySpec` and the model rosters driving its ConfigMap by hand already; cdk8s
  let the Deployment/Service render that same model directly instead of staying
  hand-synced to it.
- **Still one Kustomization at a time.** These criteria pick which directory goes
  next, not license to batch several conversions into one change.

## Explicitly deferred (not decided, not scheduled)

Recorded so they don't get re-litigated as if phase 1 requires deciding them now:

- Generating the _entire_ cluster tree with cdk8s.
- Moving Flux's authoritative manifests into their own branch (`cluster-manifests` or
  similar), so generated output stops living on `devel`.
- Building the authoritative manifests in CI instead of committing regenerated output on
  every generator change.

Any of these would change where the pinning test in phase 1 points (from "the committed
file in `cluster/k8s`" to something built later), so they're easier to evaluate once
phase 1's actual pattern has run for a while, not before.

## Open questions

- How to carry a `dependsOn` rationale comment through generation when one is
  genuinely needed _in the manifest itself_ (cdk8s can't emit YAML comments — see
  <../cdk8s.md>). No case has needed this yet; when one does, a `reason` kwarg the
  shared construct renders as something else is the likely shape, since `dependsOn`
  list items have no comment-equivalent field.
- `//cluster:generate_manifests` hardcodes its one output directory (no
  `--output-dir` flag) — matches phase 1's "write directly to `cluster/k8s`", but isn't
  yet a general per-directory regeneration entrypoint. Revisit when a second directory
  converts and the hardcoding actually needs generalizing, rather than guessing the
  right shape from one data point.
- Whether `image-pins/`'s content should itself get any generator involvement (e.g.
  a test that the `name:` in `images:` matches `ProxySpec.image_name`, so the two
  can't drift apart silently) — not built, low urgency while there's only one
  instance to keep in sync by eye.
