# cluster/cdk8s plan

Burn-down for finishing the cdk8s conversion of `cluster/k8s`. Entries leave when their
work lands; the file goes when the last one does. Conventions the work runs under are in
<AGENTS.md> (§ The Flux graph), not here; the pool of not-yet-reachable SSOT candidates is
<TODO.md>.

State on 2026-09-20: all 250 Flux `Kustomization`s are generated from one chart
(#7391); by directory, 3 are fully generated, 23 mix generated and hand-written files,
260 are hand-written; about 36% of manifest lines under `cluster/k8s` are cdk8s output.

## The rule every wave runs under

Derive only from nodes already in Python. A stage that would build a value from a
hand-written file, or declare at a consumer what a provider should say, waits until its
neighbourhood is in. "1:1 first, abstract when the neighbourhood is in": literals may be
duplicated while the other side is still YAML; a shared type, registry, loader or
derived roster is not written until every node it would touch is a construct.

## Wave 1: derive what the Flux chart makes derivable

Independent of each other; fan out.

- **One-file root.** Emit the Flux chart as one `.k8s.yaml` and point the bootstrap
  root (`flux-system/gotk-sync.yaml`, `path: ./cluster/k8s`) at its directory. Deletes
  the 250 per-directory `flux-kustomization.yaml`, the `spec.path` special cases in the
  writer, and the artifact copy's `exclude_consumer`. `Testing.chart()` in
  `generate_manifests.py` becomes a real `App`/`Chart`. Whatever the root
  `kustomization.yaml` lists beyond Kustomizations stays hand-written beside it. Exit:
  every Kustomization object semantically identical to before;
  `cluster/validation` whole-graph tests unchanged and green.
- **Artifacts from the chart.** Each Kustomization node builds its artifact value first
  and reads `sourceRef` off it; the `ArtifactGenerator` is assembled from the list
  last. Deletes `_DUCKTAPE_ARTIFACTS`, the per-node `sourceRef` blocks and the
  triple-written names; retires `cluster/validation/test_actions_artifact.py`. The SOPS
  `decryption` block becomes one value. Exit: `kustomize build` of each packaged
  directory unchanged, checked once in the PR.
- **Kustomization nodes take values, not charts.** `agentplane_staging` takes
  `health_checks=...` instead of `resource_chart: Chart`; `haku_console` splits into
  `console_chart(app)` plus a node taking values (AGENTS.md § The Flux graph).
- **Fleet rules without rosters.** Remove `provided_secrets`,
  `provided_config_maps` and `providers` from `add_fleet_rules` and its 14 call
  sites; keep pod hardening, pinned egress and in-chart reference resolution. The
  `DEPENDS_ON` string tuples that only fed the rosters go with them.

**Pause after Wave 1.** Look at the Flux layer as one thing before building on it:

- Node signatures. `agentplane_staging` takes 17 `Kustomization` parameters. Decide
  whether that is acceptable as is, wants keyword-only parameters, or reveals that
  some dependencies are not the node's own (a dependency inherited from a chart's
  needs, say). A record type for "dependencies" is not one of the options.
- Repetition. Re-measure lines per node after the artifact step. The remaining
  repetition should be operational fields (`interval`, `prune`, `wait`, ...) and
  literal `healthChecks` for hand-written directories; if anything else repeats,
  name it before adding a helper.
- Whether `cluster/validation/test_dependencies.py`'s cycle check is now
  unwritable in Python (every `dependsOn` a parameter) and can retire, or still
  guards the two literal edges.

## Wave 2: split the trees

One PR, after Wave 1's first two entries, because each half is broken alone.

- cdk8s output moves to `cluster/generated/k8s/<same path>`; `cluster/k8s` holds
  hand-written files only. Each artifact gets a second copy op from the generated
  tree into the same `@artifact/cluster/k8s/<path>/`, so no Kustomization changes.
  `.gitattributes` collapses to one glob. A test helper overlays the two trees the way
  the artifact does, for `test_flux_build` and `test_cluster_integration`. Exit:
  artifact contents byte-identical per Kustomization before and after the move.

**Pause after Wave 2.** With `cluster/k8s` showing only what is still hand-written:

- Re-read the remainder by directory. Decide what stays hand-written permanently
  (HelmRelease-plus-values directories, vendored `flux-system`, `parked/`) so the
  burn-down has a floor, and record that floor here.
- Local ergonomics: is `kustomize build` through the overlay helper acceptable, or does
  the mixed-directory workflow need a small `bb run` target?
- Whether generated output should stay committed. The alternative is a CI-pushed
  `OCIRepository` as the second artifact source, with CI in the deploy path and PR
  review losing the rendered diff. Default: stay committed; revisit only with a
  concrete cost.

## Wave 3: skip Kustomize where nothing is kustomized

- Verify kustomize-controller's generated-`kustomization.yaml` rule on one directory
  (which files it includes, how it treats subdirectories such as `image-pins/`), then
  remove `kustomization.yaml` from every directory with no `components`,
  `configMapGenerator` or ordering-sensitive hand-written siblings. The PR's report
  names every directory that still needs one and why.

**Pause after Wave 3.** Image pinning. The 7 `image-pins/` Components exist because
Flux image automation commits tags into a file the generator would otherwise own.
Options: keep the Component and a `kustomization.yaml` in those directories
indefinitely; or tags move into Python with CI regenerating after the bot commits.
Decide from the count Wave 3 reports, not before.

## Wave 4: close the graph

Roster-driven, parallel, each PR joining the graph the AGENTS.md way (chart, then
node taking values, then dependents).

- **Namespace Kustomizations.** Every `*-namespace` directory (a Namespace, at most an
  ExternalSecret). Turns the last string edge (`ssh-mcp -> ssh-mcp-namespace`) and every
  namespace `healthChecks` entry into derived values.
- **Half-converted workload directories**, one PR each: `agents/mitmproxy`,
  `agents/haku-egress-proxy` (one `IronProxy` construct for its two iron deployments and
  `public-coder-agent/proxy`), `agents/haku-openclaw-spike/app`,
  `agents/public-coder-agent/app`.
- **The public-coder-agent constellation** (`proxy`, `devbox`, `sshpiper`, `backup`)
  with `agent-rbac-base`; retires `test_haku_public_coder_contract.py` and
  `test_public_coder_clickhouse_reader_contract.py`.
- **The `haku/` tree** (`mailbox`, `workspaces`, `haku-ci`, `rbac`, `forgejo-tea`,
  `ui-image-webhook`) with `haku/runtime/agent/config.py`'s Settings as the sandbox
  template's contract; retires the haku sandbox, mailbox and KEDA contract tests.
- **`agentplane-index` and `aiquota-api`** rendered from their Settings.
- Then the rest of <TODO.md>, in whatever order the neighbourhoods complete.

**Pause during Wave 4, after the namespaces and two workload directories.** The
resource-chart join has only `health_checks` crossing today. If a second fact has had
to cross (a Secret name a Kustomization must wait for, a ConfigMap generator input),
look at what it is before it becomes a pattern by accident: a second value parameter
is the expected answer; anything that wants the chart back is the signal to stop.

## Wave 5: rules that need the whole tree

Not before Wave 4 has most providers as constructs; before that they would be
declarations again.

- **Tree-wide reference resolution.** Every Secret/ConfigMap a Pod reads resolves to an
  object in the tree with the right namespace, whose Kustomization is the reader's own
  or in its `dependsOn` closure. Providers declare at the source: SOPS files by their
  plaintext `name`/`namespace`, reflector targets from the source Secret's annotation,
  Terraform-minted secrets on the `gitops_terraform` CR construct. Replaces the rosters
  Wave 1 removed, with no consumer-side declaration.
- **Whole-graph validation tests moving to synth**, where the graph makes them
  unrepresentable rather than merely checked (`test_dependencies`, `test_health_checks`,
  `test_generator_namespace`).

## Done

`cluster/k8s` holds only the floor recorded after Wave 2, every Kustomization node's
inputs are values or constructs, no `cluster/validation` test spans a generated ↔
hand-written seam, and this file is deleted.
