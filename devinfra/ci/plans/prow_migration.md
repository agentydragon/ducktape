# Gradual migration to Prow

Planning baseline: 2026-09-09. This is proposed work, not a deployed system.
Tracks the rollout-dependency requirement in
[issue #4339](https://github.com/agentydragon/ducktape/issues/4339), still open at
planning time. Remove completed work from this plan as it lands; move implemented
contracts into component SPEC/README documentation.

## Intended outcome

Host CI orchestration and PR coordination in the cluster, retaining BuildBuddy RBE
and remote caching. Migrate incrementally with functioning CI and publishing at
each step. Keep independently reviewable PRs independently creatable: dependencies
constrain merging, not when agents may start work or open PRs.

A first-class use case is: **PR B may merge only after PR A's changes have reached
specified Flux-managed deployments and are running successfully.** A merged PR,
published image, selected ImagePolicy, or Ready Flux object alone is insufficient.
Dependencies can also name an unrelated commit or immutable image digest.

## Current integration points

- `.github/workflows/bazel-ci.yml` launches `devinfra/ci/bazel_ci.sh` through
  `.github/actions/bb-remote`; execution already happens on BuildBuddy.
- PR builds verify GitHub's synthetic merge commit and use its parents for
  affected-target selection. The remote runner fetches that exact commit.
- `trusted-fork-pr-ci.yml` trusts the owner/agent authors and gates other fork
  authors through a protected GitHub Environment.
- `ci.yml` passes invocation IDs into release/image planners. Superseded builds
  may be cancelled; running publishes are allowed to finish.
- `devinfra/ci/invocation_ids.py` and PR visual discovery use Actions run identity.
- Other workflows cover pre-commit, Gazelle, Nix, ansible-lint, image builds,
  publication, and scheduled maintenance.

Recheck these interfaces and actual required checks when implementing each phase.
No live cluster capacity or ruleset audit has been performed for this plan.

## Architecture and ownership

GitHub sends webhooks to Prow `hook`. The controller manager launches CI pods;
`crier` reports results; `deck` displays jobs; `sinker` cleans up. Add `horologium`
for periodic jobs and Tide when merge automation is ready. CI pods initially
launch BuildBuddy jobs rather than running the build locally.

A separate dependency controller consumes GitHub events, watches approved
Flux/Kubernetes resources, and periodically reconciles. It publishes a
`dependency-gate` status on the waiting PR's current head. It can integrate as a
Prow external plugin, but must continue observing cluster changes without a new
GitHub event. Tide consumes the status; understanding Flux is custom work.

Use Flux to own Prow manifests, pinned images, RBAC, configuration, and secret
references. Choose one writer for Prow configuration and GitHub branch protection;
do not let config-updater or branchprotector compete with existing GitOps owners.
Keep GitHub write credentials in controllers, outside PR test pods. Separate
controller, test, and publisher permissions and restrict job network access.

Use a dedicated SeaweedFS bucket if Prow's S3 uploads and Deck reads pass an
integration trial. Scope credentials, retention, and visibility deliberately.
Public webhook delivery must work independently of any dashboard login policy.
Keep an independent bootstrap/recovery path if the cluster hosting CI is broken.

## Deployment dependency contract to implement

Finalize a small declarative interface before exposing commands. Illustrative
PR-body syntax, not an implemented parser or finalized schema:

```text
Depends-On: #1233
Wait-For-Rollout: pr=#1233 target=haku-production policy=currently-healthy
```

`haku-production` would be a reviewed target definition containing the cluster,
Flux resources, workloads, containers, required health probes, and observation
window. A target may cover several deployments; all required members must pass.
The PR declaration selects registered targets and policies, never arbitrary
commands, URLs, namespaces, credentials, or cluster queries.

### Evidence chain

For each dependency, resolve and retain evidence linking:

1. The prerequisite PR to its actual merged commit, accounting for squash/rebase;
   or the explicitly declared immutable source revision.
2. That source to the expected artifact digest(s), using trusted publication
   provenance. For configuration-only changes, identify the relevant GitOps
   revision and applied resource generation instead.
3. The artifact/configuration to the desired state in the deployment repository,
   including any separate image-pin automation commit.
4. The named Flux reconciliation to that desired state. Check observed generation,
   applied revision, and readiness; reject stale conditions and suspended targets.
5. The named workloads to the expected images/configuration: rollout complete,
   current generation observed, intended replicas ready, and old replicas retired
   where the target requires it. Handle registry index/platform digest mapping
   explicitly rather than assuming every image ID equals the index digest.
6. Target-specific runtime checks and a configured healthy observation window.
   Include an authenticated functional probe when readiness does not demonstrate
   the capability needed by the waiting PR; reset the window on relevant failure.

An advanced Flux revision or descendant source commit is not automatically proof:
the required content may have been reverted. Define exact-artifact versus accepted
successor semantics per target and retain the evidence supporting that decision.
The status details should identify the first unmet condition and link to evidence.

### Health semantics and enforcement

- Support distinct policies: `observed-healthy` records that the prerequisite
  completed a healthy rollout; `currently-healthy` requires fresh evidence that
  it remains deployed and healthy. Default this use case to `currently-healthy`.
  Historical success must never silently substitute for current health.
- Bind results to waiting PR head, declaration version, target-policy version,
  prerequisite revision, and evidence timestamps. Reevaluate on head/body edits,
  prerequisite merge/closure, target changes, rollout events, and periodic resync.
- No dependencies yields explicit success. Invalid, unknown, inaccessible,
  ambiguous, cyclic, or stale dependencies block with a reason. A prerequisite
  closed without merging does not satisfy a PR dependency.
- Define who may add, weaken, or remove a dependency; invalidate prior approval
  where needed. PR-body edits do not change the head SHA, so a previously green
  status cannot be treated as valid for an unseen declaration edit.
- Require the gate through GitHub rulesets and explicitly in Tide's context policy;
  bind its publisher identity where supported. Existing test/review requirements
  remain required. Test enforcement through both Tide and direct GitHub merges.
- GitHub success statuses do not expire by themselves. Before enforcing
  `currently-healthy`, implement and test stale-success handling during observer
  outages, including an independent watchdog or a merge path that checks freshness.
  Define the acceptable observation-to-merge interval and bypass policy. Ordinary
  asynchronous status updates cannot guarantee atomicity with a live cluster;
  do not claim they can eliminate rollback or declaration-edit races.
- Keep any emergency override explicit, authorized, and auditable. Status expiry,
  observer failure, and unknown data must not silently turn into success.

## Migration phases

Each slice is independently reviewable. Parallelize independent implementation;
only enforce a gate after its prerequisites have passed acceptance.

### 1. Deploy Prow alongside existing CI

- [ ] Inventory workflows, status contexts, tokens, artifacts, cancellation rules,
      publishing triggers, and integrations that discover GitHub Actions runs.
- [ ] Deploy the core services, CRD/RBAC, GitHub App/webhook, dashboard, storage,
      resource limits, metrics, alerts, and pinned upgrade configuration through Flux.
- [ ] Add configuration validation to existing Bazel CI. Keep privileged job specs
      in trusted configuration until PR-controlled job configuration is constrained.
- [ ] Run an optional canary with distinct status names. Demonstrate webhook
      handling, checkout, success/failure reporting, retest, log upload/read, controller
      restart recovery, and cleanup. Measure resource use before choosing capacity.

Exit: operators can diagnose a real canary failure from GitHub through Deck.
Rollback: disable canary triggers; Actions remains authoritative.

### 2. Deliver dependency gates while Actions still runs CI

- [ ] Define declaration authorization, parser, target registry, evidence schema,
      policy semantics, controller persistence/recovery, and narrow read-only RBAC.
- [ ] Implement PR-merged predicates and one real deployment target; support
      arbitrary immutable commit/image prerequisites through the same evidence model.
- [ ] Publish an optional gate first. Exercise pending, healthy, unhealthy,
      rollback, changed-head/body, changed policy, ambiguous provenance, inaccessible
      target, closed prerequisite, cycles, missed webhook, restart, and stale success.
- [ ] Demonstrate B blocked before A merges, after A merges but before rollout,
      and while the expected deployment fails health checks; unblock automatically
      only after every named target passes the configured policy.
- [ ] Resolve the freshness/merge race policy above, then make the gate required.
      Verify actual merge rejection and recovery, including direct GitHub merges.

Exit: the user's rollout dependency works with current Actions checks.
Rollback: restore the prior ruleset through its owner and explicitly hold PRs with
unmet dependencies; never replace an unavailable gate with fabricated success.

### 3. Enable Tide for an opt-in merge pool

- [ ] Configure explicit opt-in, review/hold rules, required Actions contexts, and
      the dependency gate. Resolve ownership against existing merge automation.
- [ ] Start with single-PR merges and prove dependency and test failures keep PRs
      outside the eligible pool. Verify stale evidence handling at merge time.
- [ ] Test against disposable PRs before enabling the production merge pool.

Exit: Tide merges only eligible opted-in PRs while Actions supplies build checks.
Rollback: disable Tide merging; retain required checks and manual review/merge.
Batch merging remains deferred until phase 4 supports its exact source trees.

### 4. Move presubmit execution to Prow

- [ ] Extract a shared launcher from the Actions wrapper, using a pinned image
      with Nix-provided tooling. Retain BuildBuddy RBE and cache configuration.
- [ ] Map Prow execution identity to distinct deterministic invocation IDs and
      linkage records, including retries, and adapt PR visuals/artifact discovery.
- [ ] Preserve fork trust, synthetic-merge parent checks, affected-target
      selection, cancellation, timeout, and status-reporting behavior.
- [ ] Resolve source transport explicitly: Prow-created local merge SHAs may be
      unfetchable by `bb remote --run_from_commit`. Initially resolve/verify GitHub's
      merge ref, or implement proven source transport. Do not silently test PR head.
- [ ] Run optional parallel Bazel presubmits; compare tested trees, selected
      targets, failures, artifacts, and cancellation on representative changes.
- [ ] Switch required contexts after equivalence is demonstrated. Migrate
      pre-commit, Gazelle, ansible-lint, and Nix checks in separate slices.
- [ ] If enabling Tide batches, implement/test transport and target selection for
      multiple PRs; the current two-parent single-PR assumptions are insufficient.

Exit: required presubmits run under Prow with equivalent evidence and diagnostics.
Rollback: restore Actions triggers/required contexts before disabling Prow checks.

### 5. Move publishing and scheduled automation

- [ ] Define the postsubmit build-to-publish handoff explicitly; Prow job types
      do not directly translate the Actions `needs` graph and dynamic matrices.
- [ ] Preserve invocation provenance, per-target publish decisions, missing-proof
      behavior, idempotent retries, registry/release credentials, and cancellation
      semantics. Ensure older concurrent runs cannot regress mutable published state.
- [ ] Compare publication plans without writes, then transfer one publisher at a
      time. Keep exactly one active publisher for each artifact stream.
- [ ] Move remaining standalone image builds and scheduled tasks independently.
- [ ] Verify a real merge through build, publication, pin update, Flux application,
      workload rollout, runtime probe, and dependent-PR gate transition.

Exit: migrated artifact streams reach working deployments through Prow-managed jobs.
Rollback: stop the new publisher before restoring its Actions counterpart.

### 6. Retire migrated Actions workflows

- [ ] Remove replaced workflows only after their checks, artifacts, visual reports,
      release consumers, and operational tooling use the new path.
- [ ] Remove unused credentials and runners; document upgrades, retention, failure
      recovery, freshness guarantees, and operator overrides in durable component docs.
- [ ] Keep and exercise the independent recovery path; demonstrate recovery when
      the hosted CI services or dependency observer are unavailable.

## Upstream references

- [Deploying Prow](https://docs.prow.k8s.io/docs/getting-started-deploy/)
- [Components](https://docs.prow.k8s.io/docs/components/)
- [ProwJobs](https://docs.prow.k8s.io/docs/jobs/)
- [External plugins](https://docs.prow.k8s.io/docs/components/plugins/)
- [Tide context policy](https://docs.prow.k8s.io/docs/components/core/tide/config/)

These describe upstream integration points. The deployment evidence controller,
health policies, source-transport adaptation, and migration acceptance above are
proposed Ducktape work, not claims of native Prow functionality.
