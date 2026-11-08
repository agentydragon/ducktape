# Plan and delegate Debundle work

Use the [task chooser](../../README.md#choose-a-task) for individual authoring
operations. This guide owns the spec/tool improvement loop and shared planning,
worker, and integration procedures; project adapters supply concrete paths,
commands, conventions, and gates.

| Task                                | Read                                                                                   | Result                                                        |
| ----------------------------------- | -------------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| Plan an assignment without editing  | [Plan an assignment](../../docs/planning.md#plan-an-assignment)                        | Source-backed proposal, decomposition, or selector worklist   |
| Ground residual proposals           | [Prepare seeds](../../docs/planning.md#prepare-seeds)                                  | Named worker packets in `seeds.json`                          |
| Apply one assigned edit             | [Execute a lane](../../docs/spec_editing.md#workflow-executing-a-delegated-assignment) | Reviewed YAML delta and completed validation evidence         |
| Combine worker results              | [Integrate worker results](../../docs/integration.md)                                  | Validated combined spec and per-branch disposition            |
| Improve specs and the tool together | [Drive specs and tool improvements](#drive-specs-and-tool-improvements)                | Prioritized canonical capability and justified solution route |
| Coordinate a round                  | [Coordinate a round](#coordinate-a-round)                                              | Isolated assignments, current evidence, and routed follow-ups |

## Roles

- `debundle_plan_work`: read-only source/graph planning.
- `debundle_intake`: grounds residual proposals and names seed destinations.
- `debundle_lane_worker`: implements one scoped assignment.
- `debundle_integrator`: combines worker results in isolated integration state.
- `debundle_orchestrator`: owns adapter inputs, dispatch, and progress tracking.
- `debundle_architect`: [architecture review](../../docs/architecture_review.md)
  and worker-ready reorganization recommendations; no spec edits.
- `debundle_mint_names`: [binding naming](../../docs/naming.md) only.
- `debundle_stabilize`: [selector review](../../docs/selectors.md#review-one-selector)
  without renaming or moving modules.
- `debundle_explain`: [trace behavior](../../docs/cli.md#explain-behavior-with-a-partial-spec)
  and close only the coverage gaps needed for the question.

## Adapter Contract

The CLI guide's ["Delegating with an input
snapshot"](../../docs/cli.md#delegating-with-an-input-snapshot) and ["Resolved
invocation records"](../../docs/cli.md#resolved-invocation-records) sections define
command semantics. This contract defines what the adapter and each role must
exchange.

Before starting a round, the project adapter should identify:

- debundle target and explicit graph-refresh command (`run --graphs` or the
  Bazel `dependency_graphs` output group; normal build/regen outputs omit graphs)
- modules directory, patch stream if any, emitted JS root, and source root
- dependency graph, root/chunk reports, directory reports, and cycle report
  locations
- gate, regen, uniqueness-check, and smoke-test commands
- residual-only baseline config or reproducible derivation, and its normal gate command
- project convention docs and taxonomy docs
- architecture notes and module reorg paths
- worktree policy, base branch, commit/push policy, and scratch paths

For each snapshot-backed lane worker, also supply:

- a unique snapshot destination and the starting `snapshot.json`, with a
  successful `snapshot verify` and the baseline pipeline result;
- the exact starting graph and input identity: graph digest, provenance for
  captured source, YAML and package inputs, engine digest verified against that
  provenance, and the spec tree/version they describe;
- the copied engine path, copied graph and spec/source roots, tree config,
  modules path, relocated graph package-root map, and the exact regeneration
  `argv`; record the working directory, environment entry command, and local or
  remote execution boundary for every preflight and regeneration command;
- the worker's permitted YAML subtree and exact commands for the proposed
  assignment, regeneration and runtime checks, plus CI when applicable. Keep
  baseline pipeline, proposed-assignment, post-edit, runtime and CI results
  distinct; mark an inapplicable check with its reason.

For an eligibility comparison, freeze the exact source, spec, graph and provider
inputs. State the expected input schema and row count, and check both before
comparing eligibility. Even a read-only query may trigger repository devshell
setup writes and need shell escalation; use the project's environment rather
than per-tool workarounds.

Name the command that establishes selector uniqueness, including when the full
pipeline already covers it. Capture pre-edit verification's actual result and
streams too; terminal output alone is not a durable handoff record.

The adapter supplies the exact operation-specific chunk and YAML scope. Do
not infer a module subtree from a chunk ID; follow the CLI guide for the flags
each verb accepts.

For a runnable composition of one reviewed `modules merge`, starting after
`snapshot create` and `snapshot verify`, see
[`examples/isolated_edit.py`](examples/isolated_edit.py). It prints the plan,
runs the dry-run, applies with invocation capture, and regenerates explicitly;
it is an example of that edit flow, not a generic adapter framework.

Use one writable snapshot per worker. A Git worktree does not isolate external
source or package roots. A snapshot carries the graph inputs and engine, not
the full runtime closure: adapter commands must point runtime checks at the
specific worker or integration copies being tested, and include any required
runtime inputs they copy. State unsupported relocation when a required input
cannot be copied safely. A command plan or pre-dispatch invocation record is
not proof that a command ran or passed; retain each executed command's
resolved `argv` and `cwd`, then separately save its actual exit status,
`stdout`, `stderr` and result artifacts.

For native replay of Bazel analysis artifacts, materialize the complete input
closure with the `native_inputs` output group, then preflight generated source
and package-root inputs using the
[Bazel replay procedure](../../docs/bazel_integration.md#preserve-the-pipeline-cli-from-rbe).

Public skills must not encode private project names or fixed app taxonomies.

## Drive specs and tool improvements

The outcome is accepted, readable, idiomatic, runnable output for the configured
specs, with behavior preserved under explicit assumptions. Inspect module
boundaries, names, imports and runtime results as well as gate acceptance.
Output emitted despite rejection is inspection material. Keep required engine
architecture work explicit alongside downstream acceptance; neither alone
completes a goal requiring both.

Keep private inputs, concrete spec audits and project-specific progress in
[gaffer-private](https://github.com/agentydragon/gaffer-private). Public engine
changes and examples use neutral synthetic shapes.

### Complete one useful increment

Use this recipe for a scoped extraction or repair; reuse established evidence
when its inputs still match.

1. Load the [project adapter](#adapter-contract) and current handoff. Identify the
   source, spec and engine revisions, captured input and package roots, matching
   analysis, and authorized gate, emission and runtime commands.
2. Keep a [verified residual baseline and a useful passing candidate](#start-from-passing-residual-output-and-isolate-failing-assignments).
   Select one coherent addition or repair by its expected downstream payoff.
   Reduce a broad rejection before committing to an uncertain semantic project.
3. Resolve edited selectors with the complete joint context. Use the adapter's
   [source-only preflight](../../docs/cli.md#running-the-pipeline) when it supports
   that context; otherwise use full validation and record the missing capability.
   Reuse matching analysis and keep routine queries bounded.
4. Run the normal gate on the candidate. If it rejects, follow
   [assignment diagnosis](#diagnose-a-rejected-assignment). After combining
   passing edits, check their union too. For a full-spec repair, also run the
   adapter's canonical target; a subset pass covers only that assignment.
5. Review accepted emitted names, imports and module boundaries using the
   [module-shape criteria](module_shape.md). Run the relevant authorized runtime
   comparisons and confirm they exercise the changed behavior: equal empty
   results do not establish that a parser or transition ran.
6. Update the owning project's current-state handoff with the accepted spec,
   remaining residual coverage, exact revisions, command results and next failing
   addition. Distinguish partial acceptance, canonical acceptance, runtime
   observations and unverified behavior. Record tooling friction in Git.

### Combine forward and repair work

Choose tactics for the current obstacle. An obvious selector or ownership fix
can be handled directly. A difficult rejecting spec can be reduced to a passing
subset; a mysterious engine behavior can be reduced to a small JavaScript
regression case. Use these as composable heuristics without requiring every
task to follow a fixed sequence or repeat already established baselines.

In **forward work**, start with a JavaScript capture and a passing partial spec,
then extract and name coherent modules incrementally. Infer responsibilities
and architectural layers from behavior and call sites. Keep modules cohesive
and usefully sized; current paths and boundaries can change as understanding
improves. Retain a passing candidate as coverage grows.

In **repair work**, start with a rejected spec or a regression, including a
previously accepted layout exposed by stricter validation. Identify what
changed and [diagnose the rejection](#diagnose-a-rejected-assignment).
Compare matching source/spec/engine revisions and relevant assumptions. A
historical pass is evidence to investigate; it does not establish correctness
under the current contract or justify relaxing the validator. Preserve useful
reverse-engineered structure while choosing spec edits, inference, recognition
or annotations to repair the actual cause.

These approaches can interleave within one task. While extending a passing
partial spec, isolate a rejecting claim subset, minimize the relevant JavaScript
into a neutral regression fixture, repair the engine or spec, and continue
extraction. Use the [bug reproduction workflow](../../docs/bug_reproductions.md)
for that source reduction. Residual-baseline and subset reduction support both
approaches. Preserve useful reverse-engineered structure during repair, and
describe each probe's intended result without imposing an exclusive session
mode or global phase. Both approaches require correct, readable output and a
cheap iteration loop.

### Diagnose a rejected assignment

Read the actual candidate's complete structured reports and the
[rejection kinds](../../docs/cli.md#rejection-diagnostics). Trace the source units,
both directions of a blocking cycle, and the initialization effects behind each
edge. A module import cycle alone does not establish a read before initialization.
`tdz_after_collapsing_pass_one` concerns a hypothetical co-location; first resolve
the actual mutual/direct blockers and rerun the gate on that assignment.

| Established cause                                                                          | Next action                                                                                                                                                                                                                                                                                         |
| ------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Invalid selectors, missing anchors, stale inputs or incomplete probe context               | Repair the probe before drawing semantic conclusions. Ownership reduction must preserve the full selector and assertion context.                                                                                                                                                                    |
| Concrete initialization reads, writes or ordering make the requested split unsafe          | Repair the spec around the complete indivisible group, including anonymous statements and later mutations. Try one coherent owner or retain initialization in residual, then rerun the gate.                                                                                                        |
| A constraint represents possible behavior of opaque calls, getters, callbacks or providers | Establish the actual implementation and runtime premises, then choose [inference, sound recognition or an audited annotation](#choose-the-solution-depth). Conservative rejection can be correct under the existing contract. The [contract chooser](initialization_contracts.md) routes the audit. |
| The analysis or validator contradicts established facts under the current contract         | Follow the [bug reproduction workflow](../../docs/bug_reproductions.md): isolate the stage, minimize a neutral case that fails for the same reason, and verify the repair against the original blocker.                                                                                             |
| Input admission or reconstruction fails even with the residual-only baseline               | Investigate the input/tool failure before changing extraction boundaries.                                                                                                                                                                                                                           |

If the cause is unresolved, retain that uncertainty. A passing residual repair
establishes a useful lower bound; it does not prove the original split impossible.
A proposed co-location still needs architectural review and the normal gate.
Neither gate acceptance nor a cycle report establishes an annotation's truth.

### Start from passing residual output and isolate failing assignments

Every configured bundle needs an empty extraction spec that passes the normal
pipeline: retain the complete captured input/import context, assign no source
units to named modules, and use `inline_in_entry` for every chunk. All source
then stays residual in its original chunk and source order. Run and retain this
baseline for the matching engine and inputs; do not infer acceptance from its
trivial shape. If it fails, isolate the tool/input reconstruction defect before
treating a requested module boundary as the cause. Rejected readable output
cannot serve as the passing baseline.

Use divide-and-conquer when a large spec rejects or the next semantic repair
has an uncertain payoff. Work in a scratch spec, preserving the authored target:

For ownership-only probes with `inline_in_entry`, `catchall_file`,
`per_atomic_group`, or `coarse_atomic_groups`, prefer
[`run --ownership-subset`](../../docs/cli.md#ownership-subsets-with-full-selector-context)
with the complete canonical tree and its matching analysis. This retains joint
selector anchors and assertions while returning omitted claims to the selected
residual policy. Coarse mode may first complete atoms for listed retained
owners that are their sole authored owner, then groups remaining wholly
unclaimed atomic units by equal strict constraining reachability to existing
owners. This is a placement heuristic, and the normal gate still decides the
selected assignment. An empty
`modules` list is the no-extraction assignment under
that full contract;
the separate empty spec above remains the baseline without optional assertions.
Record the selected chunk-local module paths and inspect the resulting ordinary
gate and emission. Full graph export is unsupported for this operation.
Omitting an explicit catchall claim can recreate residual ownership without its
authored renames. Repair invalid full-spec selectors or claims
separately; ownership selection does not hide them.

Before splitting a module into temporary YAML siblings to withhold a few
members, try its optional `bindings` include list. Use authored readable
member/export names from the native `bindings list` catalogue or the YAML
claims, not minified `binding_names` or names adopted only by the emitted
report. An omitted list keeps every claim; `"bindings": []` keeps anonymous
statement claims, including the bindings those statements declare. Identify
source units by `(chunk_id, unit:N)` when combining chunk reports. To retain only
some anonymous claims, use their zero-based positions in the authored
`anonymous_statements` list; an empty list retains none. The
[CLI contract](../../docs/cli.md#ownership-subsets-with-full-selector-context)
and [spec contract](../../SPEC.md#ownership-subsets) describe the input and
limits. Keep full selectors, assertions, anchors and matching analysis in
place, and require the ordinary gate for each actual selected union.

Before publishing coverage totals, reconcile accepted report members with the
native authored-binding catalogue, including `readable_name` aliases and
normalized module paths. Retain the baseline identity set and list additions
and withheld identities. Automatic helper labels in emitted module reports
are not additional authored claims; report unmatched labels separately.

1. Reduce the rejecting spec by returning suspected groups of claims to
   residual until a useful subset passes. That candidate is a known passing
   lower bound. Alternatively start from an existing passing partial spec or
   the residual-only baseline. Include anonymous statements and complete
   atomic units in each group.
   An atomic-group rejection is a co-location constraint: inspect the complete
   group and try assigning it to one coherent owner before treating it as
   requiring semantic improvement. Preserve anonymous units and full selector
   context in the probe; group size alone does not establish incoherence. This
   only addresses the co-location requirement: rerun the normal gate, since
   module cycles, order, exports, or interaction with entry co-location may
   still reject the assignment.
2. Restore groups, run the normal gate, and split failing additions against
   the same passing base. Keep passing groups and continue on rejected
   combinations. If both halves
   pass separately but their union fails, isolate their interaction; there may
   be several independent conflicts and no single bad module.
3. Retain the largest useful passing partial extraction found and a small
   failing addition that explains the next blocker. Use that addition to choose
   inference, specific recognition, an audited annotation, or a different
   coherent boundary. Report partial coverage and residual code explicitly.

Use normal reports to choose large first probes. Try retaining all authored
modules in already accepted chunks together with the existing passing roster.
For rejected chunks, cycle owners and named owners in atomic-conflict reports
suggest groups to return to residual. Run the resulting combined assignment:
removing every reported cycle owner can still leave atomic conflicts. Iterate
from the actual remaining rejection, rather than treating a cycle-free result
as acceptance. Use native module catalogs for normalized chunk-local paths;
module-root prefixes in authored YAML are not necessarily those identities.

Use the [diagnosed actual blockers](#diagnose-a-rejected-assignment) for the first
coarse candidate, then bisect remaining failing additions. Removing the owner union
of hypothetical collapsed-cycle reports can discard otherwise viable modules.
For a large **actual** cyclic SCC, compare coarse omission of new SCC owner
groups with pruning or bisection at the cut endpoints. If repeated frontier
cuts recover little ownership, switch tactics. A hypothetical pass-one owner
union is not an exclusion set; check each actual union with the normal gate.

Removal can change module cycles and selector dependencies, so do not assume
acceptance is monotone or prune untried subsets from one failing result. A
branch-and-bound search needs an explicit objective and justified bounds; a
budgeted reduction yields a locally reduced conflict and a best-known passing
spec, not proof of a globally maximal extraction or minimum removal set.

A source-match capture can also be an anchor referenced by surviving typed
selectors. Preserving its emitted name with a plain chunk rename does not
recreate that selector anchor. When reducing ownership, retain source-match
owners referenced by surviving selectors or rewrite those selectors using a
supported equivalent. If an adjacent declaration is needed only to identify a
helper, keep it as unclaimed context in that helper's contiguous
`source_matches[].match` and list only the helper in `bindings[]`; see [keeping an
adjacent declaration as local selector context](../../docs/selectors.md#keep-an-adjacent-declaration-as-local-selector-context).
That local context does not create an anchor entity for other selectors.
Before running the gate, inspect [`spec validate`'s template classifications](../../docs/cli.md#selector-outcomes)
and check that an intended `reference` has not become a `wildcard` after its
anchor was removed. A unique successful match alone does not establish that
the original binding relationship is preserved.

Configured `module_roots` still require their directories when they contain no
selected YAML. A reduced config can omit unused root mappings instead of
staging empty markers; check its native analysis projection before reuse.

Hold source, package roots, engine, vendor substitutions and analysis policy
fixed while isolating ownership. Keep author assertions fixed too: annotations
and `resolved_member` selectors may depend on removed claims. Preserve their
exact scope through supported spec operations, or classify the probe as invalid
and report the missing capability. A truly empty spec with no optional author
assertions is a separate baseline if removing them changes analysis inputs.
Reuse cached analysis only when native projection/provenance checks accept it;
never bypass those checks to make a subset cheap.

Record each tried subset, exact input/spec/engine identity, actual gate result,
failing reports and termination budget. A selector/configuration failure is not
a semantic rejection. Bound the probes, reuse completed results and matching
analysis, and record missing subset/baseline tooling instead of building a
private graph reader or spending an open-ended round on manual edits. Gate and
runtime evidence for a partial spec applies to that spec; the full authored
spec remains a separate target.

### Select an observable capability

Maintain a short ranked plan in the owning project's `plans/` directory. Repair
introduced soundness defects and regressions first. Choose subsequent work by
expected canonical benefit and total implementation, validation, audit,
maintenance and operator cost. A module extraction or justified assignment
repair can advance the goal without an engine change. Include usability,
iteration speed and memory work when their payoff warrants the attention.

For substantial work, record the selected failure or capability, matching
source/spec/engine identity, source-backed cause, chosen solution route,
expected downstream change, authorized validation and remaining prerequisites.
Prefer a complete causal path to a useful result over an open-ended list of
missing interpreter primitives. When a new prerequisite expands that path or
the evidence contradicts its predicted benefit, reconsider the choice before
dispatching the next implementation slice. Review-sized PRs may implement one
capability in pieces; their count is not the outcome.

### Choose the solution depth

Compare these routes briefly before committing to a broad implementation. The
letters identify alternatives, not a mandatory ladder:

| Route                             | Appropriate use                                                                                                                                  | Required boundary                                                                                                                                                                 |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **A: general semantics**          | The construct recurs broadly, or general support costs less than maintaining special cases and audits.                                           | Model the operation and its unknown cases through the shared value/effect semantics.                                                                                              |
| **B: sound specific recognition** | A bounded source category covers the blocker with cheaply checkable sufficient conditions.                                                       | Every match must justify the conclusion under its checked conditions and declared assumptions. Missing other valid cases is acceptable; a false positive is a correctness defect. |
| **C: audited annotation**         | Automatic proof is expensive, while the author can reasonably promise the needed behavior or known library identity for this configured program. | State the exact assertion and scope, use an existing contract when it fits, and obtain any required author decision. A passing gate does not establish the assertion's truth.     |

A specific wrapper recognizer or a known-library annotation may avoid a long
prerequisite chain for arbitrary loader execution. Source resemblance alone
does not establish runtime identity or behavior. The
[contract chooser](initialization_contracts.md) separates timing, effects,
retention, result and identity obligations; assert only the facts the audit
supports. Document a new annotation's contract in `SPEC.md` and retain existing
approved contracts unless a changed assertion is explicitly justified.

Before reusing an older audit, compare its premises with the current spec and
both original and replacement providers. Fresh allocation alone does not
establish absence of inherited getters or integrity of the intrinsic operations
used on the value. Check the relevant property slots and effects;
an excluded intrinsic family does not supply an integrity promise. If that
evidence is missing, preserve the passing checkpoint and try another coherent
ownership boundary before extending a chain of ordering annotations.

Use standing user authorization for scoped model and annotation decisions;
do not request fresh approval for each choice already within that authority.

All routes feed the same typed facts, value/effect semantics and validator,
retaining the distinction between source proof and author assumption. A
recognizer need not become a second evaluator, and an annotation must not
bypass validation. Narrow coverage and annotations can remain the right
solutions. Graduate them to broader inference when demonstrated reuse,
repeated audit cost or maintenance justifies it, then remove redundant rules
or stale assertions without silently expanding the promised scope.

After a recognizer or audited operation succeeds, consider a full census of the
remaining authored keys for that same category and provider before adding
semantic prerequisites for a narrow failed or near-complete-owner cohort. Include
fully unselected and partially selected owners: owner- or function-level filters
can hide eligible binding-level progress. Keep physical implicit placement
separate from explicit canonical claims; unrestricted `bindings` can still leave
explicit `anonymous_statements` excluded, so it is not full owner extraction.
Treat template initializer shape as a lead only. Source and provenance, native
guards and author premises determine eligibility; retain the combined ordinary
gate and retention checks on the actual candidate.

### Keep the iteration loop cheap and informative

- Use the command's [name-resolution view](../../docs/cli.md#name-and-identifier-resolution).
  Raw source lookup takes input names; `describe` and `inspect-source --id`
  resolve readable graph names. `bindings list` labels direct selector
  identifiers, source-match captures, and structural member selectors
  separately; a source-match capture's local token is not an input-name pin.
  Recover the input identity through the matching native graph before proposing
  new selector support.
  Check `query.kind`: a module basename takes precedence over a binding name.
  For a collision, describe the current full owner path and select the intended
  exported-name row instead of taking the module report's first binding.
  For bulk recovery, prefer the matching native `modules.json` report's aligned
  input/emitted-name arrays and `binding_source_units` rows over thousands of
  individual descriptions. The latter maps each input name to its declaration
  source units, including redeclaration sites. A run rejected before emission
  may not produce that report; resolve the required endpoints through bounded
  native `describe` queries on the original authored module and readable name.
- Trace imported callees through the import slot, provider export/re-export
  and provider-local declaration. A raw provider binding with the same spelling
  as the imported export can be unrelated. Record the full chain before choosing
  an effect summary, recognizer or annotation; a raw-name source excerpt alone
  does not identify an imported function.
- Reuse matching cached analysis for spec changes the staged pipeline excludes
  from its analysis inputs. Refresh expensive analysis when those inputs or
  the engine change. Request full graphs and evidence streams when the
  investigation needs them; keep routine output bounded.
- Before repeating a rejected addition, name the changed input or causal
  mechanism expected to change its witness. An unrelated chunk's coverage gain
  alone does not justify rerunning the same failing assignment.
- Read the complete gate inventory, structured reports and their truncation
  metadata. The first console rejection is not the inventory, and a selected
  cycle cut is not its reciprocal path. Absence of a secondary report does
  not establish absence of that failure class.
- Check the predicted witness in existing normal CI artifacts where possible.
  Unchanged gate verdicts can conceal precision regressions; inspect affected
  execution completeness and constraints, and classify new restrictions.
  Edge counts alone conflate ordering requirements and duplicate provenance.
  For ownership edits, compare the full authored layout in the same report
  mode before and after, alongside the passing subset. Returning an excluded
  initializer's canonical claim to residual can add cycles. When coherent,
  retain that initializer in its own unselected owner and select the helpers
  separately.
- Distinguish public engine checks, private integration, accepted canonical
  output and runtime proof. A merged pin or green synthetic suite establishes
  only its own scope. Track the exact head and live job; let an existing run
  finish before refreshing its pin merely for bookkeeping. If a rebase requires
  a shared dependency update, preserve both features in a containing revision
  and check the intervening semantic changes. Earlier artifacts remain evidence
  for their recorded engine; refresh matching analysis and configured-target
  checks when the new revision changes analysis semantics. Keep patch URLs and
  integrity values consistent with the chosen source pin.
  For routine BuildBuddy polling, prefer `bbapi invocation status` and selected
  metadata fields. Full invocation records can contain serialized command lines;
  do not print or commit those payloads wholesale.
- Prefer direct storage/lifetime improvements when their mechanism is clear.
  Use a real profiler for an unanswered allocation or CPU question when it
  would change the decision. Bound that investigation; do not make every
  optimization contingent on a separate benchmarking project or claim
  unmeasured aggregate savings.
- Record confusing output, failed assumptions and avoidable rabbit holes in
  the owning project's `debug/` or `TODO.md` and commit them. Fix cheap friction
  in context; rank larger follow-ups with the main work.

Use the task's authorized checks and resource limits. These proof stages do
not authorize extra corpus runs, tests, profiles or workers. Report missing
evidence explicitly. End a slice with the observed capability change and the
remaining gap; if it made no demonstrated movement, reassess its justification
before extending the prerequisite chain.

## Coordinate a round

1. Select the outcome and solution route above. Reuse matching pipeline
   outputs, graphs and reports, refreshing stale evidence as needed. Use
   [planning](../../docs/planning.md#plan-an-assignment) for assignment work.
2. Route residual proposals through intake. Send reviewed existing-module maps
   directly to workers; intake does not consume decomposition reports. Prefer
   coherent synthesis buckets for selector cleanup.
3. Establish each worker's isolated inputs and permitted scope using the
   [adapter contract](#adapter-contract), then dispatch the relevant role.
4. Collect completed patches and execution evidence; integrate green work using
   [the integration procedure](../../docs/integration.md).
5. Update inflight/landed/blocked assignments, architecture notes, and durable
   project conventions. Track the graph/build command and input identity behind
   metrics such as residual units, patch-stream members, named-module fraction,
   selector debt, and largest generated files.

When delegation is useful and authorized, route substantial source discovery to
intake, architecture decisions to the architect, naming-only work to the namer,
and merge trains to the integrator. The orchestrator owns adapter details,
broadcasts working commands and retains the canonical outcome across handoffs.

## Execution evidence

Have the launcher persist argv and cwd before execution, as in the
[recorded example](examples/isolated_edit.py). CLI invocation output may be
absent after setup or argument-parsing failures. Keep the actual exit result
and streams separately; reconstructed commands are not execution evidence.

Workers report the branch/commit, starting manifest and graph/input identity,
reviewed relative YAML delta, destinations/bindings changed, skipped units and
reasons, and architecture/intake/tooling follow-ups.

For each executed command retain resolved `argv` and `cwd`, actual exit status,
`stdout`, `stderr`, and result artifacts. State which proof it covers: baseline
pipeline, proposed assignment, regeneration, selector uniqueness, runtime, or CI.
Keep those outcomes distinct and explain inapplicable checks. A command plan or
pre-dispatch invocation record does not prove execution or success; the
[invocation-record contract](../../docs/cli.md#resolved-invocation-records) defines
those fields.

## Isolation

Writers use isolated Git worktrees and build output bases; read-only roles may
inspect the main checkout. Snapshots also isolate external source/package roots.
Project conventions take precedence over generic guidance.

For each changed producer regenerated from BuildBuddy artifacts, use a fresh
clone-owned `bb-out/` download. A writer can clear its destination and still
copy stale inputs from a reused download tree, so require the adapter's
review-tree equality and normal smoke checks before accepting the output.
Preserve any prior tree outside the workspace and follow the
[downloaded-artifact recovery procedure](https://github.com/agentydragon/ducktape/blob/devel/devinfra/buildbuddy/docs/remote.md#checking-a-generated-tree-after-download).

Before a Git-based remote gate, stage new spec/source/fixture files or verify
that the runner includes them. Check the logged input snapshot: omitted files
were not validated.

## Failure routing

- Environment failure: find a working adapter command and broadcast it.
- Stale graph: refresh matching evidence before diagnosing another cause.
- Gate rejection or split atomic group: use the
  [assignment diagnosis procedure](#diagnose-a-rejected-assignment). Expand a
  lane to the complete group when that yields a coherent task; otherwise return
  the evidence for a boundary or tool repair. Preserve conservative constraints
  outside the established proof or audited contract.
- Unclear destination: route to the architect; broad source discovery: intake.
- Repeated selector synthesis gaps: route to Ducktape tooling using
  [selector debt](../../docs/selectors.md#selector-debt).

If Debundle tooling blocks the task, follow the [bug reproduction
workflow](../../docs/bug_reproductions.md) before working around it.
