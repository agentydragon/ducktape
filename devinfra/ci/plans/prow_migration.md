# Forge choice, portable rollout gates, and conditional Prow migration

Planning baseline: 2026-09-09. This is proposed work, not a deployed system.
Tracks the rollout-dependency requirement in
[issue #4339](https://github.com/agentydragon/ducktape/issues/4339), still open at
planning time. Remove completed work from this plan as it lands; move implemented
contracts into component SPEC/README documentation.

## Intended outcome

Hold Prow adoption while evaluating a move toward GitLab, Forgejo, or another
forge. Choose the forge before committing to new CI orchestration. Prow remains
a conditional option if GitHub stays central and its benefits justify the coupling;
building a Prow provider for another forge is not the default direction.

Deliver deployment dependency gates independently of that choice, retaining
BuildBuddy RBE and remote caching. Keep functioning CI and publishing at each step.
Keep independently reviewable PRs independently creatable: dependencies
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

### Forge compatibility

Upstream source checked on 2026-09-09 at
[`104d452f4fce`](https://github.com/kubernetes-sigs/prow/tree/104d452f4fced027c5a357b6fd6fe860a1b6064f).
The conditional Prow route assumes GitHub remains the PR host. Hosting Prow
ourselves does not by itself make PR automation portable to another forge.

| Forge                      | Upstream integration and consequence                                                                                             |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| GitHub / GitHub Enterprise | Native integration; prerequisite for the conditional Prow route below.                                                           |
| Gerrit                     | Has a dedicated adapter; current Tide source also supports Gerrit. This does not imply compatibility with other forges.          |
| Forgejo / Gitea            | No native integration found in the reviewed upstream source/docs. Plan on custom integration rather than a configuration switch. |
| GitLab                     | No native integration found in the reviewed upstream source/docs. Plan on custom integration rather than a configuration switch. |

ProwJob refs support an explicit `clone_uri`, so executing a job against a
Forgejo/GitLab Git repository is possible in principle. That is separate from
native PR/MR webhooks, fork authorization, commands, statuses, reviews, and merge
automation. An adapter could submit ProwJobs and report their results, but Tide
would still require a new provider or a separate merge controller for those forges.
GitHub-compatible-looking webhooks alone do not supply these contracts.

Keep deployment evidence evaluation independent of the forge: immutable revision
and target policy in, timestamped rollout/health evidence out. Put PR/MR identity,
declaration authorization, status publication, and merge enforcement behind explicit
forge adapters. For cross-forge prerequisites, include host and repository identity
instead of interpreting a bare PR number globally.

Prefer the chosen forge's native CI and merge controls plus the shared rollout
gate as the comparison baseline. Do not assume their exact enforcement features
or edition availability: validate those during forge selection. Existing Forgejo
CI remains unchanged until a separately scoped migration is chosen. Supporting another forge
requires its own acceptance path from event through status to enforced merge;
cloning a repository or mirroring it to GitHub is not proof of that support.

Evidence: [Tide's supported providers](https://github.com/kubernetes-sigs/prow/blob/104d452f4fced027c5a357b6fd6fe860a1b6064f/cmd/tide/main.go),
[ProwJob clone URI](https://github.com/kubernetes-sigs/prow/blob/104d452f4fced027c5a357b6fd6fe860a1b6064f/pkg/apis/prowjobs/v1/types.go),
and [Gerrit adapter documentation](https://docs.prow.k8s.io/docs/components/optional/gerrit/).

### How much code would a Prow forge integration take?

Planning estimates, not measured implementation sizes. Units are thousands of
handwritten implementation lines added or materially changed, excluding generated
SDKs, vendored code, configuration, and tests. Ranges are subjective 90% planning
intervals, using a component breakdown and existing adapter sizes; they are not
statistically fitted confidence intervals or delivery-time promises.

| Scope, for one new forge               | Central estimate | 90% range | What it buys                                                                                                                                                             |
| -------------------------------------- | ---------------: | --------: | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Minimal external bridge                |           2 kLOC |  1–4 kLOC | A small event subset creates ProwJobs, supplies clone refs, and reports results. No Tide, broad ChatOps, or full recovery semantics.                                     |
| Operable CI adapter                    |           6 kLOC | 3–12 kLOC | Webhooks plus resync, credentials, fork trust, changed-head handling, cancellation/retest, status reporting, and configuration. Still no complete Prow merge experience. |
| CI plus selected Tide/ChatOps features |          15 kLOC | 8–35 kLOC | Adds a forge provider for merge eligibility, reviews, statuses, merge operations, and selected commands/UI assumptions. Not every Prow plugin.                           |

For the 6 kLOC case, the central breakdown is roughly 1k client/auth/model mapping,
1.5k event normalization and reconciliation, 1.5k job/ref/trust handling, 1k result
reporting/retries, and 1k configuration/operational integration. A shared bridge
would reduce the second forge's marginal work, but API and authorization semantics
still need separate implementations. Do not assume GitLab and Forgejo are equal
effort or that Gitea compatibility proves Forgejo correctness.

Budget tests/fixtures separately: plausibly another 1–2 times implementation LOC.
For scale, at the pinned upstream Prow revision above, five Gerrit implementation
files total 2,788 physical lines: adapter 858, trigger 112, client 850, source 116,
and reporter 852. Just adapter/client tests total 4,676 lines. These counts include
comments/blanks, omit other integration code, and are a reference-class anchor,
not a measurement of how much a GitLab/Forgejo port would reuse.
[Adapter source](https://github.com/kubernetes-sigs/prow/tree/104d452f4fced027c5a357b6fd6fe860a1b6064f/pkg/gerrit),
[reporter source](https://github.com/kubernetes-sigs/prow/tree/104d452f4fced027c5a357b6fd6fe860a1b6064f/pkg/crier/reporters/gerrit).

The largest uncertainty is how much GitHub-specific behavior must be abstracted
across Tide, hook plugins, configuration, and Deck. Upstream review/maintenance can
dominate effort even if the diff is small. Full plugin parity is outside these
estimates. None of the rows includes our Flux/artifact/health observer: that work
is needed whichever CI orchestrator is selected. A bridge is feasible; recreating
the complete integration is a subsystem project, not a few hundred lines of YAML.

### Is anyone already working on this?

Checked public upstream issues, source, and related projects on 2026-09-09:

- Prow's [GitLab request #105](https://github.com/kubernetes-sigs/prow/issues/105)
  opened in April 2024 and closed through stale-issue automation in September 2024. This is not a technical rejection or an implementation.
- The older [multi-provider proposal #10146](https://github.com/kubernetes/test-infra/issues/10146)
  records abstraction proposals and points to Lighthouse. Maintainer comments
  describe substantial GitHub coupling and limited review bandwidth; the thread
  closed in May 2024 with future discussion directed to the new Prow repository.
- Searches of the current Prow repository found no open issue/PR matching GitLab,
  Forgejo, or Gitea integration. This bounds what was found publicly; it does not
  establish that nobody is working privately or in an unindexed fork.
- **Lighthouse is the concrete existing alternative to writing this port.** Its
  [README](https://github.com/jenkins-x/lighthouse) describes its Prow origin,
  multi-forge `go-scm` layer, GitLab support, Gitea configuration, and execution
  through Tekton/Jenkins/JayeX. It uses LighthouseJob rather than ProwJob. Its
  repository is not archived; September 2026 commits update dependencies, and
  July 2026 commits fix repo-owner and Tekton rerun behavior. That demonstrates
  maintenance, not verified GitLab/Forgejo production acceptance.
  [Commit history](https://github.com/jenkins-x/lighthouse/commits/HEAD/).
- Lighthouse's [SCM drivers](https://github.com/jenkins-x/go-scm/tree/HEAD/scm/driver)
  include GitLab and Gitea, but no distinct Forgejo driver was found. Treat Forgejo
  compatibility, current merge-controller behavior, external-status enforcement,
  and standalone deployment burden as proof-of-concept questions. Do not present
  it as an already verified drop-in multi-forge Tide replacement.

### Alternatives for “B may merge after A is healthy in Kubernetes”

The requirement has three parts: identify A's merged revision, observe its actual
deployment and health, and enforce a fresh result on B's merge. No reviewed product
was verified to provide our complete Flux/provenance/health predicate out of the
box. Several provide useful enforcement and scheduling; all still need a job or
observer implementing the deployment evidence contract below.

| Candidate                                 | Existing capability                                                                                                                  | What our gate must add / limitation                                                                                                                                                                                                                                                                      |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Forgejo Actions or existing CI + observer | Forge-native execution and branch protection; smallest migration for repos already on Forgejo.                                       | Report a required rollout status and verify its exact enforcement on the deployed Forgejo version, including bypasses, missing statuses, and stale results.                                                                                                                                              |
| Woodpecker + observer                     | Documented GitHub, GitLab, Gitea, and Forgejo PR/push support; credible shared CI option across the contemplated forges.             | The forge enforces merge protection; Woodpecker does not inherently model another PR's production rollout. Keep long waits in a reconciling observer.                                                                                                                                                    |
| GitLab CI + observer                      | Native MR pipelines and pipeline-success merge controls; external commit statuses are available in Free.                             | Associate the gate with the correct MR pipeline, create its blocking state before a pipeline can be accepted, and reconcile retries/new heads. Dedicated external MR status checks are Ultimate, not the Free commit-status API.                                                                         |
| Zuul                                      | Purpose-built project gating, cross-project change dependencies, speculative testing, and a native GitLab driver that can merge MRs. | Closest alternative for rich dependency orchestration. `Depends-On` and speculative tests do not prove A has merged and deployed. Add post-merge rollout observation and delay B's eligibility; avoid making a pre-merge job wait for its own merge. No Forgejo/Gitea driver is listed in reviewed docs. |
| Lighthouse + Tekton/Jenkins/JayeX         | Prow-style commands with a multi-forge SCM layer, including GitLab.                                                                  | Evaluate its current merge enforcement and Forgejo compatibility first; custom rollout evidence remains necessary. Useful if Prow-style interaction is a strong requirement.                                                                                                                             |

Capability sources:

- [Forgejo branch protection](https://forgejo.org/docs/latest/user/repository/protection/)
  and [Actions guide](https://forgejo.org/docs/latest/user/actions/).
  These are platform entry points; the specific protected-status configuration
  still needs an end-to-end test on our chosen version.
- [Woodpecker forge feature matrix](https://woodpecker-ci.org/docs/administration/configuration/forges/overview).
- [GitLab external commit statuses](https://docs.gitlab.com/ci/ci_cd_for_external_repos/external_commit_statuses/),
  [pipeline merge controls](https://docs.gitlab.com/ci/jobs/job_control/), and
  [Ultimate external status checks](https://docs.gitlab.com/user/project/merge_requests/status_checks/).
  The dedicated external checks are non-blocking by default until the project
  enables “Status checks must succeed.” Commit statuses can attach to an unexpected
  pipeline when duplicates exist; pin pipeline identity and verify merge behavior.
- [Zuul gating](https://zuul-ci.org/docs/zuul/latest/gating.html),
  [GitLab driver](https://zuul-ci.org/docs/zuul/latest/drivers/gitlab.html),
  [driver inventory](https://zuul-ci.org/docs/zuul/latest/drivers/index.html), and
  [pipeline managers](https://zuul-ci.org/docs/zuul/latest/config/pipeline.html).

Working shortlist: native CI plus the observer for minimum change; Woodpecker if
one CI needs to serve GitLab and Forgejo; Zuul if GitLab and sophisticated change
gating are central; Lighthouse if Prow-style ChatOps is important enough to justify
an integration trial. These are recommendations inferred from documented surfaces,
not deployed comparisons. Prefer free/self-hosted paths; do not make GitLab
Ultimate a hidden prerequisite.

The trial should use the same two PRs/MRs on each serious candidate: B stays
blocked while A is unmerged, published-but-not-applied, or running unhealthy;
unblocks only after the named workloads pass; and blocks again on rollback/stale
evidence under `currently-healthy`. Also test a new B head, declaration edits,
observer outage, and direct merge attempts. A successful pipeline demo alone
does not settle which system meets this requirement.

### Lighthouse follow-up: credible candidate, with concrete acceptance gaps

Source inspection on 2026-09-09 materially strengthens Lighthouse's candidacy.
It already has **Keeper**, a Tide-derived merge controller with a non-GitHub REST
path and required-status evaluation. We would not be starting a multi-forge merge
controller from scratch. Recommend a bounded trial if ChatOps and automatic merging
are important, before considering a new Prow provider.

Evidence scope: static inspection of Lighthouse
[`729cd0891313`](https://github.com/jenkins-x/lighthouse/tree/729cd0891313eb03c36e768c221fc2caaf912d0e)
and its pinned `go-scm` v1.16.0
[`e912546987d8`](https://github.com/jenkins-x/go-scm/tree/e912546987d80e311da8c8b702c2cbeceb7527f0).
No Lighthouse deployment, test suite, or real GitLab/Forgejo merge was exercised.

#### Merge gating is implemented beyond GitHub

- Keeper selects a REST search when GraphQL is unavailable. That path enumerates
  explicit repositories and filters PRs/MRs by required/forbidden labels and target
  branches. Configure `repos`, not organization-only queries, for this trial.
- It fetches the head commit's combined statuses through `go-scm` and rejects
  unsuccessful or missing required contexts. A `dependency-gate` context can
  therefore be an explicit requirement alongside CI results. This is a concrete
  source-level integration point for our observer, not proof of runtime correctness.
- The GitLab and Gitea drivers implement combined-status reads and merge calls.
  Keeper passes an expected head SHA through Lighthouse's SCM wrapper; the
  GitLab encoder forwards it to the merge API.
- The REST query path does **not** apply `reviewApprovedRequired`; that flag is
  translated into a GitHub search term elsewhere. Do not assume approval parity
  from shared configuration. Native forge approval enforcement or an independently
  trusted approval status must be verified before giving Keeper merge authority.

Sources: [Keeper selection and status checks](https://github.com/jenkins-x/lighthouse/blob/729cd0891313eb03c36e768c221fc2caaf912d0e/pkg/keeper/keeper.go),
[query configuration](https://github.com/jenkins-x/lighthouse/blob/729cd0891313eb03c36e768c221fc2caaf912d0e/pkg/config/keeper/query.go),
[merge wrapper](https://github.com/jenkins-x/lighthouse/blob/729cd0891313eb03c36e768c221fc2caaf912d0e/pkg/scmprovider/pull_requests.go),
[GitLab merge encoding](https://github.com/jenkins-x/go-scm/blob/e912546987d80e311da8c8b702c2cbeceb7527f0/scm/driver/gitlab/util.go).

#### Forgejo requires a targeted compatibility and race test

There is still no distinct Forgejo driver or verified Forgejo integration in the
reviewed tree. The initial trial would use Gitea mode against a disposable Forgejo
repository and check authentication, signatures/events, PR discovery, statuses,
permissions, and merge behavior on the actual version we intend to run.

One concrete source gap: the pinned Gitea driver's `Merge` copies merge style and
title but **does not forward `options.SHA`**. Thus the expected-head constraint
supplied by Keeper is lost at this boundary. Assess the server's supported
expected-head API and fix/test the mapping before automatic merging. This is not
a demonstrated unauthorized merge: server-side protection may reject it, but we
must test the new-commit race rather than rely on it accidentally doing so.
[Gitea merge implementation](https://github.com/jenkins-x/go-scm/blob/e912546987d80e311da8c8b702c2cbeceb7527f0/scm/driver/gitea/pr.go).

The open [GitLab approval-label report #1415](https://github.com/jenkins-x/lighthouse/issues/1415)
describes manually added `approved`/`lgtm` labels bypassing ChatOps authorization.
It is a 2022 report, not a reproduction on current GitLab. Nevertheless, mutable
labels must not be our sole evidence of approval or rollout health. In contrast,
the old [Gitea startup report #1394](https://github.com/jenkins-x/lighthouse/issues/1394)
shows a newline in the supplied authorization value; its open status alone is not
evidence that current Gitea support is broken.

#### Standalone installation and BuildBuddy fit

Full Jenkins X is not a documented prerequisite. Upstream has standalone Helm
installation guides for [Lighthouse with Tekton](https://github.com/jenkins-x/lighthouse/blob/729cd0891313eb03c36e768c221fc2caaf912d0e/docs/install_lighthouse_with_tekton.md)
and [Lighthouse with Jenkins](https://github.com/jenkins-x/lighthouse/blob/729cd0891313eb03c36e768c221fc2caaf912d0e/docs/install_lighthouse_with_jenkins.md).
The chart exposes engine switches; `jx` is enabled by default, while `tekton` and
`jenkins` default off. Explicitly select the intended engine. Controllers include
webhooks, Keeper, Foghorn status reporting, and the selected execution controller;
Lighthouse has its own job CRDs. Own configuration through Flux rather than also
enabling its config-updater against the same ConfigMaps.

For us, a Tekton job could launch the existing BuildBuddy workflow; it would not
replace RBE workers. That adds Tekton as an operational dependency. A smaller first
experiment is Keeper consuming existing CI and observer statuses without new
presubmits; verify whether this can run with execution engines disabled and what
CRDs/RBAC it still needs. This reduced deployment has not been demonstrated.

Do not paste installation examples unchanged: the Tekton guide still contains
PipelineResource examples, and Keeper's README/config guide retain old Tide/Prow
names and links. Current source is more informative than those copied descriptions.
Render the chosen released chart, check its CRDs/RBAC against our Kubernetes and
Tekton versions, and trial the exact pinned artifacts.
[Chart values](https://github.com/jenkins-x/lighthouse/blob/729cd0891313eb03c36e768c221fc2caaf912d0e/charts/lighthouse/values.yaml),
[Keeper role](https://github.com/jenkins-x/lighthouse/blob/729cd0891313eb03c36e768c221fc2caaf912d0e/charts/lighthouse/templates/keeper-role.yaml).

#### Maintenance and release evidence

Latest published GitHub release observed:
[v1.33.9, 2026-08-31](https://github.com/jenkins-x/lighthouse/releases/tag/v1.33.9).
It pins `go-scm` v1.15.38, whereas the inspected main branch pins v1.16.0; main is
four commits ahead, consisting of dependency updates and their merge commits.
Repeat driver acceptance against the artifact actually deployed, not just main.

There is recent substantive GitLab maintenance:
[PR #1689](https://github.com/jenkins-x/lighthouse/pull/1689), merged 2026-05-12,
resolves the target branch SHA when GitLab's MR listing omits it. The corresponding
[SCM change](https://github.com/jenkins-x/go-scm/commit/31f208e089e1)
avoids confusing the merge base with the target branch tip. These directly matter
to our source-revision contract. The repository also contains GitLab/Gitea BDD
scaffolding, but its presence does not establish recent end-to-end passes. Older
open integration issues and stale docs mean active maintenance should not be
equated with feature parity across every forge.

#### Bounded trial before selection

- [ ] Pin a released Lighthouse/chart/SCM combination and document differences
      from the source reviewed here. Validate rendered resources and ownership.
- [ ] Start in a disposable explicit repository with Keeper merge actions disabled
      or credentials unable to merge; observe missing/pending/failing/passing
      `dependency-gate` alongside existing CI.
- [ ] Verify GitLab or Forgejo native review/protected-status enforcement, including
      manually changed approval labels and who can publish the required context.
- [ ] For Forgejo, resolve the missing expected-head mapping and test a push between
      eligibility evaluation and merge. Test ordinary and fork PRs/MRs.
- [ ] Exercise observer outage, stale success, rollback, declaration edits, new
      heads, API failures, and restart. The custom observer owns health freshness;
      Keeper does not turn a lasting success status into expiring evidence.
- [ ] Then allow merges only in the disposable repository and demonstrate B blocked
      until A's exact revision is healthy in the named deployments. Keep direct
      merge protection effective too.
- [ ] Only if this passes, compare Keeper-plus-existing-CI with Lighthouse/Tekton
      launching BuildBuddy. Select Lighthouse based on verified benefit over the
      native-CI/observer baseline, not its Prow ancestry alone.

### Cluster integration

The rollout observer watches approved Flux/Kubernetes resources and periodically
reconciles, producing immutable-revision evidence without depending on Prow or a
forge API. A thin forge adapter resolves PR/MR declarations and provenance, requests
evaluation, and publishes a blocking status for the current head. The forge's
merge controls enforce it. Start with GitHub while Actions still runs CI; add
the selected forge's adapter without rewriting health evaluation.

Use Flux to own the observer and adapter manifests, pinned images, RBAC,
configuration, and secret references. Keep forge write credentials in adapters,
outside the read-only observer and PR test pods. Give branch protection one owner.
Keep an independent bootstrap/recovery path if the cluster hosting gates is broken.

Only if Prow is selected: GitHub sends webhooks to `hook`; the controller manager
launches BuildBuddy launcher pods; `crier` reports results; `deck` displays jobs;
`sinker` cleans up. Add `horologium` for periodics and Tide for merge automation.
The adapter may become an external plugin, but observation must remain independent
of GitHub events. Do not let config-updater or branchprotector compete with GitOps.
Trial a dedicated SeaweedFS bucket for S3 uploads/Deck reads, with scoped
credentials, retention, and visibility. Public webhooks must work independently
of dashboard login policy.

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
- Require the gate through the selected forge's verified merge controls; initially
  GitHub rulesets. If Tide is adopted, also require it in Tide's context policy.
  Bind publisher identity where supported. Existing test/review requirements remain
  required. Test direct merges and each enabled automated merge path.
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

### 1. Choose the forge before CI orchestration

- [ ] Compare GitLab, Forgejo, and remaining on GitHub for the actual repository
      workflow: reviews, fork trust, required external statuses, merge automation,
      private repos, artifacts/registry, API support, backup/recovery, and operation.
- [ ] Verify candidate edition/version capabilities and self-hosting requirements,
      including whether a rollout gate actually prevents direct and automated merges.
- [ ] Inventory current Actions/Forgejo jobs, credentials, artifact consumers, and
      repository dependencies. Identify what can retain BuildBuddy with a new launcher.
- [ ] Record the forge decision and CI direction. Prefer native CI/merge controls
      plus the shared gate where they meet requirements. Select Prow only with an
      explicit justification for keeping GitHub central or owning another provider.

Exit: a forge and supported merge-enforcement path are chosen before replacing CI.
Existing CI remains active. Phase 2's observer and initial GitHub adapter can proceed
while this decision is open; they do not require Prow deployment.

### 2. Deliver portable dependency gates while existing CI runs

- [ ] Define the observer evidence interface independently of forge APIs, and keep
      declaration parsing, PR/MR lookup, authorization, and reporting in adapters.
- [ ] Implement the deployment dependency contract above through an initial GitHub
      adapter, preserving current Actions tests and reviews.
- [ ] After forge selection, prove the same gate through the selected forge's
      adapter and merge controls. Verify changed heads, body edits, observer outages,
      rollback, and direct/automated merge rejection on that forge.

The following gate acceptance work applies before either CI migration route:

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
      Verify actual merge rejection and recovery, including direct merges.

Exit: the rollout dependency works independently of Prow and the CI executor.
Rollback: restore the prior merge policy through its owner and explicitly hold PRs
with unmet dependencies; never fabricate success for an unavailable gate.

### 3. Migrate CI incrementally after forge selection

- [ ] For the chosen forge, trial one optional BuildBuddy-backed presubmit and
      compare exact tested trees, target selection, fork trust, statuses, retries,
      cancellation, logs, and artifacts with existing CI.
- [ ] Move required checks one at a time after equivalent behavior is demonstrated.
      Adapt invocation identities, PR visuals, and artifact consumers explicitly.
- [ ] Transfer each publisher separately, preserving provenance and concurrency;
      keep one active writer per artifact stream. Then move scheduled automation.
- [ ] Prove merge → publication → pin update → Flux → workload → health probe →
      dependent-PR gate on the chosen platform before retiring its old workflows.
- [ ] Preserve rollback and exercise independent recovery. Elaborate the chosen
      forge's implementation plan once phase 1 resolves its platform contracts.

## Conditional route: Prow if GitHub remains central

The following work is deferred unless phase 1 explicitly selects Prow. It refines
phase 3 for that choice; none of it is a prerequisite for the portable rollout gate.

### P1. Deploy Prow alongside existing CI

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

### P2. Enable Tide for an opt-in merge pool

- [ ] Configure explicit opt-in, review/hold rules, required Actions contexts, and
      the dependency gate. Resolve ownership against existing merge automation.
- [ ] Start with single-PR merges and prove dependency and test failures keep PRs
      outside the eligible pool. Verify stale evidence handling at merge time.
- [ ] Test against disposable PRs before enabling the production merge pool.

Exit: Tide merges only eligible opted-in PRs while Actions supplies build checks.
Rollback: disable Tide merging; retain required checks and manual review/merge.
Batch merging remains deferred until P3 supports its exact source trees.

### P3. Move presubmit execution to Prow

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

### P4. Move publishing and scheduled automation

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

### P5. Retire migrated Actions workflows

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
