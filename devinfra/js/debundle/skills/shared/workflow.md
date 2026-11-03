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
  `argv`; record the working directory used for every command;
- the worker's permitted YAML subtree and exact commands for the proposed
  assignment, regeneration and runtime checks, plus CI when applicable. Keep
  baseline pipeline, proposed-assignment, post-edit, runtime and CI results
  distinct; mark an inapplicable check with its reason.

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
changed and classify the failure: invalid spec/selectors, a real unsafe split,
analysis imprecision, an engine regression, or unsupported reconstruction.
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

1. Reduce the rejecting spec by returning suspected groups of claims to
   residual until a useful subset passes. That candidate is a known passing
   lower bound. Alternatively start from an existing passing partial spec or
   the residual-only baseline. Include anonymous statements and complete
   atomic units in each group.
2. Restore groups, run the normal gate, and split failing additions against
   the same passing base. Keep passing groups and continue on rejected
   combinations. If both halves
   pass separately but their union fails, isolate their interaction; there may
   be several independent conflicts and no single bad module.
3. Retain the largest useful passing partial extraction found and a small
   failing addition that explains the next blocker. Use that addition to choose
   inference, specific recognition, an audited annotation, or a different
   coherent boundary. Report partial coverage and residual code explicitly.

Removal can change module cycles and selector dependencies, so do not assume
acceptance is monotone or prune untried subsets from one failing result. A
branch-and-bound search needs an explicit objective and justified bounds; a
budgeted reduction yields a locally reduced conflict and a best-known passing
spec, not proof of a globally maximal extraction or minimum removal set.

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

Use standing user authorization for scoped model and annotation decisions;
do not request fresh approval for each choice already within that authority.

All routes feed the same typed facts, value/effect semantics and validator,
retaining the distinction between source proof and author assumption. A
recognizer need not become a second evaluator, and an annotation must not
bypass validation. Narrow coverage and annotations can remain the right
solutions. Graduate them to broader inference when demonstrated reuse,
repeated audit cost or maintenance justifies it, then remove redundant rules
or stale assertions without silently expanding the promised scope.

### Keep the iteration loop cheap and informative

- Use the command's [name-resolution view](../../docs/cli.md#name-and-identifier-resolution).
  Raw source lookup takes input names; `describe` and `inspect-source --id`
  resolve readable graph names. A blank `bindings list` `minified` field for a
  structural selector does not prove the input identity is unavailable. Recover
  it through the matching native graph before proposing new selector support.
  Check `query.kind`: a module basename takes precedence over a binding name.
  For a collision, describe the current full owner path and select the intended
  exported-name row instead of taking the module report's first binding.
  For bulk recovery, prefer the matching native `modules.json` report's aligned
  input/emitted-name arrays over thousands of individual descriptions.
- Trace imported callees through the import slot, provider export/re-export
  and provider-local declaration. A raw provider binding with the same spelling
  as the imported export can be unrelated. Record the full chain before choosing
  an effect summary, recognizer or annotation; a raw-name source excerpt alone
  does not identify an imported function.
- Reuse matching cached analysis for spec changes the staged pipeline excludes
  from its analysis inputs. Refresh expensive analysis when those inputs or
  the engine change. Request full graphs and evidence streams when the
  investigation needs them; keep routine output bounded.
- Read the complete gate inventory, structured reports and their truncation
  metadata. The first console rejection is not the inventory, and a selected
  cycle cut is not its reciprocal path. Absence of a secondary report does
  not establish absence of that failure class.
- Check the predicted witness in existing normal CI artifacts where possible.
  Unchanged gate verdicts can conceal precision regressions; inspect affected
  execution completeness and constraints, and classify new restrictions.
  Edge counts alone conflate ordering requirements and duplicate provenance.
- Distinguish public engine checks, private integration, accepted canonical
  output and runtime proof. A merged pin or green synthetic suite establishes
  only its own scope. Track the exact head and live job; let an existing run
  finish before refreshing its pin merely for bookkeeping.
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
- Gate rejection: read the structured cycle/atom reports and the source-backed
  [diagnostics](../../docs/cli.md#rejection-diagnostics). Establish initializer,
  called-binding, provider, and runtime facts, then choose a spec repair or one
  of the [three solution routes](#choose-the-solution-depth). The
  [contract guide](initialization_contracts.md) routes assertion audits; a cycle
  alone supplies none of their premises. Preserve conservative constraints
  outside the chosen proof or approved contract, and verify the same gate and
  runtime behavior after justified edits within the task's execution limits.
- Split atomic group: expand the scoped lane to include the whole group or
  redispatch coordinated work.
- Unclear destination: route to the architect; broad source discovery: intake.
- Repeated selector synthesis gaps: route to Ducktape tooling using
  [selector debt](../../docs/selectors.md#selector-debt).

If Debundle tooling blocks the task, follow the [bug reproduction
workflow](../../docs/bug_reproductions.md) before working around it.
