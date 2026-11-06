# Selector resolution: specification

What `debundle` guarantees when it places spec entities in a bundle chunk. How to
write selectors: <docs/selectors.md>. How it is implemented:
`docs/selector_resolution.md`.

## Resolved CLI invocations

The global `--print-invocation` option parses the selected command normally,
resolves its declared CLI values, environment-backed values, and meaningful
defaults, then prints a typed `ResolvedInvocation` and exits before dispatch.
It does not read graph contents, run source analysis or proposal work, create
operation outputs, write graph rejection reports, or modify spec files.
Resolution uses parsed CLI metadata, the current working directory, and
executable path metadata only; it does not require file inputs to exist or read
their contents. If `--invocation-out` is supplied, writing that requested
record file is the sole print-only write.

`--invocation-out <path>` persists the same record before the selected
operation starts. An execution record is provenance for the attempted call,
not proof of success or of completed validation. Its `requested_proof` records
the checks the command asks to run, and its `write_scope` records possible
operation writes. Neither field describes a completed result.

The record includes the current working directory, executable path, typed
argument values and their sources, replay argv, and a shell-quoted command.
Declared environment-backed values are emitted as explicit replay arguments;
explicit CLI values take precedence. The record does not dump ambient
environment variables. A replay preserves file selection and relative-path
resolution while the caller environment changes, but argv alone does not pin
the contents of a spec, source tree, graph, or executable. Reproducing a
mutation against a changed spec requires the corresponding input identity or
snapshot provenance.

Operation classification is based on the selected CLI shape: graph-snapshot or
diagnostic-snapshot read, source-snapshot read, fresh pipeline, snapshot
creation or verification, validated edit, unverified edit, conditional comment
operation, or read-only analysis. Unrecognized shapes remain unclassified.
Running an edit gate does not establish full pipeline, reconstruction, or
runtime validation.

## Transform input modes

`--spec` selects flat input. It refuses explicitly supplied `--tree-config`,
`--tree-modules`, `--tree-vendor-marks`, `--tree-source-root`, and `--out-root`.
Inherited tree source/output defaults are inactive in flat mode and are omitted
from invocation replay arguments. Tree mode retains its existing environment
defaults. `--package-root` and `--packages-root` are shared inputs in both modes.
With `run --reports-only`, `--out-root` is also a shared input and must name
that same reports destination. The standalone projector applies the same mode
resolution.

## Read-only query failures

Once Clap accepts the command line, runtime failures from `describe`,
`inspect-source`, `graph-summary`, `atoms`, `coverage`, `scc`, and `cluster`
follow the selected output format. In `json` mode the CLI writes one object
with a typed `reason` and the full actionable error chain in `message`; in
`ndjson` mode it writes that object as one row. Both modes exit nonzero.
Both modes retain the same diagnostic on stderr. Reason codes are `io`,
`invalid_json`, `source_unit_not_found`, `unsupported_identifier_prefix`, and
`query_failed`. Unsupported-prefix failures include the identifier, prefix,
accepted forms, examples, and an available suggestion. Graph compatibility
failures retain the existing `graph_compatibility` rejection payload. In `text`
mode, runtime errors keep the existing stderr diagnostic and leave stdout
without an error report.
Clap parse and usage failures occur before this runtime boundary.

## Input and spec scope

One run or validation resolves one exact spec against one configured input
program. The program may contain multiple chunks; each entity is resolved only
against the chunk named by its spec tree. The matcher does not consult another
spec or another source snapshot. Structural matching follows the rules below:
tree equality with the documented holes and consistent lexical alpha-renaming.

Authoring tools may inspect that same spec and source, then report selector
diagnostics or propose structural candidates. Those reports do not establish
semantic intent and do not change the spec unless an author explicitly applies
an edit. A new upstream snapshot needs its own reviewed spec and exact validation
against that snapshot.

The `modules refine` and `modules merge` authoring edits may combine source-match
records with byte-identical templates and matching metadata into one ordered
capture list. Conflicting notes or overlapping local captures remain separate.
The combined document passes the same production selector resolution and edit
gate before writing; regrouping does not bypass source-unit identity checks.

Ownership-changing edits (`bindings assign`/`unassign`, `modules apply`/
`refine`/`merge`, and non-empty `modules delete --force`) require the canonical
candidate relocation assessment before a verified write. This assessment uses a
verified staged artifact and explicit full-tree transform context. An edit may
address the full authored tree or one configured subtree belonging to its graph
chunk. Canonical tree compilation overlays those exact candidate documents and
preserves documents outside the graph chunk, including other configured roots.
The full tree must match the verified graph's authored root. The joint report
contains every required materialized chunk under `chunks`; its overall `status`
accepts only when all chunk effect/placement assessments accept and the concrete
reconstruction passes the normal cross-chunk rewrites and final program
validation. Missing or duplicate chunk assessments cannot authorize a write.
Validated ownership candidates require an authored module tree without symlink
entries, since aliased entries cannot be treated as independent documents.
Graph-only dry runs check claims and selectors and report
`needs_full_assessment`; they cannot authorize a verified write. Explicit
`--no-verify` remains an unvalidated write where the command supports it.
Names-only edits and assignment-invariant no-ops keep their existing checks.
The assessment does not establish replacement, privacy, or singleton guarantees.

`modules merge --batch` composes all requested whole-module merges before one
edit-gate check. Rejected plans and gate failures leave every source and target
unchanged. Successful batches retain exact physical filenames and all claim and
metadata fields, write every target before deleting sources, and use atomic
per-file replacement rather than a crash-atomic multi-file transaction.

Verified ownership-changing edits require module trees without symlink entries.
Writing one module must not silently change claims read through an untouched
alias. Rejected edits leave the authored files unchanged.

## Ownership subsets

`run --ownership-subset <json>` changes the output ownership assignment while
resolving the complete authored spec and retaining all its analysis contracts.
The JSON is `{"modules":[{"chunk":"app","module":"features/view"}]}`;
`{"modules":[]}` selects no named modules. Chunk IDs and chunk-local module
paths must name existing materialized chunks and explicit modules. Duplicate
entries and unknown JSON fields are errors.

Each module entry may include `"bindings":["parseView"]`, an include list of
the module's authored readable member names (the final `name:` or default
export name). Omitting `bindings` keeps all its member claims. An empty list
keeps its anonymous-statement claims but no named member claims; bindings
declared by a retained anonymous statement still move with that whole
statement. Unknown or repeated binding names are errors. The list is an
ownership selection, not an initializer-expression or source-unit selector.
Indivisible declarations and rebind units remain subject to the normal
placement and realizability checks.

Full selector outcomes, relational anchors, duplicate/unmatched claims and
annotations are checked before ownership is reduced. The selection does not
change the analysis projection or source graph. Retained bindings, imported
re-exports, destructuring companions and anonymous statements use the same
normal assignment assembly, gate, translation validation and emission.
For an actual Pass-1 constraining SCC, `cycles.json` includes a complete
`pass_one_witness` closed walk of ordered, typed edge causes. Its text summary
previews a bounded number of steps and states how many were omitted from the
preview. Actual Pass-2 TDZ and conditional after-collapse diagnoses have
`pass_one_witness: null`; their cuts remain available, but this witness does
not claim to explain their reciprocal import path.
Omitted claims follow the selected residual policy: they return to entry under
`inline_in_entry`, move into the configured catchall under `catchall_file`, or
move into generated atomic-group modules under
`per_atomic_group`. Under `coarse_atomic_groups`, wholly unclaimed atomic
groups with equal reachable fixed-owner sets in the strict constraining
projection may share generated modules within intervals between fixed owners
in the fine layout's simulated evaluation order, subject to compatible public
export origins in each merged module. Its optional
`complete_partial_atomic_groups_for` array names authored module paths that
may adopt unclaimed members of an atomic group only when that module is the
group's sole authored owner. Paths are checked against the complete authored
module roster, including during ownership-subset runs. An owner omitted from a
subset cannot adopt members; those claims follow ordinary coarse residual
routing. Groups omitted by the simulator remain separate initially. Within an
otherwise matching signature, coarse routing may combine atoms only when each
public export name still identifies one source binding or source export site
in the combined module. Incompatible atoms stay in separate generated
modules; their source export names are preserved, and the ordinary export
validator remains authoritative. The normal complete-assignment gate may then
co-locate a whole actual mutual-constraining SCC only when every member is a
generated coarse residual module and the combined public export names remain
compatible. This refinement repeats until no eligible SCC remains. It does not
merge authored modules or mixed SCCs. Conditional diagnostic summaries do not
authorize merging. Any remaining blocking cycle, duplicate export, or actual
eager TDZ rejection is still rejected by the unchanged validators. Generated
paths use `__auto/residual_groups/NNNN`.
Selected module names and explicit `chunk_renames` remain intact; omitted
members retain original source names. The authored spec is unchanged.

Ownership filtering precedes residual routing for every supported residual
policy. An omitted explicit claim at the catchall path loses its authored
renames and annotations; ordinary residual routing can recreate that destination.
Full graph export remains unsupported with this option.
Each chunk's graph summary and module report records its retained module paths
and optional binding lists in `ownership_subset`, including empty lists.
Acceptance is for that selected
assignment only; it does not establish acceptance of the full canonical
assignment or complete effect coverage. Reusing analysis still requires the
normal matching engine, source and full-projection checks.

## Accepted output

Unresolved claims prevent acceptance. A run may write diagnostic reports for a
rejected spec, but writes the reconstructed JS tree, browser harness, and vendor
assets only after final program validation. Final checks inspect the exact
reconstructed syntax, including synthesized imports and exports and vendor
replacement. Syntax placement must agree with the validated assignment, including
whole-statement moves and repeated declarations. Emission consumes an immutable
validated plan.

Generated module headers inline source-unit and binding lists only when each
line fits within 160 bytes. Larger lists use a count and point to
`reports/tree/<chunk>/chunk.json` at the output root; its `selected_module_lowerings`
records retain all binding names and their source-unit IDs. Generator and
detector pragmas remain separate from these summaries. Authored comments are
emitted in full. The same pointer works for readable-tree and browser-harness output.

When browser output is configured, reconstruction also resolves the HTML module
entries and preloads against the retained chunks, prepares the bootstrap and
rewritten HTML, and enumerates copied assets. Invalid loading topology rejects
the plan before any accepted output is written, including during a dry run.
The browser writer materializes those decisions without rediscovering entries.

### Report output selection

Normal runs omit full dependency/program graph serialization and emit
`graph_summary.json` with exact graph counts and validation verdicts. `--graphs`
selects full graphs independently of diagnostic detail and effect-conflict
streams. Both selections retain all analysis facts, generated edges, premises,
gate/TV checks and final reconstructed-program validation.

Every `final_module_contents` row in the per-chunk `modules.json` report includes
`binding_source_units`: one row per `binding_names` entry, in the same input-name
order. Each row maps that input binding name to sorted `unit:N` IDs of source
units that declare it. Redeclaration sites all appear, and IDs follow the
analysis's source-unit boundaries, including units created by splitting a
comma-list declaration. Reads, writes, and calls do not count as declarations.
The existing module-level `source_unit_ids` remains the sorted union of
declaration IDs for the row's named bindings; `member_names` remains
positionally aligned with `binding_names`.

For a rejected assignment, `atomic_group_conflicts.json` and `cycles.json` are
each emitted when their corresponding evidence is nonempty, and may coexist.
An atomic-group conflict remains the primary gate verdict when both are present;
the cycle report is secondary evidence. `graph_summary.json` records both
counts. A missing `cycles.json` means zero blocking SCCs only when the current
report set completed successfully. Edit-gate writers apply the same report
selection to the post-edit assignment and replace prior rejection artifacts
after graph, assignment and shared validation complete. A preflight error leaves
the last completed gate result intact.

Each atomic conflict report includes a deterministic witness between the first
claim and the first later claim assigned to a different module. It records a
path in each direction using the same `G_atomic` projection as the gate. Every
step embeds the dependency-edge report plus its projection and orientation, so
a reversed traversal is identified as a symmetric co-location constraint;
directed initialization dependencies are never reversed. Each path keeps its
first twelve steps and reports the complete `total_step_count` and `truncated`
flag. This is bounded evidence for the selected pair, not a minimum repair set.

`--reports-only <OUT_ROOT>` requires `--analysis` and writes reports without
program/browser/vendor output. It follows the same canonical materializer and
final validator; it cannot turn a rejected program into success. It conflicts
with dry-run and inconsistent output roots. With emit-on-rejection, a gate-only
rejection preserves typed exit 3 and a truthful marker; other errors fail.
The Bazel pipeline's default output excludes full graphs even with
split_dependency_graphs enabled. A separate on-demand export action consumes
the exact same Analyze artifact for its dependency_graphs output group.
That reports-only action enables typed gate-rejection emission so it can finish
exporting every configured chunk; exit 3 is accepted only for a gate-only
rejection. It writes no JavaScript. Other failures still fail the action.

The public `debundle_cli` macro binds a graph-consuming CLI runner to that output
group and resolves generated graph, source, and package roots through Bazel
runfiles. Its input paths and invocation are documented in
<docs/bazel_integration.md>.

The Bazel `native_inputs` output group exposes the rule's declared source,
package, spec and ownership-subset inputs for native replay. Requesting it alone
selects their producers. The `analysis` and `debundler` groups supply the
matching analysis artifacts and executable.

The public `debundle_pipeline` rule accepts an explicit `analysis` file or
filegroup containing exactly one `.analysis` artifact. That target registers
only emission and optional graph-export actions and forwards the supplied
`analysis` output group. Saved-analysis engine, source and projection checks,
assignment gates and translation validation remain mandatory. Each consumer
declares its complete current inputs. Tree config and vendor marks accept source
or generated file labels; module YAML inputs remain declared in `spec_tree_inputs`.

The public `debundle_pipeline` rule's `analysis_threads` attr defaults to zero,
omitting the override so `DebundleAnalyze` uses inherited `RAYON_NUM_THREADS`
when set, or Rayon's default otherwise. A positive value sets the tracked
`RAYON_NUM_THREADS` action environment for `DebundleAnalyze` only; projection,
Pipeline and graph export keep their existing environments. Negative values fail
during rule analysis. A nonzero value also fails when `analysis` is supplied;
configure Analyze threads on the producing target instead.

Staged `.analysis` headers carry `analysis_engine_sha256`, the generated
configured compiled `staged_analysis` closure identity shared by the writer and
reader. The closure owns source preparation, cross-module and per-chunk fact
computation, loader derivation, projection/schema code, context reconstruction,
and artifact encoding and decoding. It includes build outputs, native link
inputs/flags and toolchain inputs before final executable dead stripping.
The `pipeline`, `lowering` and `cli` crates are outside this closure; shared
dependencies may still invalidate analysis conservatively. Both analyzer
entry points use the same implementation and identity. A reader rejects another
analysis identity before decoding facts, checks parser-input digests, and
requires exact canonical projection equality before using the facts. Previous
compiled identities require regeneration; there is no compatibility fallback.
Saved graphs and snapshots retain their full executable identity and their
source/spec checks, even when an analysis artifact can be reused. The standalone
CLI remains one binary.

### Reading-only output on gate rejection

`debundle run --emit-on-rejection` changes only what a realizability-gate
rejection writes; it is mutually exclusive with `--dry-run`. The selector pass,
the gate and its evidence reports under `reports/tree/<chunk>/`, plus explicitly
selected full graphs, are unchanged, and the run still exits nonzero with the
gate's primary diagnostic. When an atomic conflict and blocking SCC coexist,
both existing evidence files are written while the atomic conflict remains
primary. An accepted spec produces exactly the output it produces without the
flag.

The run exits 3 exactly when the gate's rejection is its only failure and the
marked tree below is complete; every other failure exits 1. A gate rejection
alongside unresolved spec claims is such a failure and writes no tree.

When the gate rejects a chunk, the rejected assignment is lowered anyway and
the `write_js_tree` tree (entry and logical-module files, `package.json`, the
tree reports) is written to its output directory — or, for a spec that emits
only a browser harness, to the harness's — marked unrunnable:

- a `GATE_REJECTED` file at the output root, written before any module file,
  names each rejected chunk's verdict and the paths of its evidence report and
  dependency graph, followed by the rendered diagnostic;
- each rejected chunk's entry file opens with a `//` banner naming the
  `GATE_REJECTED` marker, the verdict and the evidence report path.

The rejected program never reaches final program validation, and no browser
harness, bootstrap or rewritten HTML, vendor replacement asset or vendor
manifest is written: those describe a runnable bundle. The emitted modules are
for reading, naming and review; by construction their module graph has the
TDZ or side-effect-ordering hazard the gate reported (<docs/design.md> § Why a
static gate, not a runtime check), so nothing may load them as a program.
Without `write_js_tree` or a browser harness configured the flag writes nothing
extra.

## Dependency-graph array ordering

The dependency graph preserves source order for content observations on array
literal identities that the chunk value-flow analysis can track, including
aliases and reads promoted from directly invoked chunk functions. Allocation
seeds a separate pending frontier for each tracked array origin, so later
opaque barriers remain connected even through unsupported protocols. Allocation
alone does not depend on preceding opaque operations or reset existing content
writers/readers: one source origin can represent multiple runtime arrays.
Before any recognized eager literal-origin array exists, opaque operations do
not start an array-content chain. Lazy literal-allocation origins are
conservatively live from the start because hoisted or escaped functions may
allocate before their lexical statement. This ordering also applies to pure readers:
call purity does not make an observed array's contents immutable. It is emitted
in both strict and dataflow-aware S-chain modes.

In dataflow-aware mode, an exact completed initializer's positive operation-domain
coverage plus its lexical operation receipt may discharge only the
classifier-derived `opaque_at_init_call` fallback. Canonical primitive literals
and checked deferred callable creation supply such operation coverage through
selected local calls. The callable body's later execution is checked separately.
Other havoc, observed reads/writes, callee heap opacity and strict-mode purity
remain authoritative; completed timing or an empty access ledger is insufficient.

Statement purity has separate preparatory and completed facts. Current-owner
proof preparation retains the existing classifier, including enum initializers.
The final verdict is published after assignment/export evaluation, exact
existing certificate discharge and heap aggregation, before the next source
prefix. Graph and at-init consumers use that completed verdict; initializer-only
trust retains callback fallback roots. Complete synchronous timing does not
prove evaluation purity. A classifier-pure/completed-impure disagreement is
reported with its source site and per-chunk count and is an e2e bug.

Array member writes, method calls such as `push`, escaped arrays, and calls
whose callable body has no direct graph summary remain opaque heap barriers.
The shared synchronous-call analysis also makes a source unit an opaque heap
barrier when it reports incomplete execution effects, regardless of a purity
hint. This covers observations through unsupported coercion or property-access
protocols, and unresolved synchronous calls, without duplicating the call
analyzer. Key enumeration (`for-in`), optional calls, and tagged templates are
also barriers when their array effects are not represented precisely. Barriers
constrain the preceding array-content frontier and later array observations.

An exact whole top-level array literal initializer is a one-shot eager
allocation. A precise observation of only that cell does not inherit an older
opaque frontier, because the cell did not exist when the older operation ran.
This exception requires a unique eager allocation source and no lazy occurrence
of the same cell identity. Repeated allocations inside callables, other
observed cells, and the barrier's ordinary non-heap effects keep their order.

Purity and binding-effect completeness do not prove heap locality or argument
nonretention. Native calls consuming array literals remain heap barriers once
tracked arrays are live. Checked implicit local construction has a separate
source-derived heap capability: admitted fields only initialize fresh instance
state, and native fields require an explicit fresh-allocation model under the
shared native identity/integrity guard. Only an exact certified `new` site omits
its own opaque heap effect and constructor-body promotion. Other calls or
constructors in the same statement, including another invocation of that callee,
keep their own effects. Callee and argument evaluation, binding initialization
order, and deferred method/callback effects remain independently constrained.
When native receiver refinements condition this heap certificate, the exact
construction source unit reports the consumed input references in
`heap_native_integrity_uses`. Source-only certificates have no such use, and
configured inputs that the proof did not consume are not reported. Empty uses
are omitted from JSON and default to empty when reading older reports.
This adds no author flag or purity exemption.
The census tracks `ArrayLit` source origins, not arbitrary arrays returned by
`Array()`, `Object.entries`, or foreign imports; it does not claim full heap
ordering coverage.

These are scheduling dependencies only; they do not certify call purity,
no-reentry, property-descriptor integrity, or prototype integrity.
`trusted_dataflow_summaries` does not weaken these barriers. Unrecognized or
ambiguous origins remain conservative rather than receiving a precise
per-array ordering claim.

## Authoring reports

Each per-chunk `modules.json` may include `imported_callables`, keyed by the
importer's local binding name. A `callback_factory_evidence` entry names its
defining module and export, with `origin: author_assertion` for an audited
contract or `origin: inferred_immutable_wrapper` for a checked wrapper. The
inferred entry also names the defining local binding and, when available, a
standard `source_location` (`source_path`, `start_line`, `end_line`,
`start_column`). This is a return-only callback capability, independent of
call purity. The field is omitted when no bare imported callable was resolved.

General imported call and returned-record summaries require a separate
availability proof. Besides existing declaration summaries, a direct const
function/arrow initializer is supported when its provider contains exactly that
one declaration and local export aliases, with no retained provider captures.
After splitting, the initializer destination remains a dependency leaf, so the
resolved importing binding reaches an initialized value. This proof is independent
of the callable body's invocation, return, and callback timing proofs. Imports,
reexports, namespace/member aliases, additional executable statements and mutable
initializers remain unsupported for this initialized-provider case. Their calls
keep conservative dependencies; source-module acyclicity alone is insufficient
because splitting may change the evaluation graph.

Source-certified immutable wrappers may also supply `callback_object_arguments`:
ordinary allocated argument positions and checked direct callback uses. An
anonymous inline consumer without self-identity access completes entry
coverage. Author timing assertions do not grant this capability. The
canonical native census uses it only for supported shallow conditional regions
on stable parameters in an anonymous inline callback or its immediate anonymous
IIFEs. Discharged member-write witnesses retain the original source site,
empty `affected_roots`, and `unreachable_branch` evidence naming the defining
wrapper, consuming import, wrapper call, callback and guard locations. Deferred
nested bodies and unsupported contexts keep their conservative witnesses.
This does not change purity, callback effects, exceptions or loader return
property reads.

Full dependency and program graphs are written directly as deterministic
gzip level-1 compact JSON, without a plaintext staging file. Saved-graph
commands require gzip input; corrupt, incomplete or plain JSON graph files fail.
Small reports and report selection retain their existing behavior.

Each `dependency_graph.json.gz` written by `debundle run` references the run's
`program_graph.json.gz`, which carries input provenance:
SHA-256 of the executable engine and the exact bytes read for the tree config,
vendor marks, module YAMLs, optional binding patches, JS list, every listed JS
chunk, and vendor package files consulted by planning. Module YAML path
membership is recorded separately, including paths outside the graph chunk.
The provenance also retains a canonical runnable argv with resolved paths and
effective env-provided values, the original cwd, logical root paths, and the
config's chunk-to-module-tree mapping. A graph created before this metadata
existed remains a historical report only.

`translation_validation.json` is a bounded diagnostic view of the unchanged
edge-based translation validator. `gate_accepts`, `accepts` and `swapped_pairs`
are exact. `counts` contains exact totals for conflicts, forward-kept,
forward-swapped and placement-split edges, plus conflict totals by dependency
kind. Each corresponding list is `{total, examples, truncated}` with at most
twenty deterministic examples. Legacy comparisons use complete
category totals; effect-set comparison receives the complete legacy conflict
pair set, including pairs beyond the samples. Forward-swapped edges retain
their existing report-only role. Bounds never narrow checks or source scope.

`run --full-report translation-validation` additionally streams every result
from that same checker to `translation_validation.full.jsonl`. Rows have a
`category` of `conflict`, `forward_edge_kept`, `forward_edge_swapped` or
`placement_split`; conflict rows retain both statements, dependency-edge key
and kind, while other rows retain their dependency-edge key. The default does
not write this stream. Repeatable `--full-report-chunk` selects only full-report
sinks and validates configured materialized chunk IDs. Full diagnostic evidence
and full graph requests are independent. `full_detail_export_available` and
`full_detail_export_command` advertise the capability, not a completed file.
An active stream must finish and explicitly flush before its sibling summary
is written; any write/flush failure fails the run. Existing invalid-spec,
accepted-dry-run, typed gate rejection and final-program checks remain intact.
Bazel `full_reports` and `full_report_chunks` attrs forward the same selection
only to the final Pipeline action; analysis artifacts/projections are unchanged.

Every final candidate is assessed against its exact emitted order and structural
placement, including runs that suppress reports. Report selection controls the
serialization and full-detail sinks, not which assessment is computed. The
joint effect and structural assessment authorizes relocation. It preserves
structural and rebinding obligations and rejects uncovered crossings; an
incomplete owner can remain accepted when its effects do not cross another
nonempty statement.

The assessed assignment retains explicit named-module ownership and the
configured `unassigned_mode`. A rebind constraint never implicitly moves an
unclaimed writer or declaration into a named module. A partial claim that
leaves the other endpoint elsewhere is assessed as that split and refused;
the author must claim the complete group or retain its shared fallback destination.

Full per-chunk report emission also writes
`effect_translation_validation.json`: effect conflicts for the emitter's exact
statement order, derived from staged facts. The same assessment is mandatory
for relocation validation even when report files are suppressed; legacy graph
and edge-TV verdicts are diagnostic comparisons. Unknown global/property/host
coverage remains conservative Havoc independently of purity or synchronous
completeness. Counts include every conflicting swapped pair and existing
pair-premise disposition; observed contradictions block acceptance.
Local export lists (`export { value }`, including aliases) link live bindings
without evaluating their values or initializing a binding. Their initialization
effect footprint is empty. Dependency-bearing re-exports (`export ... from`)
retain conservative dependency-evaluation coverage; default export expressions
and declarations retain their normal evaluation effects. Structural facts,
heap and synchronous analyses, and saved analysis share this classification.
Twenty deterministic examples bound default output: rows with modeled cells
are preferred, then the earliest Havoc-only rows fill unused slots; selected
rows retain sweep order. Truncation and coverage limitations remain explicit.
`run --effect-conflicts-jsonl` additionally streams every pair, cell, access
provenance, premise disposition and endpoint opacity
to `effect_conflicts.jsonl` in each chunk's report directory, using the same
enumeration. Write and flush errors fail the run. Missing report roots and
accepted dry runs that suppress reports refuse an explicit export request;
rejected dry runs retain their existing report policy and can export.
Repeatable `--effect-conflicts-chunk` requires the export flag and validates
names against configured materialized chunks. It filters only S4 JSONL sinks:
source inputs, analysis/census selection, graphs and bounded summaries are
unchanged. With no filter every materialized chunk can export.
The Bazel pipeline exposes the same opt-in through `effect_conflicts_jsonl`
and `effect_conflicts_chunks`; only its final run action receives these flags,
and streams remain inside the existing reports tree.
Full per-chunk report emission also writes
`candidate_relocation_assessment.json`. It pairs the effect decision and exact
statement order from the existing single sweep with typed cross-destination
rebind and split-atomic-group obligations from the same assignment validation,
plus typed source/destination projection evidence from the final plan.
Candidate assessment and normal lowering share one checked projection over the
retained source-unit layout, whole-statement requests, binding catalogue,
destructuring and nested-`var` groups, and stable Set intervals. Its maps are
available to lowering only when the projection is mappable; a projection failure
for a finalized assignment that reaches this seam can still report typed
blockers and unresolved checks without manufacturing an emission placement.
Conflicting destructuring and nested-`var` requests are retained through plan
construction and checked here against the hygienic per-binding ownership
catalogue, so candidate assessment and normal emission share their typed
placement decision. Request planning uses the same destructuring owner
classifier to retain existing fixed-point pulls of unclaimed or residual-owned
siblings when a group has one explicit owner; split requests remain intact for
final projection. Nested-`var` planning and projection share the classifier over
retained statement identities, explicit binding owners and whole-statement
requests. Consistent whole-statement routing remains unchanged.
Semantic-order SCCs remain distinct from structural causes such as rebind,
co-declaration, co-location and local effects; mixed or unclassified causes
remain blockers. `placement.projection_violations` retains typed identities for
structural inconsistencies found by those checks. `placement.coverage.unresolved_checks`
names the structural checks whose complete input domain was unavailable:
destructuring-declarator grouping, source-unit-to-body mapping, binding and
shared-var destination projection, and whole-statement destination projection.
Completeness is derived from an empty unresolved set. `accepted_within_checked_scope`
requires an accepted effect decision and all four placement checks complete
without a known structural blocker and with emitter maps available. This is
the relocation verdict; vendor, export, and final-program checks still apply. The candidate
status names effect and placement blockers separately. An ordering-only veto
from the legacy graph is reported by `legacy_gate_ordering_veto` and does not
count as a structural blocker or a second emission veto. Only the joint accepted
assessment mints the immutable assignment capability consumed by lowering.
Rejected output requested with `--emit-on-rejection` remains marked inspection
material and exits with status 3.
Full semantic precision remains incomplete; see
<docs/wire_format.md#translation-validation>.
The report's `full_semantic_precision: false` is a policy-level field, not a
computed per-chunk completeness verdict. Interpret each chunk through its
unknown-coverage and refusal counts and its actual conflict rows.

The typed effect relocation decision is scoped to statement-order commutation
for the exact emitter order. `accepted/no_reordering` proves only that this
candidate order has no statement inversion. For a reordered accepted
candidate, every inverted pair has closed effect domains or crosses a
statement proved to have no evaluation effects. Open effect sets are Havoc
against every nonempty partner, so an open statement crossing a proved-empty
statement needs no assumption about the open side. An uncovered modeled
may-conflict or contradictory premise is `conflict`; an uncovered Havoc pair
is `incomplete`. A pair covered by an exact authored commuting premise can
accept conditionally as `assumed_statement_order_commutation`; its typed
premise dependency remains explicit. Observed contradictions still block. The assessment
retains typed `relocation_dependencies` for covered owners among inverted
participants and typed `premise_dependencies` for premises used by conflicting
pairs. The effect component alone does not establish structural placement, vendor
replacement, privacy or singleton obligations.

All ownership-changing CLI consumers assess their exact complete in-memory
candidate through the normal selector, analysis-load and `ChunkPlanBuilder`
path. Saved graph claims remain selector and identity evidence; the graph report
is never decoded as effect facts. The engine, source digests and completed
analysis projection must match before assessment, and failures leave YAML
untouched. Full context supports exactly one materialized graph chunk and a tree
config whose full module tree belongs to it (empty or absent `module_roots`),
with `--modules` and `--tree-modules` resolving to the same root. Auxiliary tree
inputs must be explicit. Scoped graph-only proposals remain available as
previews requiring full assessment.

Assign/unassign assess the complete post-batch documents. Merge applies every
in-memory target document and removes drained sources. Delete removes the exact
resolved module paths and still requires `--force` for non-empty modules.
Apply resolves selected proposal members and structural selectors; refine
resolves the complete requested source-unit map. Each command retains these
claim checks, then uses the same joint relocation verdict before writing. A
rejection carries the actual typed candidate assessment in JSON and text;
legacy SCC and atom comparisons do not veto an otherwise accepted candidate.

`coverage.owner_coverage_refusals` counts typed reasons why an original
owner's whole-initialization certificate was not sealed. Bounded examples
include an optional refusal and existing source location for each endpoint;
JSONL carries the same refusal inside each endpoint's opacity record. Reasons
identify only the refused proof boundary, never source bindings or expressions.
They explain the report and do not change the certificate or coverage.

Canonical runtime transfers additionally compose static global-property
may-accesses into these reports, retaining lexical source-operation sites,
proof dependencies and conservative callback assumptions. They do not alter
legacy global cells or graph edges. Indexed, conditional and prejoined lanes
remain may-access evidence and cannot establish observed contradictions or
domain completeness. When an ordered plan stops, its remaining direct local
callees retain indexed may-access sites through their settled module bindings
and captured lexical values, without executing those callees. These accesses
carry the fallback assumption; they cannot seal the stopped initializer.
Unchecked owners retain their unknown-coverage obligations.
Default examples retain twenty source operations per cell access, with exact
counts and explicit truncation; full JSONL borrows and streams all retained
operation provenance.

An original, non-exported direct class declaration may close global-property,
general-heap and host-effect coverage only for an inert class shape: no
heritage, class or member decorators, computed names, static fields or blocks,
accessors, auto-accessors, private members, or ambient declarations. Its
members are plain methods, an ordinary constructor and public instance fields.
Method and constructor bodies and instance field values remain lazy. The exact
class evaluation must finish and initialize its binding before the certificate
is sealed. The certificate covers class declaration evaluation only; later
construction and property observations retain their ordinary effects and
coverage requirements. The existing binding write and allocation facts remain
projected.

An original simple variable declarator with an unannotated
binding may close global-property, general-heap and host-effect coverage when
its initializer is a primitive literal, checked JavaScript
function/arrow creation, a read of an initialized module-local binding, a
regular-expression literal, or an expression covered by the shared value
transfers. Covered expressions compose primitive literals, initialized
module-local binding reads, static ordinary records, dense arrays, sequences,
strict equality (`===`/`!==`), `void`, logical-not and terminal-arm conditionals.
Conditional arms are restricted to direct binding reads or primitive values;
compound unsupported arms remain uncovered. Record values and array elements
compose the same transfers. Identifier, string and numeric data keys and
shorthand fields are supported; array
holes, computed keys, spreads, accessors, methods, colon-form `__proto__`, and
other unmodeled operations remain uncovered. Creating these own data fields
invokes no receiver getter, setter, user-defined coercion or callable body.
The only admitted unary operators are non-invoking: `void` discards its
evaluated operand, while `!` applies JavaScript's non-invoking `ToBoolean`.
Every evaluated operand, including a discarded sequence operand, composes its
access/coverage judgment and possible abrupt outcome. A normally completing
last value does not discharge an earlier unproved read or throw.
In a supported ordered callable body, an untagged template evaluates each
substitution once and converts it to String before the next substitution.
Known non-Symbol primitives use the scalar conversion transfer. Symbol
alternatives retain possible abrupt completion; object or unknown values
retain ordinary coercion fallback and cannot close operation coverage. The
String result alone never certifies the substitution reads or effects. Tagged
templates remain outside this transfer.
All operand binding reads remain subject to the initialized-local check. Array
creation does not consult the global `Array` binding or iterate the element
values. Every source binding read, including one in an overwritten duplicate
field, must already be initialized before the root transfer. No additional
heap privacy or native-slot premise is used. Allocation effects remain in the
report.
For a regular-expression literal, ECMA-262 §13.2.7 validates the pattern and
flags as an early error and evaluates a valid literal through `RegExpCreate`
with source-derived strings. That operation does not resolve or call a mutable
global `RegExp` binding or execute user code. The certificate requires the
shared allocation transfer to return the exact `Kind::RegExp` root allocation
and finalizes only after the declared binding is initialized. The abstract
allocation identity does not establish RegExp internal slots,
prototype/method integrity, `lastIndex` behavior, or effects of later property
reads, getters or calls. RegExp identity does not authorize ordinary-record
property proofs: reads of inherited properties (including `source`), own data
properties (including `lastIndex`), missing properties, and custom own or
prototype accessors remain uncovered unless a separate native or descriptor
proof applies. A property used as a call target also retains its independent
callee and invocation proof obligations.
`RegExp(...)` and `new RegExp(...)` remain unsupported because they evaluate a
mutable constructor binding and can invoke user code. Under the existing
[A6 input contract](https://github.com/agentydragon/ducktape/blob/devel/devinfra/js/debundle/docs/design.md) that the original chunk runs to
completion, its literals satisfy ECMAScript's early-error rules and use the
non-abrupt `RegExpCreate` completion. This does not assert that our input parser
independently validates every regexp grammar or flag restriction.

[ECMA-262 §13.2.7](https://tc39.es/ecma262/2026/multipage/ecmascript-language-expressions.html#sec-regexp-literals)
and [§22.2.3.1](https://tc39.es/ecma262/2026/multipage/text-processing.html#sec-regexpcreate)
specify the literal evaluation and creation operation.

The binding read must resolve to a declared local cell, excluding imports and
host globals, and the interpreter must check its initialization before the
read. A normally completed read retains its binding effect regardless of the
value's kind or number of abstract alternatives; it establishes no heap
privacy, value immutability or absence of getters on that value.
Ordinary and named exported variable declarations use the same initializer
transfers and owner checks. For a comma-split declaration, the canonical
item view retains the original parent and selected declarator index. The
source collector checks that exact original child against its split analysis
view before expression erasure; every declarator remains its own effect owner
in source order. A dedicated interpreter request must produce its expected
value without an unavailable alternative, and owner finalization must complete
the binding initialization without fallback or precision loss before
sealing the coverage certificate. Function and arrow parameters, including
defaults, destructuring and rest, remain lazy together with the body. Async
and generator function creation follow the same rule. This certifies no call:
later parameter evaluation, iterator/getter access, bodies and scheduling
retain their ordinary effects and coverage requirements.
Assignments, templates, default export expressions, destructured variable bindings, class expressions,
imported/global/uninitialized binding reads, decorated/type-annotated
functions remain uncovered. For class declarations,
all other shapes remain uncovered. Parentheses alone preserve admissibility for
variable initializers.
The source-covered expression is retained from canonical lowering. Dedicated
initializer execution uses that exact expression and shared child transfers;
generic values or an empty ledger cannot mint coverage. Composed child judgments
never propagate a whole-owner certificate, and invocation memos carry none.
The owner certificate survives staged analysis. Binding/allocation cells, other opacity
flags and purity retain their existing rules; the joint relocation assessment
consumes the resulting effect coverage.
Coverage certificates retain the composed transfer's typed proof dependencies,
including exact-site native identity inputs. The effect report projects their
union as `coverage.covered_owner_dependencies`; an empty access ledger does
not erase the conditions of an absence proof.

An ordered simple variable initializer can separately compose effect-domain
receipts from exact initialized module-local cell reads, canonical primitive
literals, and direct ordinary top-level function-declaration calls. Actual
creation and successful initialization of noncaptured parameter and local
`let`/`const` cells have no global, heap or host effects. Arguments, selected
defaults and declaration initializers compose their own effects separately.
Successful static own-data reads and writable own-data writes also compose
these domains after the shared slot proof. Their actual property cells remain
in the heap ledger; closed coverage does not make a heap access effect-free.
The receipt retains current-prefix proof dependencies, while receiver, callee,
argument and RHS evaluation compose their own effects. Accessors, missing or
inherited slots, unsettled receivers and unproved writable descriptors do not
receive this coverage.
A canonical in-range array-index read uses the same own-data transfer when the
current prefix retains a settled, first-generation dense array-literal witness
and the actual stored element value. Source operands are not reevaluated to
recover that value. Holes, unsupported spreads, out-of-range indices, pooled
allocation reuse, mutation and accessors retain fallback. Provider-only and
retained queries require their own prefix witness; the read proof grants no
array-write capability.
When the same exact root has both a lexical operation receipt and complete
global/heap/host coverage, its represented effects discharge the relocation
projection's `opaque_at_init_call` and unattributed `member_write_or_alias`
fallbacks. The coverage dependencies include both receipts. Actual reads/writes,
independent dynamic/global-alias opacity, incomplete execution, precision losses
and opaque result contents remain.
Legacy completion and source-only assertions cannot discharge this fallback.
The report's legacy dataflow-opacity count still describes the original
classifier; pair diagnostics show any remaining opacity.
The invocation prologue must use no `arguments`, implicit receiver or captured
cells. The existing ordered kernel composes each executed operation's receipt,
independently for global properties, general heap and host effects; missing
receipts remain unknown even when execution is complete or the result is known.
The returned value may remain allocation-opaque. An actual call receipt also
retains the settled prefix's typed value-proof conditions and current
target-selection conditions as prerequisites; those conditions alone cannot
create coverage. Child outcomes and invocation memos carry only nonterminal
receipts. Only the root owner can seal normal,
fully covered execution against its exact canonical owner, binding and
initializer span after its result binding becomes initialized. Captures,
later local stores, other property/native/global operations,
unsupported transfers and abrupt completion remain uncovered by this route.
A non-exported comma-split variable declarator may also use this route when its
checked original initializer composes to one plain binding read. Its canonical
split view must match the original parent declarator and initializer span, and
the shared local interpreter's result supplies the binding value before any
original-initializer projection. The source read therefore executes once.
Coverage still requires an initialized local cell or the existing imported
primitive-read proof. Mutable imports, globals, uninitialized or TDZ cells, and
other split initializer forms do not gain this route; their existing
leaf/value admission and fallback remain unchanged.
When an ordered root initializer has both normal values and an abrupt
alternative, only its normal values enter the module binding on the normal
continuation. Its effects remain incomplete. An initializer with no normal
result retains its unavailable value and cannot certify a later binding read.
Checked deferred function/method/accessor creation composes the same creation
effects in application execution and source-provider queries. Canonical
creation identity, capture availability and retained references remain checked;
the body and parameter defaults are not executed. A factory can obtain a
complete body-effect fragment from these operations without proving any later
invocation of its returned callable. Missing operand or other body effects
still prevent whole-owner coverage. Fresh record creation separately covers
static method/getter/setter descriptor definitions when their ordered canonical
tags match; each callable child still supplies its own creation fragment.
Defining an accessor does not invoke it. Computed keys, opaque definitions and
the data-property `__proto__` form remain outside this definition proof.
Base-class provider queries also retain their actual shared constructor-body
fragment independently of the fresh-instance and retained-argument proof. The
shared kernel executes field initializers, including implicit `undefined`
definitions, before the simple parameter prologue and constructor body. A fresh
ordinary receiver and a normally completing allocation-only prefix prove public
own data definitions, without consulting inherited setters. Private definitions
install class-qualified slots; a private assignment requires its exact slot
already present. Public `this.field = value` assignment requires the exact public own data field
already present and writable at that prefix. It evaluates its RHS once and keeps
its effects; missing own fields and prototype setters remain unsupported.
Public descriptors and private slots remain disjoint. Stored values retain weak
pooled-allocation semantics and callback reachability.
Each initializer/RHS and actual parameter operation composes its own effects.
These proofs grant no field-read, result-shape or prototype guarantee. Heritage,
defaults, borrowed access and instance private methods remain unsupported.
Checked array-literal creation also composes the same operation effects in
both contexts: creating the fresh array and defining its own indices and length
executes no user code. Each element's effects compose in source order. Holes
remain absent own properties and receive no dense-array or iterator proof. A
spread element must obtain the existing native iterable receipt for its exact
operand and source span. Exact yielded slots can preserve dense-array knowledge;
qualitative primitive-only yields retain no exact slots and are recorded only
for a fresh allocation site with the receipt's dependencies. Named function
self scopes and `var`/function hoisting retain their existing admission;
successful allocation alone does not close their effects.
Existing leaf/value initializer proofs keep their independent admission.
These three domains do not establish completeness of source binding reads or
rebinds. Closing them does not discharge unresolved-call closure or authorize
skipping its binding-effect fallback.

The ordered root can separately seal lexical read and rebind coverage from
receipts produced by the shared local-operation interpreter. Binding initializers
and supported anonymous expression statements retain their exact source owner,
expression span and root kind in the same canonical root plan and receipt.
Expression statements discard their value after executing their child operations;
they create no artificial binding. Both root kinds use the same property, call
and operator transfers, completion and dependency checks, and call-edge
connectivity proof. Supported global-store expressions also use the ordered root
plan. An unproved global store retains incomplete execution, an uncovered root
authority and conservative fallback; global lookup effects remain independent.
Initial name lists cannot
close their independent descriptor and host effects. Validated whole-expression
publication and metadata models retain their timing/capability premises without
acquiring operation coverage. Covered lexical
operations include initialized module-cell reads, canonical primitive
literal operations, exact source-checked deferred-function creation,
successful static own-data reads (including direct binding initializers), and guarded
own-data writes through initialized non-imported module bindings. A module
slot write requires separate monotonic writable-at-prefix evidence from its
literal definition; an own-data read or current value alone does not prove
writability. Freeze/integrity changes, descriptor changes, taint, escape, or
unsettled reach invalidate that evidence. A successful selected direct local
call records its exact caller body, source call-site, target body, and
consumed prefix prerequisites, including each memo-reused use. A static
module-slot callee retains its GetValue receipt before argument evaluation.
Closure-allocation root initializers use the same kernel and retain the actual
creation's callable-selection receipt when publishing their binding; a stored
function value alone cannot establish that provenance. Creation defers bodies
and parameter prologues even for opaque, async and generator callables; their
later invocation must independently establish its operation effects.
Capturing an available lexical cell does not read its value. The shared kernel
records actual cell creation, initialization, initialized reads and mutable
stores for simple captured parameters and local `let`/`const` bindings. Effects
address `lexical_cell` by the canonical declaration offset, pooling all
activations of that binding. This identity supplies no private-birth,
strong-update or unique-activation proof. TDZ, possibly initialized cells and
immutable stores retain their abrupt or uncovered result. Callable selection
travels only with its actual stored receipt; mutation or mutable-cell havoc
discards it. A settled present own-data read does not
require fresh-local ownership, while mutation retains its separate ownership
and writability checks. Accessors, proxies and receiver TDZ keep fallback.
An inferred native callee whose canonical builtin row declares
`NoExteriorAccess` likewise snapshots its exact intrinsic and prerequisites
before argument evaluation. The same kernel evaluates every operand and consumes
the existing row transfer once. A native call edge records its intrinsic family
and property in the lexical receipt. Only checked nonthrowing primitive coercions
close this operation; primitive return kinds, identity alone, authored identity
inputs, or unknown body effects do not. Direct, zero-argument construction of
an inferred intact `Map`, `Set`, `WeakMap` or `WeakSet` also uses the shared
allocation transfer and composes its effect fragment through this kernel.
Its lexical receipt records a distinct native-construction edge. The adapter
checks the constructor's global binding separately and records that read;
the fresh allocation alone supplies no operation coverage. Application execution
keeps its conservative opaque value projection until a corresponding native
heap and retained-reference model exists; a private provider allocation identity
does not supply those capabilities. Other argument forms, outer-spread
arguments, aliases, shadowed and replaced constructors retain conservative
paths.
Other native proof families retain their existing paths. Anonymous global publication still needs its own root and
descriptor/host proof.
Ordinary simple parameters initialize from already evaluated arguments in
source order; supported selected defaults execute once in the same kernel.
All parameter cells exist in TDZ before defaults run, and each successful
initialization and initialized parameter read has its own lexical receipt.
Ordinary local `let`/`const` declarations likewise compose receipts
for actual TDZ-cell creation, successful initialization and subsequent reads.
Initializer effects compose separately; an abrupt initializer never marks its
binding initialized. Prebound values do not establish these receipts. Local
declaration receipts do not certify `var`/function hoisting
or an initializer's other effects, and cannot manufacture callable
selection evidence from a stored value.
Default-bearing bodies with body `var` or function declarations retain the
compatibility path until separate parameter and body environments are modeled.
Rest/destructuring, `arguments`, async and generator
prologues remain outside this receipt family. Argument evaluation keeps its
own effects, including surplus arguments.
The root checks that its exact root call-site set matches and that the flat
call edges connect back to its exact body without expanding root-to-leaf paths.

Ordered root execution also includes an exact initializer containing one
ordinary call through an initialized binding or checked static own-data member.
Its callee and actual arguments retain independent operation coverage. Its actual call result
travels with the child effects through the shared local kernel; the old
effect-only call and later value projection do not execute alongside it.
An actual own-data read or deferred callable creation can retain a callable
selection receipt through argument binding, parameter reads, returns and
exact ordered root publication. A later initialized-binding read may consume
that receipt only when its actual value still matches. Invocation memo keys
include argument selection evidence, so equal values without the same evidence
cannot reuse a proved result. This receipt describes the selected abstract
callable, not fresh allocation or JavaScript reference equality.
An alias obtained only by flat value projection has no such producer;
singleton values and normal completion therefore keep syntactic fallback.
Member, native, imported and constructor single calls
retain their existing admission paths.

An exact top-level `Object` own-data function-reference read can consume its
existing native binding-and-slot certificate in the same ordered read transfer.
The certificate covers the global receiver lookup and property read; its inferred
or exact-site author dependencies remain on the root receipt and relocation
decision. The returned value keeps its ordinary opaque projection. Shadowed
receivers, computed keys, missing native evidence, provider-only queries and
retained suffixes keep their ordinary read paths. Reading the reference certifies
no later invocation through it.

Within those existing ordered-root profiles, a direct function or arrow
expression may be the callee. The local kernel creates that closure before
evaluating arguments, and the call transfer requires the selected singleton
closure's source key to match the canonical `Alloc(Function, key)` at that
call-site. This extends the callee form only; it adds no root-initializer
profile. Existing callable-body, parameter-prologue, capture, synchronous
callable, supported-operation, exact-call-site and normal-completion checks
still govern receipts. Named function-expression self scopes remain unproved
because this route does not instantiate their `NamedSelf` cell; runtime
execution alone does not produce an operation receipt.

If that exact single-call root receives an incomplete local child result, it
retains the child effects and losses, unknown coverage, and unavailable/unknown
return possibilities without inventing a caller remainder. Only a singleton
ordinary nonopaque, nonasync, nongenerator target can use this terminal stop.
Uncovered compound roots and suffix operands retain their conservative remainder path.
When the child's complete retained-effect proof covers every loss and its
result has a nonempty primitive envelope, the caller may instead continue
through the shared kernel. Each following operation must independently prove
its operands, normal completion and effects; the child remains incomplete and
supplies no concrete result value.

At an actual invocation-depth loss, a retained-effect query may close the
suffix through the same ordered operation kernel and prefix-read adapter.
Its worklist is bounded by the existing invocation expansion limit and refuses
active targets, cycles, unresolved selection, captures, parameters, globals,
imports, property operations, writes and unmodeled operations. Terminal returns
may compose direct zero-argument calls, primitive literals and initialized
module-cell reads through binary and sequence operations, with no activation
bindings. The shared operator transfer must prove normal completion without
user coercion; syntax admission alone supplies no coverage. Every reached
operation must supply complete lexical/global/heap/host coverage. A primitive
return envelope is checked separately; a returned callable or heap capability
keeps conservative fallback. No application prefix is mutated by a failed query.

The exact ordered root can then seal a distinct `RetainedEffectClosure` with
its lexical call/read receipt, all-domain dependencies and the exact covered
loss sites. `complete` remains false and the original depth loss remains in
diagnostics. This proof neither creates `CompletedInitialization` nor supplies
a normal execution value. The graph consumes represented reads and skips only
the redundant syntactic closure. Effect relocation consumes the same root
proof, retaining raw execution diagnostics while removing uncertainty covered
by those exact loss sites. Other opacity, unknown domains and heap effects
remain independent.

The lexical read and rebind domains retain the conditions used to select each
operation. Values, `complete`, and the existing three effect domains cannot
mint lexical coverage. The root identity is installed before ordered
execution. Sealing additionally requires the exact source owner, binding and
initializer span, a final initialized binding, complete execution, no unknown
call, precision loss, uninitialized read, or fallback-only lexical effect.
Unproved callable aliases, accessors, imported or uninitialized receivers,
activation/formal borrowed stores, unwritable stores, unsupported
prologues/transfers, and abrupt completion remain uncovered. Any failed
condition leaves the selected root explicitly uncovered and keeps the
syntactic fallback. Children and memo entries cannot seal a parent.

The graph first emits all existing observed and assumed read/rebind
contributions, then skips only its remaining syntactic call fallback when an
ordered-root lexical receipt or complete retained-effect closure seals. This migration applies only to source
owners that actually enter ordered root-initializer execution. Applied
`initializer_binding_effects: source_only` remains explicit author authority.
Owners outside the migrated path temporarily retain the named
`LegacyExecution` decision, where the existing completion boolean is still
used; that label is transitional compatibility, not operation evidence.
`Unclassified` owners and selected roots without a receipt retain fallback.
The engine-owned staged fact stores this authority as a required externally
tagged `BindingEffectCoverage` value. A missing field is rejected on artifact
load rather than reconstructed from completion. The public
`synchronous_execution.binding_effect_authority` report is a separate
diagnostic DTO and is never an analysis input.

An original simple variable declarator may also close those coverage domains
for direct bare `new Map()`, `new WeakMap()`, or `new Set()` with exactly zero
arguments. The original AST and lowered `New` site must identify the same
constructor. Map and WeakMap use the checked shared zero-argument collection
operation to return normal global-property, general-heap and host-effect
coverage; only the root source owner can seal it after its binding is
initialized. Set retains its dedicated root transfer. Each requires the
matching native Construct/Fresh/Never row with no present coercions, writes or
invocations. Their exact identity premise comes from the existing intact
intrinsic census or an accepted exact-site `map_empty`, `weak_map_empty`, or
`set_empty` input. The transfer result stays `Top`: it proves successful
initialization of this owner, not a Map, WeakMap, or Set identity, contents,
prototype, or method behavior. The global constructor read and lexical binding
write remain projected. The constructor's internal fresh allocation has no
separately modeled effect cell; its identity becomes available only at the
binding write, and later observations remain opaque or need their own existing
proof. Existing direct-NewTarget and fixed-prototype boundaries still apply.

The same root-owner certificate admits a direct zero-argument
`new TextEncoder()` when its exact Construct/Unknown/Never row is selected with
no coercions, writes, invocations, or mutations. Identity must come from the
intact TextEncoder census or the accepted exact-site `text_encoder_empty`
input. The successful result remains `Top`; the certificate does not establish
an ordinary-object shape, TextEncoder identity, methods, prototype behavior,
or later property reads. The platform object's allocation has no separate
effect cell, and its identity becomes available only at the binding write.

It also admits a direct bare `new Set([primitive literals])` initializer only
when the original source owner and lowered `New` operand identify the same
dense, nonspread array literal. The exact request must apply the native Set
Construct/Fresh/Never row with its `0..=1` arity and this one explicit array
argument; its one `Iterate(arg0)` step must be discharged by the existing
`SetIterableProof` at that call site. The proof use records its native-operation
input or inference dependencies. Native global identity, lexical reads, array
argument evaluation, throws, and owner initialization keep their normal checks.
The successful result remains `Top`: it proves neither Set identity nor
contents, prototype, or later method/iteration behavior. Array aliases,
identifier elements, nested values, spreads, wrapped array arguments,
call/sequence/assignment wrappers, and other iterables remain outside this
initializer form and retain conservative fallback.

The `map_empty`, `weak_map_empty`, `set_empty`, and `text_encoder_empty` forms accept only
zero-argument constructors. A present argument (including `[]` or `undefined`),
spread, aliases, qualified constructors, sequence/assignment wrappers, shadowed
or replaced globals, and other shapes remain outside those forms; use another
explicitly specified input form when applicable.

The ordered local root also admits a direct `new Set(arrayLiteral)` whose array
contains a spread element, when the root's exact split-initializer identity
matches the original binding and initializer span. It evaluates array elements
in source order through the shared array transfer, requires the native iterable
receipt for each spread, then consumes the same Set Construct/Fresh/Never row
and `SetIterableProof` for the constructor's array argument. The constructor
receipt seals only after normal complete root execution. Native operation input
or inference dependencies remain attached; native global identity, lexical
reads, heap-content dependencies, interval checks, throws, initialization
identity, and fresh allocation-site checks retain their existing guards. The result proves this owner's ordered
construction and allocation origin, not the Set's contents, prototype, or later
method behavior. Outer-spread arguments, aliases, wrapped expressions, stale
allocation sites, and any unproved iterable keep the conservative path.

A command that combines a saved graph with current spec or source checks the
recorded baseline before reading claims or writing edits. It refuses changed,
added, deleted, or mismapped inputs and reports the mismatch with a regeneration
command that uses a fresh output directory. The recipe translates an output
root when a supplied current spec, source, or package root maps it. If no
supplied relocation root maps the output and a current spec root is supplied,
it chooses an unused `debundle-output-regenerated` directory under that spec
root, adding a numeric suffix on collision. Without a current spec root, an
unmapped output uses an unused `-regenerated` sibling beside the recorded
output, adding a numeric suffix on collision. An accepted edit
evaluates its candidate assignment against the saved dependency analysis; the
graph remains the **pre-edit** snapshot and the next graph-backed edit requires
another `debundle run`. A batch edit checks one
starting snapshot and one combined candidate. Graph-only historical views do
not certify current input compatibility.

`inspect-source --id unit:...` resolves that exact graph node without current
module claims. It skips the authored module-scope check, allowing flat-spec
graphs, while retaining captured-input, engine, and source identity checks.
Other graph-backed selections and edits require the authored scope.

Each graph chunk maps a logical `source_path` to the physical `input_file`
whose bytes were parsed. Selector resolution keeps the logical path as its
parser identity and uses source bytes retained by graph verification. A
logical alias may have no file at `--source-root/<source_path>`; the verified
chunk mapping still identifies its input. When `--source-root` is supplied, it
must contain the resolved physical input, and any existing logical-path
candidate beneath that root must resolve to the same file. Equal content at
another path does not satisfy this identity check. Relocate physical inputs
with `--graph-source-root`, which maps each captured source-root-relative path
under the new root. `--source-root` is only a selector lookup boundary and
does not change that physical mapping.

Description reports include `module_proposals` and `module_proposal_diagnostics`
only when proposal analysis runs. Their limits sections use the same field names.
Proposals name their outgoing residual quotient-class dependencies with
`edges_to_other_residual_classes` and `other_residual_classes_referenced`.

`cluster <binding>` emits at most 20 incoming and 20 outgoing module neighbors
by default. `--limit N` sets a positive cap for each direction; `--all` emits
every neighbor and sets `limits.limit` to `0`. The
`limits.sections.incoming_modules` and `outgoing_modules` entries record
`total`, `emitted`, and `truncated` after the selected direction and reasons
are applied. Text output names the same module paths and reports any truncated
section.

`describe` and `inspect-source --id` reject an unsupported identifier-like
prefix before graph compatibility checks or graph loading. Accepted typed
prefixes are `unit:`, `logical:`, `atomic:`, `diagnostic:`, `extend:`, and
`auto_partition_`. Existing module paths are checked before rejecting an
unknown colon prefix because colons are legal in module filenames.

`describe --tree-config <file>` may query with `--modules` set to the full
app-level modules tree. It checks the graph's captured config and resolves
module paths and binding homes only from the configured YAML roots for that
graph chunk. Claims from those roots are resolved together with the ordinary
selector resolver. `--include-proposals` uses the same chunk scope. Without
`--tree-config`, the existing single-root `--modules` query remains in effect.

`describe --batch` accepts a nonempty JSON array of identifiers and returns the
same per-selection reports as separate `describe` calls, in request order with
duplicates retained. One invocation loads and verifies one graph and shares
binding-home and requested proposal lookups. All selections use the same
roots, limits and filters; module source claims retain the ordinary resolver's
semantics. JSON wraps the reports in `results`; NDJSON emits one report per
item. A failed item emits only the command failure, with no partial reports.
The invocation record retains the batch-file path, not its contents.

## Delegated input snapshots

`snapshot create` copies one saved graph's verified input set and exact engine
bytes into a new worker directory. It preserves the input bytes and root
relationships, rechecks copied file digests, and publishes the directory only
after verifying the complete copy. Existing destinations are refused.

The snapshot includes source chunks, the complete captured spec/config input
set, dependency inputs, the graph, and a manifest describing the starting
state. Its returned `analysis_input_args` names the relocated source/package
inputs for native analysis; `transform_context_args` composes those inputs with
the complete tree config, modules, vendor marks and output root. These argument
vectors come from the verified snapshot manifest and are refreshed on
verification after relocation. The regeneration argument vector uses the same
transform context with the copied engine and run-only options. The returned `graph_package_roots` map
supplies each global `--graph-package-root NAME=DIR` override for graph-backed
queries and edits, including the `__packages_root__` fallback name. Separate
snapshots share no writable
input or output files. Snapshot verification continues to work after the
original workspace is removed or the snapshot directory is moved.
Browser source assets and their summary are copied and fingerprinted when
the snapshot is created.

`snapshot verify` checks the starting graph, engine, input contents and YAML
membership. After a worker edits its spec, this baseline check fails; running
the returned regeneration command produces analysis for the edited state.
The shipped worker recipe stages a projection and matching native analysis
with the copied engine, then supplies the full context and artifact to both
preview and validated apply. Graph provenance alone does not authorize a
relocation. Preview and apply consume the same common candidate assessment.

Snapshot creation requires a graph produced by `run` from a tree-shaped spec.
Embedded source and vendor-bundle paths must be relative to the configured
source root, and all captured files must belong to declared relocatable roots.
Unsupported inputs are refused before a worker directory is published.
`describe` returns at most 20 rows per report section by default. `--limit N`
sets a positive per-section cap; `--all` returns every row and conflicts with
`--limit`. Each section has `total`, `emitted`, and `truncated` values under
`limits.sections`; `limits.limit` is the selected cap and is `0` for `--all`.
Filtering by `--direction` or repeated `--reason` flags happens before section
totals and limits are computed. Direction filters dependency, atomic-group, and
module-quotient edges. Reason values are the serialized `DepKind` names:
`eager_use`, `lazy_use`, `hoisted_use`, `eager_rebind`, `lazy_rebind`,
`deferred_rebind`, `sequenced`, `co_declaration`, `co_location`, and
`local_effect`; repeated
values are combined with OR. Rows have stable ID ordering, with the selected
binding, source unit, atomic group, proposal, or diagnostic kept first in its
matching section before truncation.

## Unassigned source

Every configured chunk selects one `unassigned_mode` destination policy:

| `kind`                 | Destination of unclaimed source                                                                                                                                                                                                                                                                                                                                              |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `inline_in_entry`      | Keep it in the chunk entry.                                                                                                                                                                                                                                                                                                                                                  |
| `catchall_file`        | Collect it in `target`, defaulting to `residual/unhandled`.                                                                                                                                                                                                                                                                                                                  |
| `per_atomic_group`     | Emit one module at `__auto/atomic_groups/NNNN` per wholly unclaimed atomic group.                                                                                                                                                                                                                                                                                            |
| `coarse_atomic_groups` | Optionally complete atoms for listed sole authored owners, then group remaining wholly unclaimed atoms by strict fixed-owner reachability and fine evaluation interval when their public export names have compatible origins; then co-locate complete actual mutual SCCs made only of generated residual modules when their combined public export names remain compatible. |

The optional owner list is an array of chunk-local authored module paths:

```yaml
unassigned_mode:
  static/app:
    kind: coarse_atomic_groups
    complete_partial_atomic_groups_for:
      - features/runtime
```

Each listed path must identify a module in the complete authored roster for
that chunk. The planner adopts only currently unclaimed source units of atoms
with exactly one authored owner, and only when that owner is listed. Atoms with
multiple authored owners remain split for the ordinary gate to reject.

An atomic group contains source units grouped by conservative dependency
constraints. Explicit claims are retained; selecting a destination policy does
not relax the complete assignment's effect or structural checks. The generated-module policies preserve
every binding and anonymous statement in its source unit. Coarse grouping is a
deterministic placement heuristic. Its cycle refinement uses the normal
complete-assignment gate's typed actual mutual-SCC diagnosis and merges only
whole SCCs whose members all have generated-residual provenance. It preserves
authored plans, destinations, bindings, anonymous source units, and atomic
groups, together with their existing claims, are preserved. The explicit
completion option adds only otherwise-unclaimed members to one named sole owner.
The complete authored roster and sole-owner snapshot are validated before
ownership-subset filtering, so filtering cannot grant a removed or imported
request ownership or make a mixed-owner atom eligible. Each completion is
reported in `modules.json` under `atomic_group_owner_completions`, including its
complete and adopted source-unit sets. Adopted ownership flows unchanged
through the existing planned-module projection, canonical preflight and
emission preparation, and final joint assessment. The final assessment remains
authoritative after placement and refinement: mixed-owner atomic groups,
authored or mixed SCCs, actual eager TDZ cycles, uncovered effect crossings,
and structural blockers still reject the candidate.

## Entities

An **entity** is one thing a spec places in a chunk: a module member, a
`source_matches[].bindings[]` entry, or an `anonymous_statements[]` entry. Each
entity's selector names the places it may occupy: a top-level declaration or
import binding, or one top-level statement.

An entity is scoped to its module's chunk, and only that chunk's places are its
candidates. In a tree spec, each module tree names the chunk its modules are
scoped to; several trees may name the same chunk, and their modules then share
it as if authored in one tree.

Tree specs also accept per-chunk `chunk_renames` with the flat-spec semantics;
see the [tree authoring guide](README.md#getting-started) for its schema.
These names apply to bindings left in the chunk entry after module claims,
without claiming or moving them. They do not change module-tree selector scope
or graph-reported ownership. `chunk_renames` is the sole source for these names
in every chunk, including the main chunk.

`selector.binding.kind: import_specifier` names an imported local. Source checks
recognize named, default, and namespace imports. Logical-module member
re-exports currently materialize named imported locals only; member claims for
default and namespace imported locals remain unsupported during reconstruction.

## Repeated `var` declarations

Repeated declarations of the same module-scoped `var` denote one binding.
Selecting or renaming that binding applies to every declaration site, including
sites inside top-level control flow. A structural selector may identify it from
any of those sites. Its declarations must share one output module; initializers,
reads, and mutations retain their observable order. A bare redeclaration does
not reset the binding. Other duplicate declaration combinations remain unsupported.
SourceUnit-based relational selectors inspect facts on each statement site; they do
not union those facts across all redeclarations. Binding selectors canonicalize
to the first declaration site, so relations composed with them cannot inspect
later sites' facts. A `source_match` used as a source-unit endpoint
(§ Native integrity inputs, § Commuting statements) selects the declaration
site its template matched, so every site is addressable there; a template
matching several sites of one binding is an ambiguity error. Control-flow
statements containing `var` move intact; all bindings they declare must be
assigned to the same destination.

## Matching

Only the shape matcher decides where a `source_match` template matches. A
template matches a place when their syntax trees are equal up to:

- **holes** — `ANYTHING`, `EXPR`, `STMT`, `STMT_LIST`, `DECLARATORS`, `SEQ_EXPRS` and
  the other hole keywords of <docs/selectors.md> match any subtree of their kind;
- **alpha-renaming** — binding and value identifiers match any identifier,
  consistently within their lexical scope.

A **free** identifier, one the template uses but never declares, is not always
alpha-renamed. In order, it is:

1. a hole keyword;
2. a **reference** to the spec entity exported under that name: by the
   template's own module if it exports one, else by the one other module that
   does. A name several other modules export makes the template `invalid`;
3. an unshadowed **global** (`Object`, `window`, `console`, …) if the chunk
   neither declares nor imports that name at top level: it matches only itself;
4. otherwise alpha-renamed.

A place matches a reference only where the free name binds, throughout the
match, the one chunk identifier the referenced entity takes, whichever selector
pins that entity. A reference to an entity whose own selector fails before the
joint solve (`no_match`, `too_broad`, `invalid`) alpha-renames.

A `source_matches[]` binding may claim a free identifier of its template
instead of a declaration (**pinning by use site**). Its entity is the top-level
declaration that identifier binds to, throughout the match; a match where it
binds no top-level declaration, or different identifiers in different scopes,
is not a place of that entity. A claimed free identifier is the entity's own
binding, never a reference or a global. A claim of free identifiers only does
not claim the matched statement.

An anonymous statement written with a bare `match:` rather than `source_match:`
skips alpha-renaming: its identifiers must equal the chunk's. Literals,
operators, member property names, object keys and tree structure are
significant. Relational selectors (`cross_ref`, `reads_member`, …) match through
facts derived from the chunk, not through templates.

An `anonymous_statements[].source_match.match` may contain a contiguous run of
top-level statements. The final statement is the only claimed place; preceding
statements constrain the match through their shape and free identifiers and may
be owned by other entities. The context form requires an anonymous final
statement. A bare `anonymous_statements[].match` contains exactly one statement.
A context window does not use a top-level `STMT_LIST` hole.

## Local selector explanations

`inspect-source` labels the parsed file's top-level statements and original
locations. With `--declarator-binding <raw-name>` and `--source-file` (or
`--source-root` plus `--chunk`), it can select one unique top-level `var`,
`let` or `const` declarator, including one inside an exported declaration or
destructuring pattern. The reported
`index` remains the enclosing raw `Module.body` index; `declarator_index` is its
zero-based position in that declaration. The byte and line coordinates,
bindings, and source cover only that declarator. Raw view preserves its exact
source slice; pretty view emits a standalone declaration containing the
selected declarator. This query has no surrounding-statement context. Without
it, inspection continues to report whole top-level items.

`spec match-selector --explain --statements` compares one inline or authored
selector against the entire selected range using its structural matching
semantics. It reports a local match, mismatch, unsupported comparison, or
diagnostic limit. An inconclusive comparison is not a mismatch.

This operation does not establish uniqueness, enforce ownership/all-different,
or resolve references to other spec exports. Loading a selector from a spec
preserves its matching mode and claimed bindings without solving other entries.
Local match results are distinct from resolved assignment outcomes.

## Assignment

Every chunk's entities form one program. Each entity takes exactly one of its
matched places, jointly with every other entity:

- no two entities claim the same place, whichever modules or trees they come
  from;
- a relational selector holds between the places its entities take;
- a template's references agree with the places their entities take.

The answer is the **unique** assignment satisfying these. An assignment that
holds only for some solutions of the joint problem is not an answer.

## Outcomes

Every entity gets exactly one outcome, in one record format shared by `run`,
`spec validate` and `spec match-selector`:

| Outcome                                   | Meaning                                                                                                                                                                                    | Severity |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------- |
| `resolved`, `resolved_by: own_selector`   | its selector alone matches one place                                                                                                                                                       | ok       |
| `resolved`, `resolved_by: own_references` | several places match its selector; only one agrees with the places of the `references` it names                                                                                            | ok       |
| `resolved`, `resolved_by: elimination`    | unique only because the named `claimers` took its other places                                                                                                                             | warning  |
| `resolved`, `resolved_by: referenced_by`  | its selector matches several places; the templates of the named `referrers`, which name it, pick one                                                                                       | warning  |
| `no_match`                                | no place satisfies its selector and constraints; a one-statement template may list up to 3 `nearest_unclaimed` statements (no entity claims them), closest first, with where each diverges | error    |
| `unsatisfiable`                           | its interacting group admits no joint assignment; optional `witness` proves an ownership conflict among a subset                                                                           | error    |
| `ambiguous`                               | several assignments exist; at most 5 of its places are listed, with `truncated` when more exist, else with `differentiators` (below)                                                       | error    |
| `conflict`                                | none of the places its selector matches agrees with where the entities its template references (`with`) resolve                                                                            | error    |
| `too_broad`                               | its selector matches more than 100 places, and it takes no further part                                                                                                                    | error    |
| `duplicate_claim`                         | resolved to a binding another entity already claims; `declaration` gives the original body index (`body_index`) and its keyword `kind` (`function`, `const`, `import`, ...)                | error    |
| `invalid`                                 | its selector does not parse, uses an unsupported construct, or its matches do not map to places                                                                                            | error    |
| `undecided`                               | the solver stopped before deciding it                                                                                                                                                      | error    |

An `ambiguous` outcome's `differentiators` give each listed place that has one
a stable anchor the other listed places lack: a literal, object key, class
member, member-path call, declaration kind or arity, in the place's own
statement or else in the statement adjacent to it. A place with none is not
listed there, and the report says when no place has one.

A contradiction affects only its group of interacting entities. Every member
reports `unsatisfiable`, with `group` naming the group's first entity as a
chunk-local identifier. An optional `witness` names distinct selector claims
and their complete candidate-source-unit union, with fewer source units than claims.
This proves an ownership conflict but need not be minimal. Each witness row records
the post-split `source_unit` ordinal, original `body_index`, and matched bindings.
Witness members participate in that conflict; other group members remain
blocked. Without a witness, the group remains infeasible and the diagnostic
states that no smaller ownership witness was found within diagnostic limits.
Independent groups resolve normally.

`run` and `spec validate` in both modes give each entity the same outcome. The
edit gate and `describe` resolve only `source_matches[]` entries and anonymous
statements, without the spec's other members, and give those entities the same
outcome except that one unique only because a relational member claimed its
other places is `ambiguous` to them, and a template reference to any other
member alpha-renames there. Only the pipeline
(`run`, `spec validate --spec`) reports `duplicate_claim`. `spec match-selector`
resolves its selector as a spec of one entity, so a selector resolved by
elimination or by its references in a spec is `ambiguous` there.

`spec validate` also reports selector-validation evidence independently of its
issue rows. `checked` counts one member, one expanded
`source_matches[].bindings[]` entry, or one anonymous statement that was
evaluated; parse or resolution failures count as checked but not matched.
`matched` counts checked entities whose final outcome is `resolved`, including
those resolved by elimination or references. A module-level diagnostic such as
a stale annotation can fail validation without adding a selector to these
counts. `success` means there are no error-severity selector outcomes and every
checked selector matched. It describes selector validation only; it does not
claim that the assignment gate, emission, or runtime checks passed. An empty
selection reports success with `checked: 0` and `matched: 0`, making clear that
no selectors were evaluated. Outcome rows remain diagnostics, not the source of
these totals. If the full pipeline stops before all configured chunks report,
`spec validate` preserves that error instead of returning partial evidence.

## Modes

- **Keep-going** (the default): every outcome is reported, entities with an
  error outcome stay unclaimed, and a chunk with any error outcome fails once
  all of its outcomes are reported. Every chunk is reported; the run fails
  with the first failing chunk in chunk-id order.
- **Fail-fast** (`--fail-fast`): the first error outcome stops the run. Outcomes
  come in a fixed order: every chunk's duplicate claims, then every chunk's
  resolved outcomes, chunks in chunk-id order.

Warnings never stop a run.

## Read-only decomposition candidates

Opt-in atomic-conflict coarsening joins whole candidate groups connected by
native reported structural atomic groups. It preserves the residual entry path
when a joined group contains that entry, then validates the complete assignment
through the shared gate before blocking-SCC coarsening. Without the opt-in,
structural conflicts stop coarsening and remain reported.

Opt-in original-home rejoining starts only from a complete atomic/SCC candidate
accepted by the shared realizability gate. It contracts whole candidate groups
only when every source unit in both groups has the same original module home; an
existing mixed-home coarsened group is never split or joined to a pure group.
Each proposed contraction passes the full shared realizability gate on its
hypothetical complete assignment, and the final assignment passes that gate again.
The command reports a proposal and does not edit the spec. An optional per-home
pair-attempt limit bounds partial fallback checks across all passes while
preserving whole-home checks; the report identifies homes left with untried
pairs. An optional whole-home pass limit bounds complete stable-order passes
across original homes; zero skips rejoining and one permits at most one pass.
If the limit stops a productive pass while pure homes remain split, the report
says `whole_home_pass_limit_reached` and includes the actual
`whole_home_pass_count`. The default remains exhaustive;
limits never bypass either gate check.

With `--preserve-selector-anchors --source-root`, decomposition resolves
authored selectors against their source chunks through the joint selector
resolver. It reports local `intrinsic_alias` and module-neutral ambiguous
source-match references in `selector_anchor_relations`, including named-member
and anonymous-statement sources. Each relation carries a tagged `source`, the
referenced member, and both source-unit-ID sets. Anonymous sources use their authored
`anonymous_statements[]` index; the resolver maps that selector to its stable
source unit before coalescing the candidate groups. A module-neutral unique reference
does not force co-location. The option is read-only and the complete candidate
still passes the shared structural and realizability gates.

## Readable names and the chunk public surface

`name:` on a member and the name assigned to `source_matches[].bindings[]`
name the spec-owned binding in its logical module. If the source chunk already
exports that binding, emitted output keeps the original chunk export name and
adds the spec name as a second export of the same binding. The two ESM export
names are live aliases; existing consumers therefore remain compatible.

`annotations:` is keyed by the final readable binding name. Each key must be
unique within its module YAML; duplicate keys are rejected while loading the
spec instead of one annotation silently replacing another. Distinct keys retain
their separate annotation data.

Named imports in other processed chunks may import the readable alias and bind
it locally under that readable name. This import-local change is conservative:
a collision with an existing or nested binding, an ambiguous alias, or an
unprocessed importer leaves the old import form unchanged. Dynamic imports and
namespace imports continue to use the original minified export names. Bindings
without a spec-assigned name are not changed by this cross-chunk pass.

## Initializer binding effects

`annotations.<binding>.initializer_binding_effects: source_only` asserts that
dynamic evaluation of that binding's direct top-level simple variable
initializer reads or rebinds no top-level lexical binding of this chunk beyond the
initializer's syntactic eager-read and rebind footprint. Calls, getters,
setters, and coercions reached during evaluation are included in this audit.
The assertion is available in logical-module `annotations:` and
`chunk_renames.annotations:`; omission makes no assertion.

Each comma-list declarator is a separate canonical source unit. The assertion
applies only to the selected binding's initializer; sibling declarators and
other statement work receive no assumption. The selected binding must have
exactly one direct top-level simple initialized declaration. A source-backed
lexical read or rebind outside the asserted footprint rejects the assertion.
For the selected initializer, the assertion completes dynamic lexical
read/rebind coverage while preserving its syntax-derived dependencies.

The synchronous execution report identifies this author assertion and counts
the assumed binding effects it narrowed. Observed contradictions are errors.
Other execution precision losses remain reported; the assertion does not mint
a whole-initialization certificate.

This assertion is applied to computed or loaded facts during materialization,
before dependency graphs and initialization effect summaries are built. It is
excluded from the canonical source-analysis projection: `debundle analyze`
stores unfiltered source facts, including fallback evidence and source-owned
operation receipts. Applying the assertion never creates or changes a receipt;
its binding-effect authority remains `author_source_only`.

This contract does not assert purity, freedom from throws, heap locality,
result shape, or integrity of global-object or host state. Shared-heap effects,
foreign registry or configuration access, global-object and host effects, and
callbacks retained for future use remain subject to their ordinary analysis.
`initializer_purity: pure` separately asserts that the initializer has no
observable effects and cannot throw. Neither contract applies to later uses of
its value.

## Static-import and host-loader queries

`static-imports` is a read-only query over the original parsed source chunks.
It reads the authored input list and prepares each chunk once, then follows only
`OrdinaryEsm` static requests resolved to an analyzed chunk entry file. It does
not require selectors, the realizability gate, or materialization to succeed.
For `--from` and each `--target`, JSON reports strict nonempty shortest request
paths in both directions and a separate strict target self-cycle. `same_chunk`
records zero-edge identity; a `null` path means no path in this resolved
ordinary-static-request subgraph. Each witness edge names the source chunk,
literal specifier, resolver kind, target file, and target chunk. Unresolved,
nonordinary, missing-target, and nonentry requests in the traversed source
closures are counted as request issues, with a bounded displayed prefix.
Neither a path nor its absence asserts cache residency, runtime reachability,
module purity, or loader behavior.

`--include-host-loader-candidates` adds every direct literal
`import.meta.require(...)` call from the same one-pass SWC source manifest to
the request graph, including calls nested in functions. These are conditional
source edges: the query does not establish that the host loader has its original
identity, that a call executes during module initialization, or that it returns
a cached namespace. The query does not add calls through aliases, dynamic
arguments, global `require`, or CommonJS exports as resolved edges. Direct
loader-value references, dynamic direct calls, global loader uses, and
CommonJS export uses remain as typed sites in the optional `--interop-census`;
calls through an alias are not traced. The census also lists dynamic `import()`
calls (`dynamic_import`) and separates `module.exports` (`unbound_module_exports`)
from other `module` uses (`unbound_module`). A direct `eval` of the global
`eval` whose single argument folds to exactly `"require"` (string literals,
`+`, and `.replace` with a string or empty `/^/` pattern and a `$`-free
replacement) reads the module-scope `require` and nothing else: it is an
`unbound_require` site, or, as the callee of a call, an
`evaluated_require_literal_call` or `evaluated_require_dynamic_call`. Any other
direct `eval` makes the module's scope dynamic. The census counts every chunk and
bounds detailed sites with `--max-interop-sites`. An absent path in the union
graph excludes only paths through resolved ordinary static requests and these
direct literal candidates; it does not clear any export-reentry blocker.

## Host behavior

`host_behavior` declares what the embedding host does, one assertion per key.
Flat and tree specs accept it at the top level. An omitted key takes its
conservative value, so an absent block is a generic host with no audited
behavior. The spec never names a runtime or version: the identity of the
executable a behavior was audited against is provenance in `note` and in the
project's pinning doc, never an engine input.

```yaml
host_behavior:
  synchronous_module_loader: present # or none
  loader_result_for_evaluating_module: unknown # throws | returns_partial_namespace
  synchronous_load_runs_app_callbacks: unknown # never
  synchronous_load_drains_microtasks: unknown # never
  reentry_free_builtin_modules: [path, fs/promises]
  reentry_free_bun_modules: [bun:ffi, ws]
  reentry_free_embedded_loaders: [text]
  reentry_free_operations: [async_local_storage_construct, promise_resolve]
  predefined_globals: unknown # standard, bun
  note: "Audited against <executable identity, revision, digest>"
```

| Key                                   | Omitted   | Reader                                          |
| ------------------------------------- | --------- | ----------------------------------------------- |
| `synchronous_module_loader`           | `present` | Export-reentry proof (below)                    |
| `loader_result_for_evaluating_module` | `unknown` | Loader-closed derivation (below)                |
| `synchronous_load_runs_app_callbacks` | `unknown` | Loader-closed derivation (below)                |
| `synchronous_load_drains_microtasks`  | `unknown` | Mandatory suspension (loader-closed derivation) |
| `reentry_free_builtin_modules`        | empty     | Export-reentry closure and loader requests      |
| `reentry_free_bun_modules`            | empty     | Export-reentry closure and loader requests      |
| `reentry_free_embedded_loaders`       | empty     | Loader requests for embedded files              |
| `reentry_free_operations`             | empty     | Synchronous-effect interpreter (below)          |
| `predefined_globals`                  | `unknown` | Native census: bundle-owned globals (below)     |
| `note`                                | absent    | None: YAML-only provenance, like other `note:`s |

The three loader keys describe a loader, so each requires
`synchronous_module_loader: present`; giving one with `none` is rejected.
`unknown` is accepted and asserts nothing. `export_reentry.json` echoes the
block as read.

Every non-default value is an author assertion about the host. The author
verifies it against the exact executable recorded in `note`, re-audits on every
executable change, and checks that the analyzed program runs under that
executable with its original builtin loader, exports, Promise constructor and
descriptors. Earlier loaded modules, preloads, plugins, patched binaries and
host instrumentation must not replace those capabilities or introduce
synchronous hooks before the affected initializers. The analyzer's local
mutation guard does not establish that full-program premise, and it neither
runs nor authenticates the executable. Debugger-initiated evaluation and state
changes are outside the behavior-equivalence contract; ordinary
program-accessible hooks and getters remain in scope.

### Predefined globals

`predefined_globals: standard` declares that before the program runs the host
defines no global-object property, own or inherited through the global
object's host prototypes, outside a closed list, and gives each listed
property its standard definition; in particular a host-defined `global` is the
global object. A window's named properties (elements with an `id` or `name`,
child browsing contexts) stay allowed: they are host objects or other realms'
windows, never this realm's intrinsics. The list is the union of the `globals` npm package's
(<https://github.com/sindresorhus/globals>, the version pinned in
`pnpm-lock.yaml`) `builtin`, `es*`, `browser`, `worker`, `serviceworker`,
`sharedWorker`, `audioWorklet`, `paintWorklet`, `node`, `nodeBuiltin`,
`commonjs` and `shared-node-browser` lists, plus the own properties of
`%Object.prototype%`. `predefined_globals: bun` makes the same claim for a Bun
host whose list also holds Bun's own globals: `Bun`, `HTMLRewriter`,
`BuildError`, `BuildMessage`, `ResolveError`, `ResolveMessage` and
`ShadowRealm`, the string keys of Bun 1.x's global object and its prototype
chain outside the standard list. `unknown` asserts nothing. The author checks
that the audited executable, its preloads and any script loaded before the
program add no other global.

This constrains initial property definitions; it is not an operation-effect or
no-reentry certificate for a later global store. Under `unknown`, a preexisting
setter may invoke an assigned closure synchronously. A complete store proof
still needs the actual receiver/reference, descriptor and prototype at the
execution prefix, normal completion, and retained capability effects.
[Host-defined exotic global objects](https://tc39.es/ecma262/2024/multipage/executable-code-and-execution-contexts.html#sec-initializehostdefinedrealm)
are separate from the property-name lists.
Even when the property-name list is closed, a host prototype with no own keys
can have an exotic `[[Set]]` that invokes an assigned closure. Adding such a
prototype preserves the existing definitions and the absence of a new property;
the name-list contract does not prove ordinary prototype operations. Consequently
`standard` alone cannot authorize moving a later initializer ahead of publication.

### Synchronous module loader

`synchronous_module_loader: none` declares that no host API can synchronously
obtain the namespace of a module while that module is evaluating, as under the
browser's native ESM loader. It is independent of browser-harness output
configuration. A browser worker can satisfy it while having a custom `window`
property, so it implies no Window realm.

Without a synchronous loader the export-reentry proof still checks a complete
ordinary-ESM static closure, absence of top-level await and absence of a cycle
back to the selected chunk. CommonJS compatibility references alone do not
expose that chunk's exports. Callbacks actually passed or published remain
tracked, including callbacks that escape through global or unknown objects.

Vendor-swapped chunks take part through their bundled bodies (§ Vendor swap
equivalence): a replacement evaluates like the code it replaces. The one
visible exception is a bundled partial replacement whose prepared bundle is
not an ESM leaf (it has static requests or top-level `await`); it blocks the
proof as `open_replacement_bundle`. Host-loader sites are admitted only by the
loader-closed derivation below.

### Loader-closed derivation

With `synchronous_module_loader: present`, the export-reentry proof also needs
the target to be loader-closed: no host loader can hand any code the target's
namespace while the target evaluates. When no analyzed chunk has a host-loader
site the derivation is `no_host_loader_sites` and reads no loader key, as with
`synchronous_module_loader: none`. Otherwise the target is loader-closed when
all of the following hold:

- `synchronous_load_runs_app_callbacks: never` and
  `loader_result_for_evaluating_module` is `throws` or
  `returns_partial_namespace`. An omitted or `unknown` key keeps the target
  open.
- Every host-loader site in every analyzed chunk is a direct literal
  `import.meta.require(...)` call (the census of SPEC.md § Static-import and
  host-loader queries). A non-literal call, an `import.meta.require` reference
  (an alias, including an exported loader value), `globalThis.require`, an
  unbound `require` or `module` (other than `module.exports`), `import.meta` as
  a value or with a computed key, a TypeScript `import =`, and a use of the
  `module` builtin that can make a loader keep every target open. Under
  `program_premises.non_literal_loader_calls: native_addons_only` (§ Program
  premises) a direct non-literal call, of `import.meta.require`, of a loader
  alias, of a created loader (below) or of an evaluated `require`, is excluded
  and counted in `premised_sites`; every other form stays open. An evaluated
  `require` called with one string literal is a literal request. A dynamic `import()` is not a
  synchronous loader site: ECMA-262 evaluates its target in a later job
  (`ContinueDynamicImport`), never on the requester's stack. Whether it names
  a debundled internal module stays the A3 admission check
  (<docs/design.md> § Conditions on the input chunk), which resolves every
  string-literal specifier, parenthesized or not, at any depth.
- Each literal request names a `reentry_free_builtin_modules` or
  `reentry_free_bun_modules` entry, resolves to an analyzed chunk's entry
  file, or names an embedded file whose host loader is a
  `reentry_free_embedded_loaders` entry (§ Reentry-free embedded files). A
  request for any other builtin or embedded file, or an unresolved specifier,
  keeps every target open.
- For each request resolved to a chunk, that chunk's static and loader closure
  excludes the target, and every chunk in it has only resolved ordinary static
  requests to analyzed chunk entries or reentry-free builtins and no top-level
  `await`. Cycles through the target are therefore
  open; a loaded chunk need not be ESM.

With `synchronous_load_drains_microtasks: never`, a mandatorily suspended site
is excluded: its innermost function is async (not a generator) and every path
from that function's entry to the site completes an `await`, so the site runs
in a promise job, which no synchronous code, including a synchronous load, can
start. The walk follows evaluation order: an `await` operand runs before the
suspension it causes; a short-circuit, conditional or optional-chain operand
joins with the path that skips it; a loop body may run zero times (`for await`
awaits before its body and its exit); `catch` and `finally` start from the
`try` entry; `break` joins its target's exit and `return`, `throw` and
`continue` leave the path. Generators, getters, setters, constructors, class
field initializers and static blocks are never suspended, and a site the walk
finds on no path stays unsuspended. The derivation then consumes the key and
reports the excluded `suspended_sites`.

A site no reachable code can enter is excluded and counted in
`unreachable_sites`. Top-level code is reachable. A function expression, arrow,
method, accessor, constructor, class field initializer or static block is
reachable when its enclosing code is. A function declaration, or a binding
initialized with a function expression or arrow, is reachable when its
enclosing code is and either its binding escapes (any reference other than a
direct call, including an export, `new`, `.call` or a repeated declaration) or
a reachable function calls it directly. Lexical nesting proves nothing either
way: a site in a nested function counts when that function is reachable.
Direct `eval` or `with` makes every function of the module reachable. The
analysis is per module: an imported function is an export, hence an escape, of
its defining module. A statement after an unconditional `throw`, `return`,
`break` or `continue` in the same statement list never runs, and neither does
anything nested in it: its references publish nothing and its calls enter
nothing. A function declaration there is still instantiated with the list's
scope and keeps the ordinary rule.

The `module` builtin is classified by its uses. A literal loader request for
it whose result is only read as `.builtinModules` is a request for the builtin
`module`. Its named import `builtinModules` is data. Its named import
`createRequire` may only be called directly as the initializer of a
`var`/`let`/`const` binding, at any depth, which makes a created loader: a
direct call of it with one string literal naming a host module (§ Reentry-free
builtin modules) is a request for that module, a `.resolve(...)` call loads
nothing, and any other use (another literal, which it resolves against its own
base, a non-literal call, an export or any other reference) is opaque. Every
other import of `module` (default, namespace, re-export or another named
import) and any other use of these keeps every target open as
`loader_factory_request`. A static or literal request for `module` loads it
like any builtin, so `module` must be listed in `reentry_free_builtin_modules`
for the request itself.

A top-level binding initialized with exactly `import.meta.require` is a loader
alias. Its definition is classified through its uses instead of as an opaque
reference. Each use of the alias, in its own module or through a named or
default import of a name it is exported under, is classified like a census
site: a direct call with one string literal is a literal request resolved
against the defining module (whose `import.meta` the loader belongs to);
any other call or reference, including a re-export through a local binding,
keeps every target open. A namespace import, `export *` or
`export { … } from` of a module exporting a loader alias, and a literal loader
request or reachable dynamic `import()` of one, expose the alias and keep every
target open.

A remaining site counts when it can run in the target's window, from the
target's entry into evaluation until it finishes. A direct load in top-level
code, outside every function, runs only while its own module's body evaluates:
it counts in the target, in the target's static closure, and in a chunk a
counted request loads. Any other site, including a loader value that escapes
from top-level code, runs whenever a callback can, since a callback any chunk
published earlier can run in the window. It counts unless its chunk
statically imports the target outside a cycle with it: that chunk's body, and
with it every reference to its functions, starts only after the target
finishes. A counted request that loads a chunk brings that chunk's whole
static and loader closure into the window. Sites outside the window are
counted in `outside_window_sites`. A site the census does not see, such as a
computed `globalThis` read, `process.mainModule` or `Function`/indirect `eval`
code, is outside this proof, as it is outside the census.

### Reentry through imported names

When the only failure is `loader_request_reaches_target`, with no global or
static-closure blocker, the policy is `reentry_through` with a set of export
names, instead of conservative. The reentry window is the union of the
closures of the counted requests that contain the target. The set is every
name the window's chunks import from the target by named or default import.

The policy stays conservative when:

- a window chunk takes a namespace import of the target;
- a window chunk re-exports from the target (`export *`, `export { … } from`);
- a window chunk requests the target through a loader;
- a reach-back request loads the target itself.

Each of these hands out the whole namespace. Under `reentry_through`, only the
named exports (`default` for the default export) are synthetic execution
roots and exposed capabilities for unknown-call fallback.

Soundness: while the target evaluates, its bindings are reachable only from
the target itself, from chunks that evaluated before it (which cannot import
it), and from chunks a load evaluates inside the window. A window chunk's
module code reads the target only through its own import bindings. That
holds for its body, for every function it creates, and for anything those
functions later run, however they are reached: by the load itself, by a
callback an opaque target statement invokes after the load, or by an escaped
value. An imported exported function runs with its own reads, which its root as an
execution root covers. So no top-level-only or synchronous-closure bound on
the load is taken.

A window evaluation that does not complete (it throws, or calls code the
analysis cannot follow) changes nothing: the bound rests on what the window's
code can name, not on what it runs. Code reachable only through a namespace is
excluded by the conservative fallback above. A re-export from another window
chunk names the target's binding in that chunk's own request, so it is
counted there. Every incomplete statement of the target still gets ordering
edges to the named exports; the analysis does not decide whether a load runs
before it.

The derivation is a judgment of the shared inference (<docs/inference.md>);
`export_reentry.json` names its rule, the keys a closed certificate consumed,
the resolved requests and loaded chunks, and the first witness and count of
each failure kind (<docs/wire_format.md#export-reentry-diagnostics>).

### Reentry-free operations

`reentry_free_operations` admits two exact zero-argument operations: a direct
`new` of a named `AsyncLocalStorage` import from the builtin `async_hooks` or
`node:async_hooks`, and a direct ordinary `Promise.resolve()` call on the
unshadowed global constructor. The former allows a named import alias (`as M`)
but not a subsequent local alias; the latter rejects a shadowed, aliased,
replaced, computed, detached, or optional callee. Both reject arguments and
spreads. Visible global/`Promise` mutation and dynamic scope disable the
Promise contract. When opaque in-source receiver effects defeat the
Promise/global integrity guard, `promise_resolve` must be paired with a
source-selected `native_operation_inputs` `promise_resolve` assertion for that
same initializer. This pair admits only the exact call's native identity and
host timing; it does not repair global integrity or affect other calls. The
assertions only suppress the opaque synchronous callback-reentry fallback after
the callee and arguments have evaluated. Purity, ordering, possible throws,
return shape, later method calls, and module-loader reentry retain their
ordinary analysis.

The tagged Bun 1.3.10
[`async_hooks`](https://github.com/oven-sh/bun/blob/bun-v1.3.10/src/js/node/async_hooks.ts)
and [`v8.promiseHooks`](https://github.com/oven-sh/bun/blob/bun-v1.3.10/src/js/node/v8.ts)
stubs support these operations; [Node's `async_hooks`](https://nodejs.org/api/async_hooks.html)
can synchronously invoke registered callbacks during Promise creation, so the
assertion must not migrate to Node or another build without a new audit.

Each successful use appears in the source unit's synchronous report under
`author_host_contract_uses`, with the exact operation and source location. An
unused assertion produces no use record. These are static proof uses, not a
runtime invocation trace. Failed Promise/global integrity guards remain visible
under `native_integrity_checks` as `host_promise_resolve`. Host assertions
establish timing only; their provenance must not be presented as an
unconditional inferred native guarantee. A successful use also adds the exact
`reentry_free_operation` key to typed proof dependencies, so evaluation,
covered-owner and relocation evidence retain the host assertion they consumed.
When a same-site `promise_resolve` native input substitutes for a failed
integrity guard, its exact input reference enters the same dependencies. A
refused, unselected or unused operation contributes neither dependency.

### Reentry-free builtin modules

`reentry_free_builtin_modules` is independent of `reentry_free_operations`. The
author asserts that each selected builtin ID's explicitly recognized bare and
`node:` static-import spellings resolve to the host builtin and cannot
synchronously reenter an application chunk during module evaluation. Only
ordinary static ESM requests that the artifact resolver did not resolve may use
the closed Node builtin list, and literal `import.meta.require` requests in the
loader-closed derivation. The export-reentry report records each consumed
static source chunk, specifier, builtin ID, and request count. Unselected names
and unknown bare packages stay unresolved.

`reentry_free_bun_modules` applies the same leaf rule to the closed list of
host modules outside Node's namespace: `bun:ffi` and the packages Bun replaces
with built-in implementations, `undici` and `ws`, each under its exact
spelling. For a bare package name the author also asserts that every request
from an analyzed module resolves to the host's replacement, never to an
installed package.

### Reentry-free embedded files

`inputs.embedded_loaders_path` names a JSON object mapping each embedded file's
input path to the loader the host applies when it is required, as Bun's
standalone module graph records it (`bun_standalone_extract.py
--embedded-loaders`). It is a fact of the input, not an author assertion. An
absolute literal loader request `/<path>` whose `<path>` the manifest lists is
a request for that embedded file. `reentry_free_embedded_loaders` lists the
loaders the author audited as running no application code: `text` returns the
file's text and `napi` runs only the native addon's initializer. A `file` asset
is not admissible: Bun's runtime loads it by extension, so a `.mjs` asset
recorded as `file` runs as code. `napi` therefore also requires
`program_premises.non_literal_loader_calls: native_addons_only`, whose second
clause covers every addon initializer, and the spec is rejected without it. A
`file`, `js` or other loader keeps every target open as
`non_reentry_free_embedded_file`. Relative requests and static imports of
embedded files are not resolved.

This leaf rule grants no purity, call safety, return shape, or later callback
fact; CommonJS/application loader, non-ESM, vendor, unsupported-request, and
cycle blockers retain their normal rules. The closed set is specific to the
audited host, not a promise about future runtimes or all Node modules.

## Program premises

`program_premises` declares facts about the analyzed program that the engine
cannot derive, one assertion per key, beside `host_behavior` in flat and tree
specs. An omitted key asserts nothing. Each is an author assertion over the
whole program, including code the analyzer never sees; the audit and its
provenance go in `note`, which the engine does not read. `export_reentry.json`
echoes a non-empty block, and a closed loader certificate lists each premise it
consumed under `premise_keys`.

```yaml
program_premises:
  process_prototype_chain_accessors: none # or unknown
  non_literal_loader_calls: native_addons_only # or unknown
  intrinsics_never_written:
    families: all # or a list: [Object, Array, Map]
    note: "Audit list: program_graph.json.gz; runtime probe: <invocation>"
  note: "Audit: <where the premise was checked>"
```

- `process_prototype_chain_accessors: none`: no code in the program defines or
  redefines an accessor property on `process` or on any object in its
  prototype chain (including `EventEmitter.prototype` and `Object.prototype`),
  or changes that chain. Building the `process` namespace reads every property
  along that chain, and `worker_threads` sets `process` members inside a
  worker, so `reentry_free_builtin_modules` accepts `process` and
  `worker_threads` only with this premise; otherwise the spec is rejected.
- `non_literal_loader_calls: native_addons_only`: every loader call whose
  argument is not one string literal either fails or loads a native addon, and
  no native addon's initializer calls application JavaScript or a module
  loader. The loader-closed derivation then excludes such calls (§ Loader-closed
  derivation).
- `intrinsics_never_written`: § Intrinsics never written.

### Intrinsics never written

`intrinsics_never_written.families` names native census families (`Error`,
`FinalizationRegistry`, `Function`, `Object`, `Array`, `Math`, `RegExp`,
`Symbol`, `Promise`, `Map`, `Set`, `WeakMap`, `WeakSet`), or `all` of them. It
asserts that no execution of the program writes any slot of a named family:
its global binding, its constructor's properties, its prototype object and
properties (with `%ArrayIteratorPrototype%` for `Array` and
`%SetIteratorPrototype%` for `Set`), or its `@@species`.

The engine still runs the census (§ Inferred native property-key domains). A
census write on a named family's exact object refutes the premise and the spec
is rejected, naming each site: a write whose receiver is the family's
constructor, prototype or iterator prototype, a global-object write keyed by
its binding name, or an assignment to that binding. Every other witness that
would reach a named family (an unknown receiver, a global-object write that
couples every prototype slot, a descendant) is overridden: the family's slots
stay intact and `%Object.prototype%` property checks pass when `Object` is
named. `program_graph.json.gz` lists each overridden witness under
`intrinsics_never_written_overrides` with its source location, the families it
would reach and why (`unknown_receiver`, `global_object`, `family`). That
generated list is the author's audit list. Proofs that use an overridden slot
record `intrinsics_never_written` among their program premises.

The author audits every listed site: its receiver is never an intrinsic of a
named family under any input the program and host can supply. Specs are per
snapshot, so the audit is redone with each snapshot. Verify the premise at
runtime as well: snapshot the identity, and where relevant the property
descriptors, of every named family's slots before the bundle runs and after
startup completes, and require them unchanged.

## Hoisted function imports

A direct eager read of a function declaration retains a `hoisted_use` source unit
edge. The function binding is initialized during ESM instantiation, so this
edge does not constrain evaluation order or atomic co-location. It still
participates in the imports graph: entering the function's module can change
the traversal order and expose a TDZ on another binding. The gate must model
that import even when reading the function itself is safe.

## Export-reentry diagnostics

Materialization reports the selected export-reentry policy and its observed
blockers in `export_reentry.json` beside the per-chunk dependency graph. This is
diagnostic evidence only: it does not change the analyzer's proof or emitted
JavaScript. `Full` reports include accepted and rejected chunks; rejection-only
reports include it on rejection, while accepted dry runs remain silent. The
typed wire schema and blocker fields are described in
<docs/wire_format.md#export-reentry-diagnostics>.

The default is an output-only borrowed summary with exact collection/site
counts and at most twenty deterministic examples per blocker, builtin, request,
loaded-chunk or reentry-name collection. Truncation is explicit. Loader failure
counts and their existing first witnesses remain complete. The actual policy,
loader derivation and staged analysis payload remain complete and unchanged;
no semantic check consumes the presentation view. `run --full-report
export-reentry` streams the original complete schema as compact
`export_reentry.full.json` and explicitly flushes it before writing the summary.
Write/flush errors fail the run. Optional `--full-report-chunk` filters only
full-report sinks; graph exports remain independent. Accepted dry runs refuse
an explicit full-report request when they suppress reports. No output capability
flag asserts that an unrequested full file exists.

The static import closure admits a source with no AST items, including a
comments-only file: it cannot execute statements or reenter an importer.
Sources containing statements without a runtime ESM declaration retain the
`non_esm_chunk` blocker. This source fact does not waive any other closure or
runtime requirement.

## Vendor swap equivalence

A `swap`, `partial_swap` or `bundled_partial_swap` mark is the author's
promise that each replacement behaves like the bundled code it replaces, in
everything the engine reasons about: purity, whether and when callbacks run
(synchronously or deferred), returned values and allocations, module-evaluation
effects and loader behavior. Analysis derives facts from the bundled body and
applies them to the emitted program, which runs the replacement. The author
owns the promise. A different build of the same package version (down-levelled
syntax, lowered decorators, another bundler's helpers) or a wrong version pin
can break it without any visible change to the spec. The promise covers the
replacement's behavior as the bundled code exhibits it; global or intrinsic
changes a replacement package makes while initializing remain the author's A11
obligation (<docs/design.md> § Conditions on the input chunk).

A swapped export's derived foreign call summary names the replacement in
`modules.json` as `imported_callables.*.foreign_call.replacement_package`, so
the author can audit it against that package. Other facts carry no such field.

## Partial vendor-swap ownership

The vendor strip pass assigns top-level items to swapped packages through
source reads and rebinds (including nested function bodies), plus writes to
referenced local values. Inferred possible call targets do not assign package
ownership: a function passed to a different invocation of the same helper is
not thereby part of this package. The strip pass rejects non-shareable items
also needed by residual exports and checks retained bindings and observable
writes before removing swapped items.

A removed item's side effect must not reach state the residual keeps, including
through the functions it calls. Values a removed item hands to host code — a
call on a global the chunk does not declare, such as a devtools hook's
`inject(internals)` — are treated as values: host code calling back into what
it was handed is outside the analysed program, and the author (for the swap's
replacement, which hands over its own values) and the host own it, like A11.
Native methods called on containers the chunk owns still run the callbacks they
are handed.

## Synchronous analysis limits

Recursion and analysis limits preserve the first-await boundary where retained
callback read and write coverage is complete. Possible synchronous reads and writes remain
constraints, including for private callbacks that have not escaped. Unsupported
bodies and dynamic scope retain conservative whole-body effects. Opaque-call
reachability follows those same synchronous prefixes, so a value first read
after a definite `await` does not expose its callback body through that path
during chunk initialization.

Prefix write coverage includes assignments, updates, destructuring and loop
targets. A write whose target or value evaluation contains an `await` may still
be retained conservatively. Deferred writes continue to require co-location
with the binding declaration because ESM imports cannot be reassigned.

Ordinary eager reads and synchronous callback reads stay between the known
preceding and following writes of the binding, including `var` initializers
and later assignments, even when the eager reader's source unit is pure.

A local named export list, `export { local as publicName }`, evaluates no
expression and contributes no heap-content barrier of its own. This sufficient
condition requires no source module and only named specifiers. Exported live
bindings and callbacks retain their ordinary visibility and reentry policy.
Other export forms retain their existing syntax effects and conservative
handling, including default expressions and re-exports.

For ordinary synchronous functions and block-bodied arrows with simple
parameters, the canonical index can retain a complete straight-line local
body: simple declarations, local assignments and sequences, unconditional
noncoercing values, and terminal return or throw. Execution initializes `var`
locals to `undefined`, retains lexical TDZ and const-write errors, and applies
local writes in source order. Only completing paths reach the next operation;
binding observations retain callback-path provenance through ordinary outcome
composition and invocation memos. Thrown capabilities remain conservatively
published. The value operations use the shared literal-allocation transfers.

Identifier `++` and `--` in such a body read the binding, apply `ToNumeric`,
and store through the same local or captured-cell path as assignment. Prefix
and postfix results retain only possible Number or BigInt kinds; the analysis
does not infer exact old or new values. A proved primitive Number conversion
can carry a local binding-effect receipt when the target is an initialized,
uncaptured mutable `let` or parameter. `var`, captured stores, const/TDZ errors,
possible Symbol or BigInt throws, and object coercion remain uncovered or take
the conservative fallback. Member updates are outside this ordered subset.
Static field assignments through a local or parameter binding also execute in
order when the receiver is one settled ordinary record allocated during this
invocation and the field is an existing own data property. This permits local
aliases, replacement of a parameter by a fresh record, and record self-links.
Sites already represented in the heap or reachable through inputs or observed
external bindings retain conservative handling because allocation generations
are not distinguished.
The receiver reference is evaluated before the right-hand side, even if that
side replaces the binding. Stores join values in the shared allocation-site
heap; they do not overwrite earlier abstract values. An unproved store stops
ordered execution and conservatively summarizes the remaining body, retaining
input, local and newly allocated capabilities and possible setter effects.
Its precision loss identifies the assignment's source span.
An incomplete, already-executed local call in an exact terminal `return call(...)`
of a callable stops ordered execution without publishing the caller's entire
frame: there is no remaining caller operation to summarize. This sufficient
condition requires a directly selected, singleton ordinary source function,
excluding opaque, async and generator bodies, and a child value transfer. Child
effects, escapes, precision losses and incomplete execution remain; partial
returns are widened to include unknown normal and abrupt completion, and no
effect coverage is issued. A selected callee with neither a source return nor
a fall-through path retains only its abrupt result; this terminal fallback
cannot reintroduce a normal value that the invocation boundary excluded.
Nonterminal calls, operand failures, unknown targets and missing-result
transfers retain ordinary fallback.
Calls, member reads, computed writes, branches, unknown free bindings, dynamic
scope and unsupported syntax retain the conservative execution path. This
coverage does not certify initializer effects or an imported provider summary.
Certified record writes, writes through unrebound parameters and
array-iteration writes retain their established execution path while their
proof obligations are migrated to ordered operations.

Synchronous execution also completes an exact member read from a TS enum
object when, at the read, the binding is `PlainData`, no statement but its
declaration writes it, its initializer has run, and the static key is one of
the primitive fields that initializer stored into its fresh receiver. This proof suppresses only possible getter/callback
reentry: the member read and its eager binding dependency remain. Mutation,
opaque escape, reflective writes, accessors, unknown keys, and reads before the
initializer keep the conservative member-read path. Purity of the initializer
alone does not establish the receiver's property identity.

Opaque calls holding a class or instance may invoke its constructor, field
initializers, methods, and accessors. Their synchronous read closure stops at a
definite `await` where retained callable coverage is complete; uncovered source units
and incomplete reachability retain whole-body effects and rebinds.
Defining a pure, unpublished subclass with a direct, uniquely known ordinary
class superclass reads that superclass for the eager dependency graph, but does
not by itself publish or invoke the superclass constructor or methods. Actual
construction and publication remain reachability roots; unknown or alternate
superclass values retain conservative roots.
When those effects reach a top-level class declaration with a simple binding
as its superclass, analysis uses the possible class values from the prefix at
the declaration. Later writes to that binding do not replace the superclass
captured by the class. Unsupported or uncertain heritage retains the generic
fallback.

A pure initializer that calls a uniquely known local function can retain an
unpublished callback graph in a fresh ordinary record without making that
initializer a speculative reachability root. The function must have simple
parameters, no synchronous calls, writes, dynamic scope, defaults, or other
effects, and a single return value modeled as a plain data record. Its values
may be parameters, local or inline functions, primitives, or nested records
of the same shape. Arguments must be inline functions or primitives. The call
and its eager dependencies still run normally; any later export, write, or
invocation of the result restores its callback reachability.

A second source-proven record form permits ordered assignments to distinct
pre-existing own data fields of one fresh local object. The function body must
contain exactly a `const` initialized by a plain object literal with static
data keys and literal values, one or more simple static field assignments from
simple parameters or literals, and a return of that same local. Every written
field must exist in the initializer and be written once. This avoids inherited
setters, accessors, proxies, aliases, reassignments, callback calls, external
publication, and write-order uncertainty. A shared certificate is consumed by
function-body purity and synchronous callback analysis; the latter also checks
that the execution index fully represents the writes. If any check fails, the
ordinary effect and reachability paths apply. Calls and eager dependencies are
still evaluated; later use or publication of the result can promote its
retained callbacks.

At an immediate top-level object destructuring declaration, a direct call to
one stable local function may also supply a fresh own-data record shape. The
function must be synchronous and have a straight-line prefix followed by an
unconditional terminal return of an object literal with only distinct static
own data fields. Each selected pattern key must be present; defaults, nested
patterns, rest, computed keys, accessors, spreads, `__proto__`, alternate
returns retain the conservative pattern reason. Factory calls also require no
dynamic scope or callee rebindings. The value-flow target must be one canonical
local function identity.
This removes only the destructuring getter/proxy purity reason; the call and
all prefix, argument, throw, read and callback effects remain independently
analyzed. An immediate fresh object literal uses the same own-data check.

A direct local wrapper call may also retain callbacks in a fresh checked class
instance without exposing them during the initializer. The wrapper must have
one return containing only checked local `new` allocations, simple parameters,
no other calls or effects, and a unique stable identity. Inputs must be fresh
plain-data records, inline functions, or primitives. A fresh record may contain
a dense fresh array of these inputs or initialized, exact own primitive members of a
settled TS enum object. Enum reads retain their eager dependencies;
mutation, escape, holes, spreads, and unknown keys retain conservative
reachability. Nested allocations must be inert; constructor
property reads must resolve to own data or safe missing properties under A11.
The initializer, wrapper, and class source units still contribute actual synchronous
effects and ordering. Their execution alone does not expose deferred methods or
make them available as host accessors. An exported result is eligible when the
analyzed module closure rules out synchronous importer reentry during provider
initialization. The result binding is written after the checked call returns;
later use and publication follow the ordinary callback paths. Unsupported
constructor paths retain conservative reachability.

A binding annotation `retained_record_factory: true` supplies only audited
prototype premises for this source-checked local factory proof. The author
asserts that missing input fields read by the checked constructors are actually
absent along their prototype chains, and that instance assignments successfully
create ordinary own data properties without interception. Getter-freedom alone
is insufficient: an inherited data value can retain an unrepresented callback.
This assertion can override conservative prototype-mutation census blockers;
it does not declare the factory pure or native identities intact.

Admission requires a direct unary call of the selected stable top-level hoisted
factory with a fresh literal data record. The canonical AST check excludes
assignment/sequence-wrapped records, spreads, computed keys, accessors,
`__proto__`, duplicate keys, array holes and other eager calls. Nested dense
arrays, literal values, inline callbacks, method definitions and independently
checked enum reads are supported; defining a method allocates a function the
record retains like an inline callback. A template literal is supported when
every substitution is a literal, a primitive `const` below or a checked enum
read, so its `ToString` runs no user code. A record value may also be an
identifier when, at the call, it is an initialized top-level binding with no
dynamic-scope contamination and one of:

- one simple top-level `const` with a direct string, number, boolean, null, or
  BigInt literal initializer, preceding the call, with no source writes and
  exactly one primitive abstract value in the current prefix;
- exactly one source function as its whole-chunk and prefix value; the record
  stores it without calling it and retains it like an inline callback;
- exactly one untainted literal dense array or plain-data object as its
  whole-chunk and prefix value, every current slot of which is primitive.

The binding read still executes normally, retaining its eager dependency and
TDZ behavior; other object aliases, other member reads and nonliteral
initializers are not admitted. The ordinary
source-checked return-only constructor proof and normal execution remain in
force, including eager lexical reads, TDZ, argument evaluation, exposure checks,
side effects and ordering. A typed call-site certificate may remove the generic
impure-callee purity reason only after that full source proof and successful
ordinary execution. It changes no factory or class summary and leaves callee,
argument, lexical-read, TDZ, and other purity reasons intact. Native evidence
used by the constructor proof remains attached as purity dependencies. Only the
exact checked constructor/property and input-object/missing-property pairs
consume the assertion. Failed source proof or runtime admission restores
conservative reachability and issues no purity certificate. Consumed author
obligations appear in `synchronous_execution.local_record_factory_uses`,
separately from inferred native integrity. An annotated direct call that keeps
the conservative path records one typed reason in
`synchronous_execution.local_record_factory_rejections`: the refused record
shape with its outermost property, or the checked-proof condition that failed
(with the constructor check for `constructor_proof`).

An implicit local class constructor can also receive a source-derived
construction certificate. The class must be a direct declaration in the same
chunk, without inheritance, decorators, an explicit constructor, computed
keys, static fields, or static blocks. Instance field initializers may be
absent, primitive literals, boolean negation of a numeric/boolean literal,
deferred function/arrow allocations, or zero-argument construction of an
unshadowed built-in `Set`, `Map`, `WeakSet`, or `WeakMap`. Ordinary method and
getter bodies remain deferred. Purity admits only a direct zero-argument
top-level `new` initializer after that declaration when whole-chunk value flow
resolves its binding to exactly that class. It suppresses only construction's
side-effect ordering; method invocation, callback escape, and ordinary eager
reads keep their own checks. The synchronous interpreter consumes the same
class certificate and retains its separate binding-identity and deferred-body
checks. Unsupported shapes and uncertain identities keep the conservative
paths. Before any semantic consumer uses this composed certificate, its native
field identities must pass the shared native-integrity and empty-collection
rules over the canonical execution index's completed census. Purity,
synchronous execution and heap ordering share that validated result;
recognized native replacement invalidates both. This uses the existing local
and whole-program native census and does not change the catalogue's A11
assumption for other whitelist admissions.

When a Set or local-class purity certificate consumes a captured value input,
the resulting pure verdict carries the exact `value_inputs` references in its
`dependencies` field. Those references are unioned through pure local calls,
binding initializers, and inferred cross-module exports, including fixpoint
rounds where the verdict stays pure but learns another dependency. Empty
dependency sets are omitted, preserving the existing `{ "kind": "pure" }`
report shape. An author `pure_exports` assertion remains its own axiom and
does not acquire captured-value references from unrelated inference. The
dependency field records the assumption used by that purity decision. The
captured-object assertion itself does not declare the capture initializer pure,
prevent callbacks or throws, or establish freshness or initialization order.

A SHA-bound `argument_inputs` contract can preserve a plain-data object passed
to one selected direct local function. The contract names a positional
parameter and exact arity, and certifies that the function and its callees
cannot mutate, retain, return, publish, or expose the argument graph through
callbacks or opaque forwarding during or after an activation. Selection
requires a direct synchronous function declaration; a shared stable-callable
fact and complete singleton call target guard each use. Source-visible direct
mutation, store, return, throw, or capture rejects the assertion. Rebinds, spread
calls, and arity mismatches keep the ordinary escape path. The assertion says
nothing about the helper's purity, reentry, or unrelated effects. Opaque native
behavior is an explicit audit obligation rather than inferred native identity.

At-init callback analysis also records possible implicit primitive conversions
from arithmetic, relational and loose-equality operators, the left operand of
`in`, numeric unary operators, updates and non-logical compound assignments.
If retained value flow proves an operand primitive, the conversion adds no
callback effect. An object or unknown operand uses the conservative synchronous
callback path, covering custom `[Symbol.toPrimitive]`, `valueOf` and `toString`
methods without assuming which conversion method runs. Strict equality,
short-circuit operators, `typeof`, logical negation and `instanceof` do not
create a ToPrimitive effect. Callback effects reachable through an operand's
retained lazy closure are included as well, covering an accessor that returns
another conversion callable. This is callback-effect analysis; it does not
classify the enclosing operator as pure.

Primitive values are tracked by their possible ECMAScript primitive types. A
chunk-top `const`, or a stable top-level `var` with a Number initializer (whose
type also includes its hoisted `undefined`), has the types its initializer
completes with: literals, templates, operator results, conditional and logical
alternatives, the last sequence element, earlier tracked bindings, and the
primitive return of a builtin row purity may trust by spelling, when its
unshadowed, unwritten intrinsic receives only primitive operands where it
coerces (`Symbol` returns Symbol, `Number.isNaN` Boolean). The synchronous
interpreter, which proves each row's integrity slots, also takes `Math.round`
and its family as Number and `Symbol.for` as Symbol. A stable direct ESM export
carries its types to named/default importers and re-exports, so copying one
through local bindings preserves its callback-free conversion fact;
reassignment to an object or an unknown alternative restores conservative
conversion analysis. An exact static own primitive member of a TS enum object
settled at the statement (as above), used as a computed property key or a
template substitution, is primitive too; whole-chunk mutation, escape,
accessor, rebind and source-order checks still apply.

Statement purity resolves a coercion against these types, and against the
values synchronous execution has settled for a top-level binding no other
statement writes, when the operand types make the specific operation
non-throwing: a Symbol is a safe property key but throws as a string or numeric
operand (templates and string concatenation included); BigInt mixed with
another numeric kind in arithmetic, and unary `+` and `>>>` on a BigInt throw;
BigInt arithmetic that can divide by `0n`, take a negative exponent, or grow
past the engine's size limit may throw, so only comparisons, equality, bitwise
`&`/`|`/`^` and string concatenation resolve on BigInt operands. An operand known only to be some primitive does not resolve. A
hoisted `var` read before its initializer counts as `undefined`. No value is
folded, and every binding read, import-order and TDZ constraint remains.

Raw stable export kinds remain available while the program summary resolves
copies and re-exports, but they do not cover an importer's normal `GetValue`.
The execution-facing imported-value adapter includes a primitive value only
when the same local binding has a matching read capability. That capability
requires an analyzed stable primitive export, an exact ordinary ESM
importer-to-provider request, and a fully resolved static dependency closure of
the provider that does not reach the importer. Unresolved, unsupported,
non-entry, or importer-reaching dependencies leave the imported value
unavailable to ordinary candidate execution. The capability retains every
local export cell that supplied the value through copies and re-exports;
aliases do not erase that provenance. It authorizes only the reached imported
binding read's existing module-read effect coverage. It does not certify
provider effects or general ESM completion. Until that completion is modeled,
raw primitive kinds do not stand in for a normal imported value or its read
effects.

Guarded native call summaries share one checked projection for call timing
and successful return values. The summarized operations, the argument counts
and operand shapes they admit, their allocations, writes and return shapes are
the rows of `facts/abstract_domain/builtins.rs`, each with its ECMA-262
citation. A call is admitted only when every coercion its row lists lands on a
proven primitive or exact ordinary operand. A row returning a primitive
gives its result's types; a row returning an argument keeps that
argument's identity and the callbacks stored in it; a row with an untracked
result stays opaque.

The intrinsic must be undeclared and unshadowed. Dynamic scope or visible
writes to its intrinsic/global family, including aliases and reflective writes,
disable the row. Spreads and unsupported input shapes remain conservative.
Callee and argument evaluation retain their effects and throws. These summaries
neither classify calls as pure nor bypass their ordinary source-order constraints.

## Purity hint diagnostics

The per-chunk `modules.json.redundant_purity_hints` report disables each tested
`purity: pure` hint before checking redundancy. Its `proof_basis` distinguishes
`without_purity_hints` from `with_other_purity_hints`; the latter is conditional
on retaining other chunk-local purity hints. These diagnostics do not change
acceptance or edit the spec. Remove hints individually and revalidate. A missing
entry is inconclusive. Probe scope: <docs/purity_soundness.md#redundant-purity-hints>.

## Chunk-export contract audit

`debundle spec audit-contracts` loads the current flat or tree spec and source,
requires one exact `chunk_export_purity` policy key and a positive finite
`--max-probes`, and runs the ordinary full dry-run acceptance pipeline. A
failing baseline stops the audit before any removal verdict. Each selected
contract is then removed alone from a fresh in-memory copy of the same
authored spec and the full pipeline is recomputed, including vendor resolution,
assignment, lowering, and final reconstruction validation. No saved graph is
accepted as proof. The command writes no authored spec, emitted output, or
probe graph. It compares complete captured input provenance between baseline
and probes and verifies those files again before reporting. The JSON report
includes `input_provenance_sha256` for the exact captured input set and engine.

The target identity is typed: policy key, contract kind, export, optional
member, and optional callback signature. A `source_markers` policy key remains
a policy label, never a fabricated chunk ID. Filters `--kind`, `--export`,
`--member`, and `--signature` select exact targets; signatures use the typed
JSON object with `argument_count` and `callback_arguments`. Every selected
target beyond the budget is reported as `budget_skipped`.

When a probe removes a `namespace_independent_members` member, it also removes
the matching dependent `source_callable_members` assertion so the candidate
spec retains the required subset relation. Probing a source-callable member
removes only that identity assertion.

`removable_under_remaining_assumptions` means only that the configured source,
spec, and assignment still pass full acceptance after this one removal while
all other author contracts and runtime assumptions remain. It does not prove
the removed contract independently inferred or pure. Individually removable
contracts are not necessarily jointly removable. `failed_full_acceptance`
means the changed candidate failed the shared pipeline; it does not establish
that the contract is semantically true or uniquely necessary. A failure before
input capture is `inconclusive`. The audit does not apply removals.

Optional `--checkpoint` creates a new record; `--resume-from` loads and updates
one. Both modes hold an exclusive kernel lock on a stable sidecar. Resume always
runs fresh full baseline acceptance, then compares complete `GraphProvenance`
and normalized selection before reusing definitive target results. Inconclusive
results are retried; budget skips are never stored. `--max-probes` remains the
total deterministic prefix, including reused results; a smaller cap retains
saved results outside the reported prefix. Verified definitive results are
saved by atomic sibling-file replacement, with no power-loss durability claim.
The final report schema and independent-removal semantics remain unchanged.

## Source-anchored export policies

`chunk_export_purity` normally keys contracts by the defining chunk ID.
An optional `source_markers` list instead identifies that provider by a
conjunction of purpose-bearing substrings; the key then serves only as a
policy label:

```yaml
chunk_export_purity:
  lazyRegistry:
    source_markers: [neutral-lazy-registry, private-callback-storage]
    pure_exports: [factory]
    inline_callback_factory_exports: [factory]
```

Matching uses the loaded provider entry's current analysis source before
logical-module lowering: exact input text for pass-through source entries,
including entries retaining an AST solely for read-only premise analysis,
or the current staged AST's rendering for other AST entries. Mutable access to
a read-only retained AST discards its original-source representation before
exposing the staged body. Matching does not
read filenames or original text from an earlier parser snapshot. Every marker
must be nonempty, and the conjunction must identify exactly one loaded
provider, including providers outside the selected chunks' import closure.
Zero/multiple matches, an empty list, whitespace-only markers, and multiple
policies resolving to one provider are spec errors. An explicit chunk-ID
policy and a source policy for the same provider also conflict.

Only the policy map is resolved to the existing provider ID. This does not
alias, select, rename, split, relocate or rewrite the provider, and it grants
no admission override. It can therefore configure an analyzed but unselected
provider whose `import.meta` context must remain in its existing output file.
Existing input/output relocation rules remain unchanged. The markers identify
a provider; they do not prove its contracts. Each purity, callback, intrinsic
and publication premise still requires the same independent source audit.

## Inferred native property-key domains

Native mutation analysis distinguishes finite string keys, affixed string
keys, definitely Symbol keys and unknown keys. Stable aliases of unshadowed
`Symbol.for(value)` and lexical `[key, value]` loops over closed data-object
`Object.entries` inputs can preserve unrelated named global bindings. The latter
requires intact Object operations and Array iterators for both the result and
pair destructuring. `Symbol.for` returns a Symbol whatever its argument; a
throwing conversion leaves no key.

An affixed key is a string with a constant prefix and suffix: an untagged
template with substitutions (its first and last literal parts), or `left +
right` where an operand is a string literal, template, affixed key or stable
alias of one. `+` with a string operand is string concatenation, so the result
starts with a string left operand's prefix and ends with a string right
operand's suffix; the other operand's conversion may run user code but cannot
change those characters. A numeric literal is a key, not a string operand. An
affixed key may name exactly the strings at least as long as its affixes that
start and end with them. Mutation directly on a native object still invalidates that
family's prototype slots, regardless of key domain.

`Object.assign(target, ...sources)` writes finite string keys when every source
is an object literal of data properties and methods with static keys (no
spread, getter, setter or `__proto__` in any spelling): it copies exactly those
own enumerable keys, as successive assignments. Any other source makes the
write keyless.

A11 supplies initial native identity. All analyzed chunks contribute their
canonical mutation facts, including deferred writes, before resolving a closed
write invariant and propagating invalidated operation dependencies to a fixed
point. Providers and consumers share this inferred integrity input; it is not
a spec assumption or configurable override. Unknown keys, unstable bindings,
shadowing, unsupported patterns and visible native replacement fall back
conservatively. Global-slot value reads retain unresolved native identity
through aliases and descendants, so a write through one may reach any native
object, under its key; a bundle-owned global's value (§ Bundle-owned globals)
reaches none. A11 remains an author obligation for hidden
native-alias paths, including a descriptor method called through an alias.
Key precision grants no call, purity, throw or return summary.

## Captured ordinary-object values

`chunk_analysis_options.<defining chunk>.value_inputs` can attach a typed
`ordinary_object` value premise to one provider binding:

```yaml
chunk_analysis_options:
  static/peer:
    value_inputs:
      - source_match:
          match: "var captured = globalThis.runtimeSettings.state;"
          bindings: [captured]
        value:
          kind: ordinary_object
        audited_source_sha256: "<64 hex characters for the parser input>"
```

`source_match` must resolve through the canonical selector engine to exactly
one declared placeholder binding. `audited_source_sha256` must match the exact
bytes recorded as the provider's parser input, including when the graph was
freshly recaptured. `expected_export`, when present, must name a direct export
of that exact local binding. The provider must be loaded, non-excluded and in
the existing analyzed static-request closure, which follows runtime imports
and sourced named/star re-exports. It may remain unmaterialized; this option
does not select it as an emission root or change output ownership.

The author audits that the successful initializer's direct value is an ordinary
non-Proxy object whose identity differs from the actual global object and every
native constructor, prototype or object identity represented by the census.
This says nothing about purity, reentry, throws, allocation freshness,
properties, descendant identities or object lifetime. The object may have a
native prototype, contain native values or expose getters and callbacks. Its
initializer and all reads, effects, callback calls, throws and ordering remain
in the ordinary analysis.

The current source-derived boundary accepts only a top-level simple binding
initialized by an opaque binding-rooted member read. The canonical declaration
and write index must establish one declaration and no possible reassignment,
update, repeated declaration, destructuring write, loop write, deferred write
or dynamic-scope contamination. A recognized global or native identity
contradicts the contract and fails; unsupported initializer categories are
rejected rather than interpreted as values. Stable aliases are propagated only
when the existing index proves their availability and write stability.

A mutation through the direct captured value discharges only that receiver's
prior `unknown_global_value` possibility. The census still emits an author-
conditioned witness, including when its affected-root set is empty. A member or
descendant of the captured object remains `unknown_global_value`, including
computed, symbol and `constructor` paths. Calls or construction involving a
captured value are widened only when the existing value facts show that input
is involved. A synchronous local callable may return a member or descendant
rooted in the selected binding. The shared value-flow closure carries that
opaque possible-native descendant through local aliases, synchronous wrappers
and supported object or array literal projections. Returned containers do not
inherit the captured binding's ordinary direct identity; a container field
creates this descendant flow only when it carries the selected input. Every
reaching assignment producer and return site in a capture-connected flow must
close. Unknown parameters, unresolved recursive paths and unsupported escapes
retain their possible-native roots and cause the input contract to be rejected
when they prevent a closed summary. A synchronous callable whose returns are
all statically primitive is also accepted; those results do not create
captured-object aliases. Captured async or generator callables remain
unsupported. This is an intra-chunk analysis over the existing value
expressions, not a general return or cross-module alias analysis. A write
through a descendant therefore retains the usual global/native barrier, and
recognized native writes elsewhere in the analyzed program remain effective.

Legacy own-syntax heap opacity is also omitted for an initializer carrying an
exact same-owner `CompletedInitialization` receipt. The shared interpreter
issues this receipt through either the exact non-invoking initializer proof or
normal root completion with covered general heap, global-property and host-effect
domains. The non-invoking proof admits only checked allocation-only expressions,
lazy functions, inert classes, RegExp literals and exact supported native
constructor forms. Both routes preserve source-owner identity and initialized
bindings. This removes only the own syntax-opacity bit: explicit heap cells and
allocations, receipt dependencies, callee heap opacity and incomplete-execution
barriers remain.

The existing exact private frozen-namespace recognizer can also cover its whole
initializer through the root-effect route. It checks the fresh null-prototype
data object, primitive descriptor, stored values and exact native call sites.
Coverage additionally requires intact Object/Symbol slots and Object.prototype,
since inherited descriptor getters can execute during `Object.defineProperty`.
Those census dependencies remain on the receipt; stored callbacks retain their
later reachability and invocation effects.

The census records each input reference actually consumed by a receiver
refinement. Those dependencies flow into successful native guards, native-call
identity uses, inferred imported returned-record certificates and uses, and
the separate timing, purity and heap results for Set or local-class proofs.
Heap reports associate each use with the source unit where the certified
construction removes its opaque heap barrier. A source-only local-class proof
carries no value-input use. The source digest
binds the input to parsed bytes, but it does not establish the external runtime
audit, code outside the analyzed prefix, or host behavior.

## Native integrity inputs

A native premise is an integrity flag on an intrinsic slot: `%Set%`,
`%Object.freeze%`, `%Array.prototype%`, `%ArrayIteratorPrototype%`. A slot is
intact when no write in the closed whole-program write census (§ Inferred
native property-key domains) can reach it; it is unknown otherwise.

An indexed eager static read of an unshadowed native binding's known own data
property shares its exact binding and property-slot proof across synchronous
execution, purity and heap ordering. This includes reads in computed class
keys, such as `Symbol.dispose`. Receiver and key evaluation, other class
initialization effects and invocation of the resulting value retain their
ordinary checks. A shadowed binding, dynamic scope or unproved slot integrity
keeps conservative lookup effects. Exact author inputs retain their declared
operation and source-site scope.

The read-only `debundle explain-integrity` query reads a saved dependency graph
and its linked program graph without reanalysis. `--limit N` bounds reported
precision losses per source unit, after `--at` filtering, and census witnesses
per slot. Each query row reports exact `total`, `emitted` and `truncated`
counts in `precision_loss_limit` or `witness_limit`; `--all` shows every row.
These bounds affect only the query output: saved graph precision losses and
census witnesses remain complete. A witness's `possible_discharges` names
candidate contracts for author audit; it does not record configured or
consumed premise evidence. Details: <docs/cli.md#native-integrity-guards>.

A `%Set%` or `%Object.freeze%` slot is an own data property of the global
object or of a census constructor or namespace. A property write, update,
delete or reflective definition changes only its receiver's own property under
its key: a setter or Proxy trap it runs is analyzed source with census writes
of its own, and A11 excludes host code changing intrinsics. So:

- A string key reaches only slots its domain may name, on every object its
  receiver may be; a Symbol key reaches none. Slot names are the finite set of
  census global bindings and builtin static names, none of them `__proto__`:
  a `__proto__` write changes the receiver's prototype, not an own slot, and a
  key whose write runs an accessor runs analyzed source or host code (A11). `Error.prepareStackTrace = f` leaves `%Error%`
  intact, and `location.href = url` leaves every own slot not named `href`
  intact.
- A global-object receiver reaches only global slots. A constructor or
  namespace receiver reached through its intact global binding reaches only
  its own slots; through a replaced one it may be any object.
  `%Object.prototype%` writes reach no `%Object.<name>%` slot.
- An unknown key reaches every slot of its receiver. A computed key or
  iterator-prototype path is exact only while every slot it was derived
  through is intact.
- Assigning a census name replaces its global slot. Rebinding a global-object
  alias (`globalThis = other`) leaves every slot unknown.

A prototype slot covers every property on the object, including inherited
lookups, so it is unknown after any write to its family or the global object.

Two narrower slots name one own property each, under the same reach rules:

- `%Array.prototype.map%` is the string-keyed own property `map` of the
  prototype object. Writes to it come from an exact `%Array.prototype%`
  receiver (exact while `%Array%` is intact) or from a receiver that may be any
  object, under a key that may name it. The global object and the constructor
  are different objects, so their writes never reach it. A family root mutated
  without a witness reaches every property.
- `%Array[@@species]%` is the constructor's `@@species` accessor. The census
  does not tell Symbol keys apart, so any Symbol- or unknown-keyed write that
  may land on `%Array%` reaches it.

The Array iteration rows require only their method's slot. A row whose result
array comes from `ArraySpeciesCreate` also requires `%Array.prototype.constructor%`
and `%Array[@@species]%`. They need neither `%Object.prototype%` nor the Array
iterator:

- the receiver is a dense array whose indices below `length` are own data
  properties, so `HasProperty` and `Get` never reach its prototype chain;
- `length` is own;
- no row's algorithm reads the iterator.

Reads the callback performs, such as an element's property, are member reads
the interpreter checks separately.

Each consumer names every slot its operation reads: the callee's binding and
member, and any constructor static or prototype method the algorithm looks up.

Two author fields instead assert slots at one exact source site, as an
`IntrinsicIntact(slots, site)` axiom:

```yaml
chunk_analysis_options:
  static/peer:
    native_operation_inputs:
      - source_match:
          match: "const marker = Symbol('marker');"
          bindings: [marker]
        operation: symbol
        audited_source_sha256: "<64 hex characters for the parser input>"
        note: "The original Symbol data binding is stable during chunk initialization."
    native_call_identity_inputs:
      - source_match:
          match: "function makeRecord(name) { return Object.freeze({ name }); }"
          bindings: [makeRecord]
        callee: object_freeze
        audited_source_sha256: "<64 hex characters for the parser input>"
```

A `native_operation_inputs` entry gives exactly one selector: `source_match`, or
`resolved_member: { module: <logical module path>, name: <public member name> }`,
which reuses an already resolved logical member's canonical selector result in
the same provider chunk. A `source_match` names one declared binding and
selects the declaration site its template matched (§ Repeated `var`
declarations). A `native_call_identity_inputs` entry selects the declaration
holding the call. `audited_source_sha256` must match the provider's parser input,
as for `value_inputs`; the report retains the selector provenance, digest, spec
location, selected binding and site.

After selector resolution, a `resolved_member` native premise contributes its
provider chunk, original binding and prepared-body declaration index, operation
and audited parser-input digest to analysis identity. Renaming the logical module
or public member while selecting the same declaration preserves projection and
artifact bytes. Changing the selected declaration or premise invalidates saved
analysis. Reports render the current authored selector and rationale even when
the facts came from a saved artifact.

| Input                                | Selected source                                                                                                                                                            | Asserted slots                                                              |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| `text_encoder_empty`                 | Top-level simple binding initialized by direct bare zero-argument `new TextEncoder()`                                                                                      | Original global `TextEncoder` data binding                                  |
| `map_empty`                          | Top-level simple binding initialized by direct bare zero-argument `new Map()`, or an object containing only such data fields                                               | Original global `Map` data binding at each selected construction            |
| `weak_map_empty`                     | Top-level simple binding initialized by direct bare zero-argument `new WeakMap()`                                                                                          | Original global `WeakMap` data binding                                      |
| `set_empty`                          | Top-level simple binding initialized by direct bare zero-argument `new Set()`                                                                                              | Original global `Set` data binding                                          |
| `symbol`                             | Top-level simple binding initialized by a direct `Symbol(...)` call with zero or one syntactically primitive argument                                                      | `%Symbol%`                                                                  |
| `symbol_for`                         | The same with `Symbol.for(...)`                                                                                                                                            | `%Symbol%`, `%Symbol.for%`                                                  |
| `promise_resolve`                    | Top-level simple binding initialized by a zero-argument `Promise.resolve()`                                                                                                | `%Promise%`, `%Promise.resolve%`                                            |
| `set_from_primitive_array`           | Top-level simple binding initialized by `new Set([primitive literals or earlier stable primitive bindings])` over a dense, nonspread array                                 | `%Set%`, `%Set.prototype%`, `%Array.prototype%`, `%ArrayIteratorPrototype%` |
| `set_from_set_spread`                | `new Set([...seed, primitive literals])` in the same declaration as a selected `set_from_primitive_array` seed and every declarator between; all stay in one output module | Those slots and `%SetIteratorPrototype%`                                    |
| `object_own_data_function_read`      | Top-level simple binding initialized by `Object.<name>`, a function-valued data property in the builtin summary table                                                      | `%Object%`, `%Object.<name>%`                                               |
| `symbol_well_known_class_keys`       | Top-level simple class-variable initializer with direct `Symbol.asyncIterator`, `Symbol.asyncDispose` or `Symbol.dispose` computed ordinary method keys                    | `%Symbol%` and the exact `%Symbol.<name>%` at each selected read            |
| `symbol_primitive_class_keys`        | The same direct class-key shape, allowing primitive own-data values installed by checked guarded fills                                                                     | Exact data reads yielding unknown primitives; no original-value identity    |
| `array_spread_from_primitive_arrays` | Last declarator of a `const` whose earlier declarators are dense primitive arrays and whose initializer holds primitives and spreads of them                               | `%Array.prototype%`, `%ArrayIteratorPrototype%`                             |
| `callee: object_freeze`              | Top-level function whose sole statement returns `Object.freeze(arg)` with one nonspread argument                                                                           | `%Object%`, `%Object.freeze%` at that call                                  |
| `callee: set_empty_private_field`    | Stable top-level class with an implicit constructor and one private field initialized by zero-argument `new Set`                                                           | `%Set%` at that constructor                                                 |

Every call is ordinary, nonoptional and uncomputed on an unshadowed global;
other shapes, dynamic scope and positively identified writes to an asserted
slot anywhere in the analyzed program (alias, reflective, deferred,
cross-chunk, restored) reject the input. Opaque receiver or key writes remain
census witnesses covered by the assertion: a write to an apparently unrelated
property can still run a setter or Proxy trap.
The separate `symbol_primitive_class_keys` capability has the narrowly checked
guarded-fill exception below and does not assert original native slots.

A validated `symbol` site has an additional pure-initializer consequence only
for direct `Symbol()` or `Symbol('literal')`; `symbol_for` requires exactly one
direct string-literal argument. Other permitted primitive arguments retain
their ordinary conversion and effect checks.

For `set_from_primitive_array`, an identifier element must resolve to the same
canonical binding as one earlier direct top-level simple `var`, `let` or
`const` declarator initialized by a primitive literal. The completed source
census must find no other declaration, assignment, destructuring rebind or
update to that binding. This proves primitive type at the selected constructor
site, not the exact element value; ordinary eager reads, TDZ and source order
remain in force. Member reads, calls, spreads, holes and unknown values do not
qualify.

The author asserts that the slots keep their original native identities at
**every** synchronous initialization execution of the site in every schedule
emission allows, including reentry. It is not a snapshot: a later or deferred
replacement could move before the site in an emitted schedule. At that site the
slots' flags read author-asserted, and a consumer that would otherwise need the
census takes the axiom instead, recording the input in
`native_operation_input_uses` and its proof dependencies. A `promise_resolve`
input applies only together with `host_behavior.reentry_free_operations:
[promise_resolve]`. An asserted operation still evaluates its callee and
arguments with their effects and throws; the input grants no family-wide
integrity, purity, return shape or guarantee at another site, and A11 remains
the external obligation for code outside the analyzed program.

Without an authored input, the engine can infer primitive Set construction from
the completed conservative native-write census under A11 and any validated
`intrinsics_never_written` premise. A protected `new Set([...seed, primitives])`
also requires an earlier fresh primitive-array Set in the same original
declaration, unique stable simple bindings, and independently proved primitive
Set constructors for every intervening declarator. It requires all five protocol
slots listed above, including `%SetIteratorPrototype%`; source shape and native
constructor identity alone do not suffice. This proof is completed before the
single structural value-flow solve, retains inference/premise provenance rather
than an authored input reference, and adds an atomic placement constraint for
the entire source interval to all graph-backed edits and emission.

Spread and construction execute one shared native iterable operation. Its result
distinguishes exact array slots from finite primitive-only yields of unknown
cardinality and order. A successful native Set construction retains only its
fresh Set allocation kind and source origin. Neither result establishes Set
entries, size, properties, uniqueness, or exact array indexes/length. Runtime
admission still checks the evaluated operand, source identity, prefix mutation
and escape state, and protocol proof. The operation receipt covers its modeled
body only; operand effects, abrupt completion and terminal initialization
coverage remain separate.
Checked ordered Set-spread roots evaluate each operand once and compose array,
iteration and construction fragments. Inferred integrity and an exact validated
`set_from_set_spread` input use this same transfer. The input supplies native
identity and protocol evidence; actual operands and normal completion remain
checked, and the selected input reference stays in the construction receipt.

The newly inferred protected Set spread admits only the canonical identifier
operand naming its earlier checked constructor. It cannot infer this contract
for a Call, New, getter/member read or arbitrary computed spread source. Generic
ArraySpread outside a checked ordered root still uses the flattened operand/value
projection boundary: those projections are separate runtime requests, with no
cached operand reuse or proof of a single abstract evaluation. Unsupported effectful sources retain
conservative fallback; preserving one observable Node invocation does not prove
the interpreter projected that source only once.

`text_encoder_empty` asserts the original standard constructor through an ordinary
global data binding at this exact site. The checked shape excludes aliases,
subclasses, qualified/global-property callees, arguments, spreads, shadowing,
and sequence or assignment wrappers. Parentheses after `TextEncoder` are optional;
one binding in a comma declaration may be selected. Source-known global `TextEncoder` writes
in any analyzed chunk contradict the input; opaque mutation witnesses remain
under the author's audit. The original constructor performs no user callbacks
with zero arguments. Its result stays opaque: later property reads, getters,
`.encode` calls and prototype behavior receive no certification. No
`host_behavior.reentry_free_operations` entry is needed. `TextEncoder` belongs
to the native write census, but is outside `program_premises`'
`intrinsics_never_written` family vocabulary, including `families: all`. Syntactic purity requires this
exact-site input and records its dependency; an unannotated construction keeps
conservative purity even when the ordinary census proves its callback timing.

`map_empty`, `weak_map_empty`, and `set_empty` use the same exact direct-
constructor shape and global data-binding assertion for `Map`, `WeakMap`, and
`Set`. A known write to the selected global constructor binding in any analyzed
chunk rejects the input. Zero-argument standard construction returns its fresh
empty collection before looking up an adder or iterator; its existing builtin
row supplies operation timing. The exact-site identity input does not certify
later collection methods, property reads, iteration, callback retention or
mutations, and does not grant purity or integrity to another site. Aliases,
shadowing, arguments, spreads, qualified callees and sequence or assignment
wrappers remain unsupported. Each selected declarator carries its own
assertion, including comma declarations.

`map_empty` also accepts one unambiguous top-level simple binding whose
initializer is a nonempty object literal containing only noncomputed ordinary
data fields, each directly initialized by bare zero-argument `new Map()`:
`var EMPTY = {counts: new Map(), statuses: new Map(), latest: new Map()};`.
One assertion covers every constructor site in that initializer, with separate
exact-site evidence and operation receipts. Fresh and restored analysis validate
the same original field syntax and call spans. Each construction must read the
original global `Map` data binding in every admitted initialization schedule;
the audited parser-input digest remains required. Spreads, accessors, methods,
computed keys, `__proto__` fields, empty objects, nested values, other calls and
wrapper expressions are rejected. Keys may be identifiers, strings or numbers.
Shadowed or imported constructors and known contradicting global `Map` writes
remain rejected. The object and its fields retain ordinary value and heap
inference; the input does not make the selected binding a Map or certify heap
locality, later collection behavior, or program-wide native integrity.

`symbol_well_known_class_keys` selects one unambiguous direct top-level simple
variable initialized by a class expression without `extends`, decorators or
static blocks. At least one computed method key is required. Every computed
key of that class must be an ordinary method key whose expression is exactly
`Symbol.asyncIterator`, `Symbol.asyncDispose` or `Symbol.dispose`, with an
unshadowed bare global `Symbol` receiver and uncomputed member access. The
supported names must also have primitive own-data rows in the builtin table;
other primitive properties, function/object properties, computed fields,
computed accessors, aliases, wrappers and other computed expressions are
rejected. For example:
`var Stream = class { [Symbol.asyncIterator]() {} [Symbol.asyncDispose]() {} };`.

The author promises the original global `Symbol` data binding and original
selected own data properties at each exact key read in every admitted
initialization schedule. The audited parser-input digest is required. Known
writes to the global binding or a selected property anywhere in the analyzed
program, including deferred and cross-chunk writes, contradict the input;
opaque mutation witnesses remain under the author's audit. Each read has its
own resolved record, site axiom, evidence and proof dependencies, including
after saved-analysis restoration. The existing own-data-read proof governs
callback timing and heap ordering at that read. Instance field initializers,
method bodies and static field initializers retain their ordinary analysis.
The input establishes no whole-class purity, class result shape, heap locality,
future method behavior or integrity at another Symbol read.

`symbol_primitive_class_keys` uses the same class shape and three supported
property names, but asserts a different read contract: at every selected key
read in every admitted initialization schedule, the unshadowed global `Symbol`
read resolves through a data binding to an ordinary non-Proxy receiver and
the selected own data property yields a primitive. The primitive may vary with preceding writes. Native Symbols and
Symbol-valued polyfills qualify; no original well-known value or particular
primitive type is certified. The parser-input digest remains required.

This capability is separate from `IntrinsicIntact` and authorizes neither
original native identity nor any other read or call. Its abstract result is an
unknown primitive. Getter invocation and `ToPropertyKey` callbacks are ruled
out only at the selected reads. Each read retains separate evidence and proof
dependencies, including after saved-analysis restoration.

Global `Symbol` replacements and other positively identified selected-slot
writes in any analyzed chunk reject this input. The sole eligible selected-slot
write is a direct top-level statement of this exact AST shape (either `!=` or
`!==` is accepted), for one of the three supported property names:

```js
if (typeof Symbol.asyncDispose != "symbol")
  try {
    Symbol.asyncDispose = Symbol.for("Symbol.asyncDispose");
  } catch {}
```

The receiver must be bare and unshadowed; the probe, assignment key and string
argument must agree. There may be no alternate, additional guarded work, catch
binding, nonempty catch, finally block or wrapper expressions. Recognition
makes the write eligible under the author's final-read contract; it proves
neither that installation succeeds nor that `Symbol.for` is original. The
guard read, call, assignment/setter and catch retain their ordinary effects,
and the raw native write census remains intact.

The selected reads retain opaque external-heap observations and ordinary heap
ordering with preceding and following writes. External storage has no unique
source-allocation birth: its observation seeds the chunk's heap timeline before
any source allocation, including conservative deferred observations. The
required saved heap fact retains this obligation, and initializer completion
cannot discharge it. Static initializers and deferred instance code remain
ordinary; the capability grants no whole-class purity, class shape or heap
locality. It may remove callback constraints while heap ordering still requires
co-location.

### Bundle-owned globals

Under `host_behavior.predefined_globals: standard` or `bun` a write through the value of
a bundle-owned global property reaches no intrinsic slot, whatever its key.
Without that key every global-slot read-back stays an unknown value.

A property `g` of the global object is bundle-owned when it is not in the
host's list (§ Predefined globals), is not a census binding name, and every
census write that may store into `g` stores a fresh object (inline, or a
top-level binding every write of which stores an object literal, a primitive, or
the result of an inline function whose every `return` is one), a primitive, a
fresh `Map`, `Set`, `WeakMap` or `WeakSet` (when no write may replace those
bindings), or the value of a bundle-owned property, `g`'s own included:
`root.g = {}`, `root.g = root.g || {}`, `root.g ??= new Map()`,
`root[key] = root[key] ?? { ... }`. A property analyzed code only reads, such
as `window.componentHandler`, qualifies vacuously. A store that may name `g` is any write
whose key domain may be `g` and whose receiver may be the global object, an
unknown value, a native descendant or a prototype. Properties keyed by
`Symbol.for` symbols form one bundle-owned slot, read through a stable binding
with a `Symbol.for` key domain, while no write may replace `%Symbol%` or
`%Symbol.for%`.

The set is a greatest fixed point over every analyzed chunk's writes, since
all chunks share one global object: a write through a bundle-owned value is not
itself a store into a global or prototype. Every property is dropped if any
global-chain write has an unknown key or may name `__proto__`, if a keyless
reflective write (`Object.assign`, `Object.setPrototypeOf`) reaches such a
receiver, or if a call on one has a method key whose domain may spell
`__defineGetter__` or `__defineSetter__` (an unknown computed key included).
`__lookupGetter__` and `__lookupSetter__` install nothing. A sloppy assignment to an undeclared `g` is a store into `g`;
updates, destructuring and reflective writes naming `g` drop it, as does a
`delete` that may name `g` on the global object. A `delete` on a prototype
stores nothing.

`chunk_analysis_options.<chunk>.no_accessor_install_callables` discharges that
check for computed-key calls inside one audited function:

```yaml
no_accessor_install_callables:
  - source_match:
      match: |
        var runFunc = ne((t, ...e) => { ... }, "runFunc");
      bindings: [runFunc]
    note: Every caller passes only strings.
```

`source_match` selects one declaration whose statement contains exactly one
outermost function (or accessor) with at least one computed-key method call.
The entry covers every
computed-key method call in that function, its nested functions included. The
author asserts that no activation of those calls installs an accessor, for example because every argument that reaches
`__defineGetter__`'s callable position is a primitive, which throws `TypeError`
before installing. Every proof using the bundle-owned set records the entries
that discharged a call.

Soundness, by induction over execution: initially `g` is absent from the
global object and, by the host claim, from its host prototypes, or is a named
property; the `%Object.prototype%` names are in the list. Every later store
into `g`, on the global object or a prototype it inherits from, writes a
non-intrinsic value, so `g` never holds an intrinsic; a `delete` only removes.
Writes through `g`'s value land on non-intrinsic objects, so they cannot store
into `g` on the global chain either. A property no write names stays absent or
a named element: a call through it throws or runs on that element, and an
accessor it installs lands on the element, never on the global object or an
intrinsic slot. Setters and Proxy traps a write runs are
analyzed source with census writes of their own; A11 excludes host code
changing intrinsics or adding globals, and covers hidden native-alias paths as
for every census fact: an aliased descriptor method, or a write through a
parameter or container field that holds the global object. Analyzed
chunks are ES modules, so top-level declarations create no global property,
and dynamic scope leaves the census open. A descendant of `g`'s value stays
unknown. Proofs that use the discharge record `predefined_globals` among their
host behavior dependencies.

## Commuting statements

`chunk_analysis_options.<chunk>.commuting_statements` certifies that two source
units of that chunk are order-independent during synchronous initialization:
neither observes the other's effect, so emission may schedule them in either
order.

```yaml
chunk_analysis_options:
  static/app:
    commuting_statements:
      - a:
          source_match:
            match: "decorate([], Widget.prototype, 'state', 2);"
        b:
          resolved_member: { module: app/settings, name: settings }
        note: "The decorator installs an own property the settings initializer never reads."
```

Each endpoint (`a`, `b`) is exactly one selector in the `native_operation_inputs`
grammar: `source_match` or
`resolved_member: { module: <logical module path>, name: <public member name> }`.
A `source_match` without `bindings` selects one anonymous (non-declaring)
top-level statement; with one binding it selects that binding's declarator in
the statement its template matched — inside a comma declaration the one
declarator the binding belongs to, and for a binding declared at several `var`
sites the matched site rather than the binding's first (§ Repeated `var`
declarations; a template matching several sites is ambiguous). This is how a
later `var` site becomes an endpoint: `resolved_member` reuses the member's
canonical selector result in the same chunk, which is the first site. Every
endpoint resolves through the shared selector engine and must
identify exactly one post-split source unit of the option's chunk. An endpoint
that does not resolve, resolves to several source units, or names a member of
another chunk is a spec error that fails `spec validate` and `run` with a
diagnostic naming the entry and endpoint (`commuting_statements[<i>].a`). A
pair whose endpoints are one source unit, a pair repeating another entry's
unordered source-unit set, and an entry for a chunk the run does not
materialize are errors. Entries are resolved when their chunk's dependency
graph is built and never reach analysis, so editing them leaves every
analysis fact unchanged. An entry carries no parser-input
digest: the two structural selectors pin the statements. When a selector no
longer resolves on a new snapshot, validation fails like any member's; the
premise is never silently kept.

The premise makes inert only the `sequenced` (side-effect ordering) edges
between the two selected source units, in either direction. The dependency
graph still records each such edge as what source analysis found, with
`constrains_init_order: false` and `commuting_premise` naming the entry, and
`dependency_graph.json.gz.commuting_statements[]` lists per entry the edge ids it
inerted and the spec location; a blocking SCC in `cycles.json` lists the inert
edges between its members as `inert_commuting_edges`. The premise never
touches `eager_use` edges: an at-init read is a dataflow dependence whose
reordering changes behavior, and the remedy there remains co-location.

The assertion certifies only its selected pair. The graph preserves ordering
against third source units when a compacted ordering path used an inert edge.
For example, removing `b -> a` from `c -> b -> a` retains `c -> a` unless that
pair is separately certified. Added `sequenced` edges carry a `preserved_order`
cause naming the intermediate source unit and the premise on the path. This
preservation repeats across overlapping premises, independently of their order
in the spec. An entry may discharge an edge exposed by another entry; unused
assertions are checked after preservation finishes.

`spec propose-commutations` uses the same preservation closure for its
single-pair cut predictions. Its combined-proposal analysis may stop after
proving a surviving cycle; `counts_are_lower_bounds` then marks the reported
counts and cyclic memberships as incomplete. An acyclic prediction requires
completed preservation. These predictions do not certify an author's assertion.

An entry applies to ordering whose effect analysis is opaque at an endpoint
or on the preserved path. It is rejected when the analysis derived a source-known
effect-set conflict, read from each `sequenced` edge's retained cause
(§ Realizability cut explanations) and the two recorded effect summaries: a
`cell_order` edge with a `read_after_write`,
`write_after_write`, `write_after_read` or `use_after_allocation` relation; a
`binding_timeline`, `shared_var_access` or `uninitialized_observation` edge
whose two binding effects are both `observed`; two summaries sharing a cell (a
binding, a static `globalThis` property, array or Set contents) with at least
one write; or a shared `var` declaration.
For this premise check, each summary includes the statement's own effect cells
and heap-content reads and writes of callees invoked on non-assumed paths.
These are modeled may-accesses; the check does not assert that every call or
access executes at runtime. Callee cells contributed only by an assumed
callback or fallback path remain conservative ordering evidence and do not
contradict a premise. Fresh allocations are separate from content writes.

A `cell_order` edge carrying only barrier relations (`after_barrier`,
`barrier_after_prior`, `frontier_after_cell`), a `strict_chain` edge with
an endpoint that is not dataflow-summarizable, and a binding-ordering edge
with a `fallback` effect exist because an effect is
opaque (a call the classifier cannot prove pure, an escaped global-object
alias, a dynamic key, incomplete synchronous execution, an opaque heap
frontier); those the premise makes inert.

A binding effect is `observed` when it comes from the statement's syntax
(declaration, initializer, eager read or rebind, local effect) or from
synchronous execution of code the statement definitely runs. It is `fallback`
when only a conservative path contributed it, one that assumes code may run at
the statement; the `reason`'s `path` names the first such path:

- `fallback_summary` (`loss`: the precision loss that opened it): having
  stopped expanding callbacks (`callback_pool_limit`, `recursive_invocation`,
  `invocation_depth_limit`, `invocation_expansion_limit`) or handed a
  returned callback to opaque code, the interpreter attributes each such
  callback's whole synchronous read/rebind closure to the statement.
- `opaque_callback_invocation`: a callback opaque code may invoke (escaped
  earlier, exported, or reachable from the opaque operation's inputs),
  executed as if it ran.
  A member read whose abstract receiver is the global object records its
  global-property read but does not identify which escaped callback, if any,
  occupies that slot. The result stays opaque; if called, ordinary unknown-call
  handling still accounts for the escaped callback pool under this assumed
  path. A separately known function alternative in a mixed known/opaque callee
  keeps its own observed effect provenance.
- `escaped_callback_closure`: the synchronous closure of a callback, class or
  foreign record exposed to opaque code, including values a member read of
  an escaped record or of a native slot the census does not prove intact may
  yield.
- `possible_setter`: an accessor owner's body that a property write after an
  unknown call, or an `Object.assign` onto an unproven target, may trigger.
  A setter declared for the written property stays `observed`.
- `possible_getter`: available accessor and callback bodies that a property
  read may invoke after skipped or opaque code changed the receiver's
  descriptors or prototype. The read returns an unknown value and keeps global
  reach, including when it looks up a method's callee. Its reason carries one
  witness: `lookup.lexical_owner` and the SWC byte positions in
  `lookup.span_start` / `lookup.span_end` identify the triggering lookup, while
  `promotion` is either `{"kind":"lazy_owner","source_owner":N}` for one
  promoted source owner or `{"kind":"raw_callbacks"}` when raw callback
  promotion has no single owner. This is a representative witness, not a list
  of every lookup or owner that could contribute the effect.
- `whole_body_promotion` (`operation`: `callable_inputs` or
  `array_iteration`): the lazy body of a callable whose inputs or iteration the
  interpreter could not model, beyond what its execution reached.
- `private_constructor_restore`: callback effects restored when a
  private-constructor result proof's execution failed.
- `unresolved_call_closure` (shared-`var` accesses only): the read closure of
  an at-init callee promotion could not resolve.

A setter or getter applied to the read receiver, a constructor body run by an
unsupported `new`, and a method called on a receiver holding an array stay
`observed`. An effect both observed and assumed is `observed`. Asserting a
pair with a `fallback` effect asserts that the assumed code does not run (or
does not reach the binding) during that statement's initialization. Ordering derived
between two observed effects is never routed only through an assumed one: the
binding timeline also orders each binding's observed effects among
themselves, so inerting an edge to a fallback effect never releases an
observed write/read order. An entry whose endpoints share no
`sequenced` edge, or only `strict_chain` edges between two summarized effects
on disjoint cells (which `dataflow_aware_s_chain` already leaves unordered), is
unused and fails validation. Both failures fail `spec validate` and `run` with
a diagnostic naming the entry, both source units and, for a contradiction,
each conflicting effect. This is explicit author input, not an inferred fact:
A1–A11 and every other contract still apply, and it grants no purity,
no-reentry or receiver guarantee.

That classification is decided once per edge when the graph is built and
retained as `dependency_graph.json.gz` `edges[].premise_verdict` on every
`sequenced` and `co_declaration` edge: `inertable`, `summarized` (strict-chain
adjacency of two summarized effects), or `known` with the `conflict` the gate
would report. Applying a premise reads the retained verdicts of the edges
between its endpoints; `debundle spec propose-commutations` reads the same
field to draft entries for the constraining `sequenced` edges inside a
rejection's blocking SCCs, so a drafted pair is accepted or rejected exactly
as the gate decides. For `pairs_cannot_break`, its report also names the
remaining nontrivial cyclic components as `residual_components[].modules`;
member paths and component rows are sorted by canonical module path, and
`residual_modules` equals the sum of their member counts. The text report
prints the same member paths, while `pairs_break` has no
`residual_components` field. These identities are diagnostic only and do not
change the gate decision. The `premise_verdict` field is absent on graphs
written before it was retained; the draft command reports such an edge as
`verdict_not_recorded` rather than classifying it itself.

## Ordinary own-property inspection

A guarded `Object.getOwnPropertyNames` or `Object.getOwnPropertyDescriptors`
call can inspect an exact ordinary object-literal shape through the object's
canonical identity, including a local binding or alias. The source must be
untainted, and its current own-key
set must still equal the indexed static shape. Statically known string and
number keys are supported, including literal-computed data keys; computed
method/accessor keys, spreads, decorators, `__proto__`, unknown/added keys,
escaped or unsupported mutations, and native replacement remain conservative.
Callee/argument effects and throws remain represented; inspection captures
data-value identities and getter, setter and method callbacks without invoking
accessors.

`Object.getOwnPropertyNames` returns a dense array of exact string primitives.
Canonical array-index names are numerically ordered first; other string names
retain literal creation order. The index never uses `BTreeMap` order as source
property order. Inspection does not read property values or invoke getters.
Argument evaluation and effects still occur eagerly, and the call's possible
throw remains represented. Its return/timing certificate does not establish
purity or exception freedom by itself. Ordered execution can independently
close the native body after proving this exact ordinary prefix, normal native
protocol execution, and the callee's inferred identity. Names read an
`object_shape` heap cell; descriptor capture also reads each captured data
`own_property` and allocates `descriptor_map_contents`. Applying a valid carrier
to an eligible ordinary target reads the carrier and target shape and writes the
target shape and named properties. Callee and operand execution keep separate
coverage obligations. The builtin rows retain unknown unconditional body effects.
An imported callable derives the same native transfers through its isolated
provider heap and the shared local interpreter. Its native-operation requirements
are checked across every chunk, including descriptor-application protocol slots.
Opaque symbolic parameters prove no ordinary receiver shape. Returned descriptor
carriers and applied accessor targets retain their stored values and callbacks;
allocation-only retention never supplies effect coverage on its own.
For a direct imported call with a literal record argument, the canonical call
index can demand a conditional provider-body summary for that record's complete
descriptor layout. The same local interpreter executes the provider with scoped
symbolic property values; input shape and property reads remain parameterized
until the caller checks its actual current descriptors after operand evaluation.
The instantiated summary supplies body effects and normal completion separately
from the existing returned descriptor graph. Only this independent closed body
proof removes the projection's conservative possible-provider-throw branch;
argument throws and failed return-graph installation remain conservative.
Provider module-cell effects, unknown invoked callables, unsupported input
layouts and native mutations decline this conditional summary.
Proxy objects, shadowed bindings, native
replacement, escaped sources and unsupported mutations remain conservative.

The descriptor-map carrier has no general readable shape. A guarded
`Object.defineProperties` consumes a live carrier and a settled, extensible
ordinary target. An uninterrupted first literal execution proves the initial
attributes of its existing data properties; each overwritten property must be
configurable. Native descriptor capture preserves known attributes and retains
unknown attributes as unknown. Integrity changes, escape and pooled reentry
revoke the literal witness. Captured data values and accessor callbacks are
weakly applied to the target's existing identity, so aliases observe the update
without acquiring a singleton or strong-update proof. Repeated capture with
conflicting source identity, values or attributes invalidates the carrier while
retaining its callback graph. Recapture through a stale literal Data shape after
an applied accessor remains conservative.

A successful native definition returns a descriptor postcondition attached to
that selected value. It is valid only for the same target and current heap
epoch; argument evaluation can invalidate it. Const-local aliases can retain
it, but ordinary callable boundaries drop it. Every consumed definition advances
the epoch, including definitions that change only attributes. The postcondition
never installs an exact shape for all objects at a pooled allocation origin.
Author-asserted wrapper projections do not create this native proof.

The shared native transfer also models two-argument `Object.assign` when the
source has a checked ordinary literal shape with enumerable own data properties.
Each existing target slot must be writable data; an absent slot requires the
extensible target and intact ordinary Object prototype. The absent `__proto__`
case remains unsupported. Source getters, target setters, proxies, unknown
attributes and failed strict writes retain fallback. The transfer reads source
shape and data cells and target shape, and writes target shape and named cells.
It composes with native definition postconditions through the same application
and provider interpreter, preserving callee-before-arguments ordering. The
canonical row retains unknown unconditional body effects.

Map member access/mutation, return, escape or opaque consumption invalidates
application but retains callbacks. A copied accessor read promotes its getter;
opaque target use retains getter, setter and method callbacks. Retaining a
native result's target identity without an eligible application does not
suppress possible throws or supply effect coverage. Readable maps, arbitrary
descriptor edits, general imported wrapper-body coverage, general purity and
exception-free behavior remain outside this contract.

## Inferred FinalizationRegistry callback retention

A direct `new FinalizationRegistry(cleanup)` or
`new globalThis.FinalizationRegistry(cleanup)` uses the `FinalizationRegistry`
row of the builtin table when `cleanup` evaluates to exactly one known ordinary
function origin or closure. The constructor retains that callback in its new
registry; it does not invoke it during construction. The successful result is
opaque and retains the callback and its captures. Later use or opaque
consumption promotes those callbacks through the ordinary retained-value
analysis.

The bare global and `globalThis` binding must be undeclared and unshadowed.
`globalThis` support is limited to direct static dot-member callees, including
existing modeled calls such as `new globalThis.Map()` and
`globalThis.Object.freeze(record)`. Local aliases, computed or optional
namespace access, spreads, unknown or mixed callback values, and noncallable
inputs remain on the conservative path. Dynamic scope, or any visible write to
the global object or the target intrinsic in any analyzed module, disables the
model. Callee and argument evaluation, throws, and at-init ordering remain
represented. The constructor remains impure and opaque for heap ordering: no
consumer reads the row's allocation as a heap-locality certificate, and this is
not a purity, throw-freedom, registration or method model.

The [FinalizationRegistry constructor](https://tc39.es/ecma262/2025/multipage/managing-memory.html#sec-finalization-registry-constructor)
checks callability, creates registry state and stores the callback through
[`HostMakeJobCallback`](https://tc39.es/ecma262/2025/multipage/executable-code-and-execution-contexts.html#sec-hostmakejobcallback).
The default non-browser host operation records the callback for later jobs.
The [HTML host operation](https://html.spec.whatwg.org/multipage/webappapis.html#hostmakejobcallback)
also records the callback and execution context without invoking it.

## Inferred returned-record construction timing

A parameterless export with a derived foreign call summary (§ Derived foreign
call summaries) whose result is always one fresh object literal of
noncomputed, distinctly named methods and getters is a returned-record factory.
An argument-free direct call cannot synchronously reenter importer
code. This is independent of purity and possible throws.
The returned record retains opaque deferred capabilities: later access,
invocation and opaque consumption conservatively preserve importer live-cell
reads and callback effects. The summary and provider member/capture provenance
appear in `modules.json` under `imported_callables.*.returned_record`.
Used certificates also appear in the dependency graph's
`imported_callable_summaries.*.returned_record`. A source unit's
`synchronous_execution.returned_record_uses` separately records `call_timing`
and `return_capability` consumption with the call-site location and a summary
reference. Callback-input factory evidence and uses retain their own fields.
The summary's `inference` records the composing source-identity rule, and the
empty-collection and native-integrity rules when the body constructs a
collection, with their captured-input dependencies. Actual use
also appears in `synchronous_execution.inference_uses`; the dependency set is
propagated from required premises rather than reconstructed by each consumer.

## Inferred namespace-wrapper timing

A stable directly exported hoisted function may receive a source-derived
namespace-wrapper summary when its complete body checks an ordinary input,
uses a private stable WeakMap cache, creates a namespace with the input's
prototype and optional default reference, and installs deferred own-name
forwarding getters. The shared proofs require the actual native bindings and
Object, WeakMap, Array and Function integrity. Shadowed receivers, visible
native mutations, extra eager operations and cache exposure retain fallback.

At a direct imported call, the caller must prove an exact untainted ordinary
object with its current own shape and supply a literal `true` second argument.
The summary narrows synchronous callback reentry after callee and argument
evaluation. The result retains the input and deferred callback graph while
remaining opaque: cached alias identity and possible throws remain covered.
Neither purity nor a fresh result is inferred. Early returned getter access,
eager argument effects and callbacks written through a cached alias retain
their ordinary ordering constraints.

Provider identity and rule evidence appear under
`modules.json.imported_callables.*.namespace_wrapper`. Actual use records both
the provider derivation and the caller's own-property derivation in
`synchronous_execution.inference_uses`.

## Derived foreign call summaries

Simple-parameter functions with one direct return built from inputs,
primitives, static ordinary record/array literals, strict equality (`===` or
`!==`), `void`, or terminal-arm conditionals use the same value and literal-heap
transfers as application execution. Strict equality evaluates both operands
without coercion and yields an unknown Boolean; it proves no runtime equality
or identity. `void` evaluates its operand before discarding its value. A
conditional retains its test and joins both possible arms, currently restricted
to direct parameter reads or primitive literals, without truthiness refinement.
Unsupported compound arms decline centrally without retrying another evaluator.
Strict arithmetic, relational and loose-equality expressions retain their
operator and evaluate their operands once, from left to right. The shared
primitive result and throw rules apply only after both completing operand
values are known to be primitive. Unproved object coercions retain conservative
callback effects; Symbol conversion, mixed numeric kinds and potentially
throwing BigInt operations cannot obtain a complete normal-effect receipt.
The canonical lowering retains complete
source coverage before value lowering can erase syntax. This narrow
`CoveredAllocationOnly` certificate excludes external access/write,
publication, invocation and throwing operations. Parameters are scoped opaque
values: forwarding/storing/returning one proves no input kind, property shape,
native identity or coercion behavior. Any unsupported input access or transfer
declines the summary. A failed migrated query does not retry a different
evaluator. Other supported bodies retain their existing strict source proof.

Conditional body summaries are separate from that allocation-only contract.
For a stable hoisted provider with one simple parameter without a default, the canonical imported
call census can request finite ordinary-record layouts observed at literal
arguments. The same ordered interpreter queries the body under each layout;
data values and accessor identities remain scoped symbolic paths. A present
data-field read and supported descriptor operations contribute parameterized
reads. They supply no caller allocation identity or private status for inputs.
The consumer checks the actual complete descriptor layout and literal attributes
after all operands execute, substitutes its real object/property cells, and
composes only a closed normal body receipt. Getter-invoking reads, missing fields, changed
attributes, taint, unsupported callbacks, exterior writes and other unproved
operations retain conservative paths. Provider module captures still require
their own qualified cell footprints. Native identity and protocol integrity
remain independent requirements. This body evidence can compose with an audited
descriptor-wrapper result capability; it does not replace that capability or
infer a call result from effects. Saved summaries retain the input conditions,
parameterized reads, source identity and dependencies, without query-scope IDs.

Covered straight-line bodies use the same ordered local-operation executor as
application execution. This includes local declarations and assignments,
sequences, returns, and checked writes to existing own data fields of fresh
ordinary records, including aliases and self-links. A static dot-field read
whose receiver is a simple binding uses that same fresh-record check and shared
heap at its actual operation position. Only an existing own data field on a
settled, fresh, unborrowed ordinary record supplies a covered read; copying its
value proves no property or callable behavior on that value. Application root
reads use the same present-slot operation when one simple declarator reads an
initialized module-local binding and no other call, write or opaque operation
can intervene before its initializer result is projected. A single original,
non-exported declarator can also use the shared ordered local-operation plan
when its whole initializer lowers into that grammar, combines at least two
calls or a call with a field-value operand, and has no direct
binding/global/property writes or dynamic scope.
A simple binary initializer can also use this plan when exported, if its
canonical source covers every child and its only indexed call operations are
implicit coercions. Each operand read still requires its actual initialized
local-cell receipt. The scalar operation contributes no effects only after
the shared primitive rules exclude coercion hooks and abrupt completion; the
whole-owner certificate still requires closed effects in every domain.
Every admitted call selects a synchronous local callable or a matched imported
source-body summary. Local member callees require a present ordinary data slot
and an unused implicit receiver; imported members require their explicit normal
selection premise. Native whole-owner proof families retain their established
dispatch until their value transfers accept evaluated operands. Evaluated return values pass directly to initializer publication;
the flat call pass and later value projection do not repeat those operations.
Calls within the initializer retain their effects and can mutate the prefix
after an earlier operand has selected its value. Unsupported runtime paths
summarize the remainder without retrying. Unproved collection construction,
split declarations, applied accessors, unsupported operators, numeric array
element reads and missing/inherited fields retain their existing conservative
paths.
Root present-slot reads remain opaque allocation judgments and confer no
whole-owner effect-coverage certificate. The operation records an `own_property` heap cell identified
by allocation origin and static property. This cell includes every runtime
generation of that source allocation; it proves neither a strong update nor
privacy. Opaque heap barriers and uncovered effect domains remain authoritative.

At a root initializer's current prefix, an exact static `length` read may
consume the own `length` data slot of one settled, first-generation dense
array literal. This read yields the static numeric kind and records the exact
`OwnProperty` cell, so ordering it against a later native `push` observes the
same length slot that `push` writes. The proof requires the array literal's
single settled dense-allocation witness and an existing own data value; pooled
or reused allocation sites, holes, unknown or multiple receivers, unsettled
descriptors, and accessors remain conservative. This is a root-initializer
read only: it does not admit callable/provider/retained queries or new array
mutation behavior. A canonical literal index key can read an in-range
element under the same witness, using the stored heap value and recording its
exact `OwnProperty(origin, index)` cell. Dynamic keys, holes, out-of-range
indices, mutations and pooled sites retain fallback; the slot proof grants no
write or prototype-read capability.
Length and checked index cells share a uniquely eager array's existing birth
proof against older opaque heap operations. An intervening opaque operation
or a repeated allocation site retains the conservative frontier.
A provider query starts
with scoped opaque parameters and an empty private heap. Parameters,
instantiated locals and checked private `var` captures can supply binding
values. Every reached value judgment composes, including discarded expressions;
input mutation, bindings outside that capture rule, unproved field reads/writes and
possible abrupt completion decline the summary. Argument retention is derived by traversing the returned graph,
including cycles. The query uses no additional source parse or AST evaluator.

Hoisted direct function exports and explicitly audited source members (§ Source
callable identity for namespace members) can carry the actual ordered body-effect
fragment for a synchronous allocation-only body with simple parameters and no
parameter defaults. This product is independent of its allocation judgment:
every reached operation must cover global properties, general heap, host effects
and lexical reads/rebinds, with normal completion. Static ordinary data-record
creation has its own operation receipt; child evaluation composes separately.
Parameter and local let/const creation, initialization and reads use actual frame
initialization. A mutable private parameter/let store supplies its own receipt
after the shared kernel checks initialization and immutability; captured stores
remain separate. Simple binding prefix/postfix increment and decrement use
the same actual read, primitive ToNumeric rules and checked store operation.
The application adapter also covers stores to source-declared module `var` and
initialized module `let` cells through this operation. It records the binding
write, preserves weak value joins and invalidates stale callable selections.
Imports, immutable bindings, lexical TDZ and unknown references supply no normal
store receipt. An ordered expression root performs its assignments once;
the later flat value projection does not replay their right-hand sides.
Complete shared invocations retain their transferred module values across call
merging. Incomplete, assumed or uncovered invocations still widen possible
rebinds. A legacy flat body cannot supply whole-body operation coverage from
the receipt of its last child call.
Their scalar abstraction retains numeric kinds for both the returned old/new
value and the stored value; emission preserves the original update syntax.
Object coercions, Symbol conversion, possible BigInt size-limit failure, TDZ
and immutable stores do not acquire normal effect coverage. Captured updates
record both accesses on the same pooled lexical cell and invalidate old callable
selection through the ordinary store. Provider module-cell initialization and
cross-module effect qualification remain independent obligations.
`void`, strict equality and logical negation compose their
evaluated children's effects before their own noncoercing operations. Checked
empty native collection construction carries the shared allocation and operation
effect products. Its summary retains constructor dependencies and the native
families whose global bindings the consuming invocation must read. Arrays,
other native construction, closures, var/hoisted local bindings
and unsupported operations retain unknown domains in this bounded producer.

Reads of direct module `var` declarations and simple `var` cells in an actual
lexical ancestor's callable scope may carry initialization evidence. Module
instantiation initializes module var cells; selecting an actual source closure
requires its enclosing environments to exist. The canonical lexical-parent chain
and declaration scopes establish the latter relationship. These proofs give no
concrete value kind, privacy, immutability or initialized RHS value. Imported,
lexical, parameter/default-scope and dynamic captures acquire no such proof.
Whole-body free reads and captured writes remain authoritative, including
expressions whose values are discarded.

A separate value-kind proof refines an eligible var capture only when it has
one simple declaration, no hoisted function value or subsequent writes, and an
initializer whose normal primitive kinds the shared primitive evaluator covers.
The canonical collector retains that candidate from the original initializer
syntax; an erased assignment in the value projection supplies no such proof.
Native return kinds require the actual unshadowed binding and every required
slot to remain intact under the completed census. Their dependencies guard both
the allocation summary and any body-effect fragment. The kind set includes
`undefined` because the var cell can be read before its initializer executes.
These kinds establish no initializer effects, completion, or execution order.
A proved primitive capture retains no callback capability; unknown captures keep
the ordinary opaque retention path. Returned fields remain opaque to importers.

Ordinary calls and imported class constructions consume this fragment only
after the same shared kernel has evaluated the selected callee and every
argument once in source order. The invocation kind remains explicit; a class
summary never licenses an ordinary call, and a function summary never licenses
construction. Construction retains its opaque instance/callback projection,
without granting own-field, prototype or later-method effects.

A class constructor's closed body does not establish its binding's GetValue.
An independent source proof covers that read only when the provider consists
of that one stable class declaration and local named exports, and the canonical
class definition has no static field/block, computed method key or other
unproved eager operation. Every emitted consumer then depends on a
non-reentrant leaf. Other class providers keep possible TDZ
completion, including cycles. A replaced provider's initialization proof
retains its vendor-swap equivalence condition. Saved summaries preserve this
initialization proof separately from the actual body fragment and its dependencies.

An invocation's receipt records a module/export/member/callable edge.
A direct function import is initialized at module instantiation, including in an
ESM cycle; a member's normal selection requires its explicit identity assertion.
Results retain the existing opaque capability/alias projection, and an abrupt
operand cannot seal the root. Consumed member identity and vendor substitution
are distinct `source_callable_members` and `vendor_swap_equivalences` proof
dependencies. Timing assertions and allocation-only summaries without a complete
fragment supply no body-effect receipt.

The exact owner's combined lexical and global/heap/host operation proof also
supersedes its legacy opaque-heap fallback in the relocation projection. Its
result contents remain opaque and every recorded cell access, independent
dataflow opacity and execution loss remains. An allocation judgment or completed
value without that complete operation proof cannot discharge the fallback.

The allocation-only capture projection outside that body-effect lane retains
its narrower rule:
A private capture must resolve to simple `var` declarations in one enclosing
callable scope with simple parameters, and must not redeclare a parameter.
Imports, module-scope bindings, lexical declarations, parameter/default scopes
and dynamic scope do not acquire this proof. Function-scope `var` initialization
proves that reading the cell has no TDZ or getter behavior; it proves no value
kind, immutability or heap locality. Unknown captures have scoped symbolic
identities distinct from arguments. An unknown capture forwarded directly as
the return value still declines the summary. An unknown capture stored in a returned fresh graph is
reported under `retained_provider_captures` and retains opaque foreign
capabilities. All actual arguments remain reachable in that case because the
capture can alias one of them. Later opaque use promotes possible importer
reads and rebinds and invalidates reached prefix heap properties; it does not
grant getter-free access or a result-identity proof.

Application field reads outside the fresh-record proof retain their existing
native, array and borrowed-data checks. Possible getter effects use the normal
conservative read transfer and truncate the remaining ordered body through the
existing callback fallback. Earlier operations are not replayed. Read results
from this application fallback remain opaque to the allocation-only judgment;
synchronous completeness alone grants no provider certificate.

Application execution also retains calls through simple binding references and
static dot members of simple binding receivers in the ordered body. It reads
the receiver and callee before evaluating arguments from left to right and
stops each abrupt path. The shared call transfer consumes
these evaluated operands with the canonical call's proof sites and policies.
It returns effects and values together for a singleton local source function
or closure, admitted native rows and supported foreign-summary projections.
Publishing an ordinary source function does not replace its call target.
Invocation still reads live captures and applies the existing cell havoc,
heap uncertainty and completeness checks. An unknown callee alternative prevents
singleton admission, including after a possible binding reassignment.
Invocations of tainted source functions bypass result memos so each invocation
consumes the current reachable state.
No separate value evaluation repeats the call. Objects
reachable by that callee, receiver or its arguments lose the caller's fresh
own-data admission. Completed operands and returned capabilities remain
reachable by a truncated remainder, including while later arguments execute.
An unknown target, incomplete invocation or missing result transfer uses the
existing conservative callback fallback at that call site without replaying
prior operations. A timing proof alone supplies no result transfer. Models
that still reevaluate source operands cannot serve an ordered caller; their
operands and results must first enter the shared transfer.
Member lookup keeps the shared field-read guard and the canonical callee's
existing native or namespace lookup certificates. An unproved getter truncates
before the arguments. Local method results also require the existing
unused-receiver check, including nested callable implicit captures; source
methods requiring `this` stay conservative. Invocation and fallback use the
captured receiver even if an argument rebinds its source cell. Computed or
dynamic receivers, optional calls, spread arguments and direct eval remain
outside this ordered grammar.

Eager binary expressions in these ordered callable bodies evaluate their left
and right operands once, in source order. When both results are known
primitives, the shared primitive transfer supplies the result kind and records
a no-effect operation fragment only when the operation cannot throw for those
kinds. A possible Symbol or BigInt throw leaves effect and completion coverage
unknown. If either operand is an object or unknown value, the existing
coercion fallback receives both already evaluated operands, so user coercion
runs after the right operand and is not replayed. Short-circuit operators,
`in`, `instanceof`, and strict equality remain outside this transfer. This
does not admit a binary root initializer or a provider summary containing
calls.

Guarded dense-array `push` consumes that captured receiver and evaluated
primitive arguments. An uninterrupted first dense literal allocation without
spreads supplies a separate descriptor witness: original prototype,
extensibility, writable own length and no own method replacement. The shared
transfer rechecks this witness
and the native prototype guards after argument evaluation, checks the resulting
length fits `u32`, and records length, shape, index and array-content effects.
It returns a normally completing Number and composes its operation receipt with
the caller. A checked push advances the length witness while invalidating exact
element contents. Reusing the allocation site, escape, skipped execution or
descriptor changes revoke the witness; no new author premise is introduced.
An undeclared, unshadowed native global can enter an ordered body only when its
global slot passes the existing native-integrity guard. Member identity and
invocation operands retain their separate checks; an unavailable local capture
or an unproved global stays conservative.
Invocation results remain opaque to the allocation-only judgment; a strict
provider query declines calls. This supplies no property-origin export identity
proof.

Ordered record literals evaluate static data-field values through the same
local-operation kernel, from left to right. Supported child calls, closures,
reads, assignments and nested records retain their effects and results without
another evaluation. Abrupt children stop the remaining fields. Operands already
evaluated remain capability roots if a later field truncates execution, and a
child that reenters the allocation site prevents fresh-record admission for
that joined site. Duplicate fields retain all operand effects and the existing
conservative heap joins. Computed keys, spreads, accessors and prototype-setting
fields remain outside this ordered grammar. A child invocation or closure still
does not grant an allocation-only provider certificate.

Ordered application bodies also create anonymous function and arrow closures.
An expression-bodied arrow lowers directly to the same retained return
operation as a block body; it uses the same parameter checks, source coverage,
read/write census, live-cell instantiation and effect/result transfer. This
does not add a second expression evaluator or broaden supported operators.
Each activation instantiates distinct cells for its parameters and declarations
referenced by nested callables. Cell references survive through intermediate
closures, including references used only after suspension. Reads use current
cell contents; declaration initialization and local stores update those contents
at their operation positions. Lexical cells start in TDZ. Ordered parameter
prologues create all parameter cells in TDZ and initialize them one by one;
compatibility prologues supply prebound parameters. `var` cells start
initialized. Captured writes to a `const` or definitely
uninitialized cell have abrupt completion. Other captured writes join possible
values because general invocation queries can represent calls that are skipped.
Those writes do not establish exact CommonJS cache state or ordered caller
control. New cells and changed contents invalidate invocation memos through the
prefix revision; structural capture interning alone proves no runtime identity.

Unknown or skipped execution widens reachable mutable cells while retaining
their known capabilities. A skipped continuation can initialize a lexical cell,
so widening preserves both TDZ and completing possibilities. Initialized `const`
bindings retain their value, but reachable objects still undergo ordinary
property invalidation. Callback and escape traversal follow current cell
contents. Activations outside ordered source coverage retain conservative value
summaries and cannot supply an exact mutable-cell proof. The canonical nested
identifier census identifies captured bindings without another source scan.
After statement execution unwinds and its invocation memos retire, collection
traces live prefix values, escaped and exported capabilities, runtime heap and
descriptor state, revocations, and active bindings. Unreachable cell payloads,
including cycles, can be reclaimed. Cell identities are never reused. Live
roots, heap tables and identity slots still grow with the analyzed chunk; this
establishes no memory bound independent of chunk size.
Capture descriptions are weakly interned. Live values and memo keys keep their
descriptions canonical; expired descriptions can be reclaimed, and their numeric
identities are never reused. Revoked audited-result descriptions remain strong
semantic roots, so recreating an equal description cannot restore its proof.
Owner-boundary cleanup retires a bounded batch of expired interner entries.

Anonymous function and arrow closure creation use a separate closure-capable
provider mode over the same canonical `LocalBody` and ordered local transfer.
It admits straight-line local initialization/assignment, sequences, returns,
unconditional literal values, and ordinary data records whose values may
contain those closures. The returned closure is a fresh allocation in
`foreign_call.returns`; when the whole result is one closure, the derived
summary also retains its exact full range from the canonical callable-span
index for consumers that need it. A record containing closures remains a fresh
allocation without a root-closure range. This does not infer the closure's
result, execute its body or widen callback-factory inference: a local-bound
returned closure does not acquire direct-wrapper evidence.

When a returned closure is retained, the provider summary conservatively keeps
every formal argument. It also follows the closure's canonical nested-callable
read projection into the final values of its captured local cells, recursively
traversing ordinary data records and further closures. Reached checked private
`var` captures are reported under `retained_provider_captures`; this preserves
capabilities held through a local heap object even when the closure does not
read the provider capture directly. Reads resolved in the provider module
environment, and unresolved global names, remain deferred with the closure:
creation does not read or copy those values from the factory activation, and
they do not become retained provider captures. An enclosing lexical cell still
needs a value in the final local environment or a checked private-`var` capture
projection. A returned graph that reaches an async or generator closure, an
implicit-`this` closure, or a closure with an unsupported enclosing capture
declines the summary. Closure bodies are deferred and are not executed by the
provider transfer. These allocation and retention facts do not establish
deferred callback behavior; callback-factory evidence keeps its independent
source checks. Catch parameters and destructured variable declarations inside
these deferred bodies are local bindings, not captures of the factory. Their
initializers and binding protocols still run only when the closure is invoked;
this local-binding fact grants no provider-body or class-closure coverage.

Calls, field reads/writes, `this`, throws and captured writes in the provider
body remain outside these transfer modes where their behavior is not proved.
The returned closure body is deferred and its operations are not executed by
this provider query. Methods, getters and setters are typed property definitions:
methods define data-valued functions, and accessors retain separate getter/setter
payloads. Async and generator method bodies remain deferred. Definition order,
paired accessors and overwritten values retain their source-order effects and
conservative capability containment. Ordinary own-data reads refuse accessor or
ambiguous descriptor shapes. A method's canonical home-object site records source
containment only; it grants no receiver, unique-instance or ownership proof.
Detached methods retain that object's capabilities under the existing heap
multiplicity and taint rules. Dynamic method receivers never become lexical
constructor captures. A sparse literal keeps holes absent and grants no dense
array proof.

Every function/arrow provider uses the shared canonical allocation/local-operation
transfers. The shared provider projects root closure ranges and the existing
parameterless, unique method/getter-only record metadata from canonical facts;
setters, data mixtures and duplicate names still suppress record metadata.
Unsupported syntax or a refused transfer stays unknown without another evaluator.
Only the executable prefix through a return runs; full canonical declaration
facts still govern hoisting, initialization and TDZ in that prefix.

Source coverage excludes async/generator functions, defaults/rest/destructuring,
interpolated template syntax, operators outside the supported primitive subset,
nested/computed/optional member reads, computed/spread/accessor/prototype fields,
member/optional/spread calls, direct `eval` and
unsupported control-flow statements. A source-covered throw still prevents the
allocation-only certificate. Every operand, test and arm must retain source coverage
and covered access evidence; an outer supported operation cannot hide an
unsupported or effectful child. Possible abrupt completion prevents the
certificate. Local-assignment pre-evaluation and a complete synchronous
execution outcome do not establish this certificate. Child transfer judgments
and proof dependencies compose; this premise-free consumer
declines any unexpected nonempty dependency set rather than discarding it.
Expression and ordered-body coverage remain bounded to these supported forms.

A directly imported, stable top-level function or class declaration of another
analyzed chunk receives a summary when every operation its call performs runs
no other code. The body may read only its parameters and its own `const`/`let`
bindings (`var` reads before initialization yield `undefined`; lexical TDZ
reads have no summary). It may create primitives, closures, object or array literals with
literal keys other than `__proto__`, and regular-expression literals. A regex
literal transfers only to a fresh `Kind::RegExp` allocation; its own property
descriptors, internal slots, prototype methods, `lastIndex` behavior and
coercion or matcher protocols remain unknown. This literal transfer does not
resolve the mutable global `RegExp` binding. `RegExp(...)` and `new RegExp(...)`
are outside this admission. It may construct an argument-free
`Map`/`Set`/`WeakMap`/`WeakSet` whose global slot is intact in every analyzed
chunk's census. The shared provider operation accepts only a direct
zero-argument `new` of that unshadowed global binding and retains the native-row
proof dependencies in the summary; argumentful, aliased, shadowed or replaced
constructors remain uncovered.

Each foreign summary retains its `native_operations`: invocation kind, intrinsic
family and optional static property. Before consuming a summary, every analyzed
chunk must leave every slot required by each canonical native row intact.
An intact global alone does not prove a static method's identity. These are
operation prerequisites, independent of return values and body-effect coverage;
reported semantic derivations come from the transfers actually consumed.

A class may also define instance fields and
write its declared private fields. Straight-line statements only. A class
qualifies only without
`extends`, decorators, parameter properties or an explicit `return`. Any other
operation, including a provider binding read outside the private `var` rule,
leaves the callee unknown.
A vendor-swapped provider is summarized from its bundled body (§ Vendor swap
equivalence).

The summary applies only to a spread-free ordinary call of a function or a
`new` of a class through the importing binding, when no narrower summary or
author contract names that binding. Such a call invokes no callback and records
no precision loss. Its result is the returned argument itself, or a fresh
opaque value retaining live references to the arguments it may hold, including
containers that acquire callbacks later. The carrier proves no object shape,
privacy or exact result identity; equal interned capture sets do not mean equal
runtime results. This carrier cannot certify an audited factory's ordinary
input graph. The analysis taints the objects among those arguments, and a construction may still throw
before the provider evaluates. Arguments it neither returns nor retains are not
exposed when no retained provider capture can alias them. The call gains no
purity or effect-completion certificate from this summary.
Methods of a returned object stay unknown. The summary appears in `modules.json`
under `imported_callables.*.foreign_call`; a use appears in
`synchronous_execution.inference_uses` as `foreign_call_summary`.

## Audited callback factories

Optional calls retain conservative callee, receiver and argument callback
effects even when their value and short-circuit behavior cannot be inferred.
Known local return graphs keep their callable captures and a possible undefined
result. Optional calls do not acquire assertions scoped to ordinary direct-call
syntax.

Without an author assertion, a direct named import may receive an inferred
callback-factory summary from its defining entry file. The export must have a
derived call summary (§ Derived foreign call summaries; a single-declarator
top-level function or arrow initializer that is never reassigned also
qualifies) whose body directly returns a fresh zero-argument function literal.
The admitted importer call supplies exactly one inline
callback; any other ordinary identifier parameters are omitted. The returned
function may capture only those parameters and its own locals; indexed calls,
writes and property reads must not introduce an additional callback capability.
The summary defers the supplied callback after a successful factory return,
while preserving eager import order, possible pre-initialization throws, and
the factory call's ordinary effects and purity classification. Other call
shapes and unproven source behavior use conservative timing.

`chunk_export_purity.<defining chunk>.callback_factory_exports` lists named
exports whose calls are audited to defer a single callback argument.
`callback_factory_members` lists static members on an imported namespace with
the same contract. At an importer, the analyzer applies either contract only
to a call with exactly one non-spread inline function or arrow callback, or a
retained result of another audited factory. The retained input is a fresh
ordinary callable/plain-object graph, and the outer factory must preserve the
original callback inside its own fresh returned graph. Construction and other
argument shapes remain conservative. The provider must not invoke that callback or any
caller-, host-, or export-provided callback synchronously, or publish the
callback outside the returned fresh ordinary callable/plain-object graph. The
consumer may later reach or invoke the captured callback through that result.
An invocation includes synchronous reads by the callback and by callbacks it
returns synchronously. Reads after an unconditional `await` remain deferred;
unsupported callback bodies retain conservative whole-body effects.
The provider must not synchronously read or write importer-owned state through
another route, including imported lexical cells or caller-owned objects; effects
confined to provider-private state are allowed.

The author must audit every provider and type branch, helper, callback-bearing
property access on both input and result, and prototype integrity; neither
graph may contain proxies or accessors at the call. This assertion describes
callback flow only. It does not
assert that the factory call is pure or effect-free. Writes through the result
that can install opaque values, accessors, or a new prototype conservatively
restore callback effects; known primitive metadata writes retain deferral.

Factory composition requires separate provenance from a factory whose existing
source proof or author assertion guarantees the fresh ordinary returned graph.
Opaque foreign, collection, native and source-derived constructor results do not acquire that
provenance from their retained callbacks. Unknown exposure, possible skipped
mutation and recognized result writes revoke composition eligibility. Member
projection and invocation retain callback reach but do not transfer eligibility
to their results. Interned capture keys may conservatively share revocation
across distinct results; they never establish runtime equality or freshness.

`chunk_export_purity.<defining chunk>.inline_callback_factory_exports` is a
narrower named-export contract. It applies only to calls with exactly one
non-spread syntactic inline function or arrow callback. The provider must
return a fresh ordinary callable containing that callback, without invoking
it or any caller-, host-, or export-provided callback synchronously or
publishing it elsewhere. Its other synchronous behavior must satisfy the
same importer-state restriction above. The author audits provider and helper
paths reachable for this admitted input shape; other overloads are unclaimed.
Calls with a named callback, a retained factory result, spread arguments, or
`new` use conservative callback timing. This contract is independent of purity
and does not grant the broader `callback_factory_exports` composition rule.

### Descriptor-copy returned wrappers

`descriptor_copy_factory_exports` is a separate author assertion for a direct
imported factory whose audited provider copies one input object's own property
descriptors into a fresh ordinary returned object and then assigns named data
methods. It does not widen `callback_factory_exports` or imply purity:

```yaml
chunk_export_purity:
  provider:
    descriptor_copy_factory_exports:
      wrap:
        - argument_count: 1
          object_argument: 0
          eager_own_data_keys: [create]
          assigned_data_keys: [create, call]
```

The sole signature must select one fresh, static-key ordinary object literal
passed to an exact direct imported call in a single-execution top-level
initializer. Every eagerly read key must be own writable data. Copied getters
are deferred until a later member read; accessors colliding with an assigned
key are rejected because assignment could invoke a setter or throw. Computed,
spread, duplicate, `__proto__`, proxy, optional, aliased, nested, or repeated
call shapes are outside this contract. Argument expressions still run normally.
The defining export must resolve to a stable, unrebound hoisted function in the
canonical callable facts; known reassignment and dynamic scope reject it.

The author audits the provider and its helpers: no synchronous invocation of
input/caller/host callbacks, no publication except through the returned graph,
no other importer-state access, and a fresh ordinary result that retains the
input graph. The provider's eagerly read keys and assigned keys must be listed
completely. The original `Object.getOwnPropertyDescriptors`,
`Object.defineProperties`, and `Object.assign` slots and the ordinary target
prototype/setter behavior must hold throughout admitted executions. The shared
source census rejects known writes to those exact slots or assigned prototype
keys; unresolved reflection is an explicit author lifetime obligation. Provider
defaults absent from the modeled copied/assigned keys remain opaque. A later
unknown read, call, mutation, or escape conservatively exposes retained getter
and method callbacks. The call keeps all ordinary effects and possible throws.
The consumed inference use reports provider, call-site shape, and own-property
projection rules; its typed proof dependencies identify the author contract.

### Retaining existing or multiple callbacks

`retained_callback_factory_exports` and `retained_callback_factory_members`
are **separate, stronger author assertions**. No provider name implies this
contract. Each entry lists exact audited signatures:

```yaml
chunk_export_purity:
  provider:
    retained_callback_factory_exports:
      factory:
        - { argument_count: 1, callback_arguments: [0] }
    retained_callback_factory_members:
      api:
        make:
          - { argument_count: 2, callback_arguments: [0, 1] }
```

Positions are zero-based. At least one position must be listed, every listed
position must be less than `argument_count`, and all arguments must be present.
Unlisted positions admit only the inert primitive syntax of `no_reentry_exports`.
Spread, construction, computed/chained/aliased callees, uncertain callback
values, and unlisted arities retain conservative timing. The older contracts
continue to admit their existing shapes independently when both are declared.

Listed positions admit inline functions/arrows, stable local named function
values, and retained audited-factory results. Every admitted callback contributes
to a single over-approximated returned callback graph; extracting any member or
invoking or publishing that graph can expose all those callbacks and their
lexical captures. The contract never proves callback purity.

The author must audit **both the provider and each admitted call site's input
graphs**. Unlike a fresh inline callback, an existing function has observable
identity, aliases, own properties and a prototype. All provider/type/helper paths
must refrain from synchronously invoking any caller/importer/host callback,
publishing or mutating any supplied graph, or accessing importer-owned lexical
state through any other route. The provider must return a fresh ordinary
callable/plain-object graph retaining the supplied callbacks, without introducing
additional provider callbacks, existing-instance aliases or cross-call state.
Provider-private effects remain allowed and normally ordered.

The callable property/prototype surfaces exposed through the result must contain
only inert metadata, standard intrinsic properties, and capabilities already
represented by the supplied callbacks and their lexical captures. Additional
callbacks attached as custom properties/prototypes, proxies and accessors are
outside the contract. A provider audit alone cannot establish this importer-side
obligation. Recognized mutation, rebinding and exposure keep the conservative
fallback; the checker does not prove the absence of all external aliases.

Namespace members retain the existing namespace identity, mutation and lookup
guards and require the same namespace capability audit as the fresh-only
contract. Result mutation and later callback invocation/publication retain the
ordinary synchronous-effect checks. No callback deferral assertion implies
purity or permission to reorder argument evaluation.

`debundle gate diagnose <binding>` provides a bounded source-backed trace for
one ownership target through constraining promoted callback dependencies. Its
supported rejection family is a matched imported namespace-member
callback-factory signature rejected by a top-level static write to that member
or a recognized alias, or by a computed-key write whose receiver is the
namespace binding or a recognized alias, or by the exact namespace binding
passed as a non-spread ordinary top-level call argument. The report names the
matched signature and exact source witness: write receiver/property or call
span/argument index, with a graph source-unit reference. An opaque-call label
requires a matching `UnknownCallTarget` loss at the same owner and span; the
guard alone does not prove a mutation occurred. A
supported row also requires the promoted edge's callee source unit to match a
statically resolved callback argument root from that exact signature.
Unresolved callback arguments and source-unit mismatches are inconclusive. The
report provides an explicit-path `inspect-source` command. Diagnose currently
does not read or fingerprint the source input, so it does not emit a parsed
statement ID or apply a possibly stale `SourceUnitOrdinal -> StatementId`
crosswalk. Five rows are returned by
default; `--all` requests exhaustive output, while query status summarizes all
uniquely linked candidate edges regardless of the output limit. This is a
local witness for this guard family, not a claim of a sole or globally earliest
cause. Computed-key writes through other receiver expressions, other guard
families, and absent promoted
dependencies are inconclusive. Multiple candidate barriers at one source unit
are also reported as an ambiguous link rather than cross-paired with callback
dependencies. Configured overloads are included only when their arity and
callback argument positions apply to the actual call.

### Retaining callbacks in constructed instances

`retained_callback_constructor_exports` is a separate author assertion for
direct `new` expressions through named or default imported bindings:

```yaml
chunk_export_purity:
  provider:
    retained_callback_constructor_exports:
      Box:
        - { argument_count: 3, callback_arguments: [0, 1] }
```

It uses the exact signature validation and callback/input-graph audit above.
Non-callback positions admit only inert primitive syntax. Spreads, namespace
member constructors, local constructor aliases, uncertain callback values and
unlisted arities keep conservative timing. This assertion does not apply to
ordinary calls; factory assertions do not apply to construction.

The author must audit construction, including superclass construction, field
initializers, default arguments and helpers: none may synchronously invoke,
inspect, coerce, mutate or publish the supplied callback graphs, or access
importer-owned lexical state through another capability. The imported
constructor's identity and construction behavior must remain stable during
synchronous initialization. The result must be a fresh instance retaining only
the supplied callback graphs, without preexisting-instance aliases or shared
callback storage across constructions. Its reachable methods, prototype and
property surfaces must introduce no unrepresented importer capabilities;
provider-private helper methods are allowed only under that same audit.
Proxies, accessors and externally mutable callback-bearing prototypes are
outside the contract. Intrinsic-integrity premises must also be established. An unexposed, unmutated
result of this exact constructor contract can compose into another audited
constructor. Its separate provenance does not certify an ordinary factory input,
because the instance contract permits provider-private prototype helpers.

Recognized callback rebinding, mutation and exposure retain conservative
timing. Extracting an instance member, invoking it or publishing the instance
can expose every retained callback and its captures, including graphs returned
by those callbacks. Argument evaluation and provider-private effects retain
their normal checks and ordering. The contract does not assert construction
purity: `pure_new_exports` or `purity: pure_new` requires its own audit.

### Calls without synchronous reentry

`chunk_export_purity.<defining chunk>.no_reentry_exports` asserts that direct
calls of listed named imports cannot synchronously invoke caller-, importer-,
or host-provided callbacks or read importer-owned lexical state. The author
must audit all provider paths and preserve the exported callable's identity
through synchronous initialization; a provider-side rebind or mutable wrapper
that can change its behavior is outside the assertion. Provider-private effects
remain possible, and the result stays opaque. Only a direct imported binding
call with no spread or `new` and the same checked primitive argument forms as
`no_reentry_members` below is admitted; zero arguments are allowed. Argument
expressions are still visited normally.
Aliases, uncertain arguments, and result use retain ordinary reentry checks.
The defining module still initializes in its usual order. This contract is
independent of `pure_exports` and callback-factory assertions.

### Callback registration without synchronous reentry

`chunk_export_purity.<defining chunk>.no_reentry_callback_exports` is a separate
author assertion for direct imported calls with exact callback-bearing signatures:

```yaml
chunk_export_purity:
  provider:
    no_reentry_callback_exports:
      register:
        - { argument_count: 2, callback_arguments: [0] }
```

Positions are zero-based and at least one callback position is required. Every
unlisted argument must use the checked inert primitive syntax described above.
Listed arguments must resolve to stable local ordinary callable identities;
inline functions/arrows and stable named aliases are supported. Rebound,
exposed, mutated, uncertain or non-callable values, retained result graphs,
callee aliases, namespace calls, spread and construction remain conservative.

The author audits every admitted provider/type/helper path and each callable's
own/prototype surface and aliases. During the call, the provider must not invoke
any supplied, caller-, importer- or host-provided callback, mutate supplied
graphs, inspect effectful properties/prototypes or access importer-owned lexical
state. The exported callable must retain its audited identity and behavior
through synchronous initialization. Provider-private effects remain allowed.

Unlike the returned-graph factory contracts, this assertion permits storing
callbacks in cross-call provider registries or returning opaque capabilities.
After argument evaluation and the audited call, every supplied callback and its
capture graph becomes globally reachable in the analysis, even if the actual
provider only returns it. Every later opaque call or protocol operation may
invoke those callbacks. Sibling/operand evaluation and later invocation,
publication or mutation retain their ordinary checks. The result stays opaque.
This contract does not imply purity or any factory/constructor retention policy.

An exact top-level anonymous call may carry the same timing premise directly
when the audited call site is local to the spec:

```yaml
anonymous_statements:
  - match: "register(callback);"
    no_reentry_call:
      callback_arguments: [0]
```

`callback_arguments` positions are zero-based, must be nonempty and unique, and
must be within the matched call's argument list. The selector must resolve to
one top-level expression statement: either a bare call or a call in the
rightmost operand of an `&&` chain, allowing parentheses. For example,
`typeof window !== 'undefined' && window.addEventListener('resize', callback);`
asserts timing only for `addEventListener`. Guard evaluation, including any
calls in the guard, keeps its ordinary effects and reentry obligations. A
conditional expression, sequence expression, or other operator surrounding
the terminal call does not activate the assertion.

Only a direct identifier callee or a static member callee on an identifier
receiver is admitted. Optional or computed members, effectful receivers,
spread arguments, and nested eager calls inside arguments remain on the
conservative path. Callback arguments must be ordinary stable local
functions or closures; other positions must use the admitted inert primitive
syntax above. Runtime values must still resolve to one stable callable
identity at the call site.

The author audits the exact callee lookup and invocation, including every
provider/helper/type path and the callable identity that can reach this site.
Neither lookup nor invocation may synchronously invoke supplied, caller-,
importer-, or host-provided callbacks, read caller-owned lexical state, mutate
supplied graphs, or inspect effectful properties/prototypes. Provider-private
effects remain allowed, and the call may store callbacks in cross-call
registries. Callee and receiver lookup, callback creation, argument evaluation,
reads, throws, and source/emitted ordering retain their ordinary analysis; the
assertion only suppresses synchronous callback reentry. After the call, every
admitted callback and its capture graph becomes globally reachable, and later
opaque calls may invoke it. This assertion establishes neither purity nor a
return shape; ordinary analysis remains in force and may independently infer
either. Unsupported source shapes or runtime callback values use the ordinary
conservative path. This assertion is independent of purity and other callback
factory/constructor contracts.

### Namespace and record calls without synchronous reentry

`chunk_export_purity.<defining chunk>.no_reentry_members` asserts a narrower
call-timing fact for listed static namespace members: with admitted primitive
arguments, the call does not synchronously invoke caller-, importer-, or
host-provided callbacks or read importer-owned lexical state. Provider-private
effects remain possible. It neither asserts purity nor describes the return
value; the return stays opaque. The same member must also satisfy
`namespace_independent_members` so its lookup cannot itself execute foreign
code, and the namespace identity and mutation checks must pass. The checked
call shape is a direct `namespace.member(...)` with no spread or `new` and only
primitive literals, `void` or `!` of a primitive literal, or a template with no
interpolations as arguments; zero arguments are allowed. Argument expressions
are still visited normally. Caller-owned objects, callbacks, aliases, optional
or computed lookups, and changed namespace members retain ordinary reentry.

`chunk_export_purity.<defining chunk>.no_reentry_record_members` is a separate
author assertion for direct static member calls with at least one fresh object
literal argument. Every other argument must be an admitted primitive value or
another fresh object literal. The objects may recursively contain those values
under static data keys; spread, accessors, methods, shorthand, computed keys,
and `__proto__` keys are excluded. This assertion requires an independent audit
of each admitted record shape and its provider branch, including any reads,
coercions, or retention of the object and its prototype. The provider must not
synchronously invoke caller/importer/host callbacks or access importer-owned
lexical state through the record. A primitive-only `no_reentry_members` assertion
does not imply this record assertion, and neither annotation is a general
whitelist for a named library. The same independent lookup, namespace identity,
mutation, and publication checks apply; the call result remains opaque.

`chunk_export_purity.<chunk>.namespace_independent_members` maps namespace
exports to individually audited static member names. Each lookup must be stable,
run no caller/importer/host code, and expose no capability to obtain the namespace
or replace an audited factory. An ordinary own data property or an explicitly
audited inert forwarding accessor can satisfy this; a primitive result alone
cannot. The guarantee covers the member value's reachable operations, properties,
prototypes, constructors, accessors, and returned callbacks, including implicit
namespace receivers in member calls and tags. Independently supplied argument or
receiver capabilities may propagate normally; the implicit namespace receiver is
not such an exception. Hidden closure capabilities to rewrite factories are
forbidden. This summary grants neither purity nor callback deferral.

The author must identify every imported export that can alias the namespace
family and retain the provider/runtime input audit below. The checker groups all
configured family roots together. Only when every runtime occurrence is the
receiver of a direct static lookup of a listed member may namespace admission
ignore unrelated unknown values and `this`. Whole namespace uses, writes,
updates, deletes, assignment/destructuring/loop targets, computed or optional
lookups, namespace exports, dynamic scope, or incomplete member lists retain the
ordinary conservative proof. Member results may be called, chained, constructed,
or used as superclasses with their ordinary execution effects. Callback deferral
still requires `callback_factory_members`; export/reentry and early callback
invocation checks remain active. See the [source-privacy proof](https://github.com/agentydragon/ducktape/blob/devel/devinfra/js/debundle/docs/design.md#namespace-capability-privacy).

Explicit member writes nested in object or array destructuring, including nested,
default, rest, and loop-head targets, retain the same conservative namespace
mutation checks as ordinary member assignments.

For a namespace-member contract, the author must also audit the provider and all
caller/runtime paths that supply values used by admitted callbacks. The listed
factory members must keep their audited identities and behavior throughout the
analyzed chunk's synchronous initialization. In particular, provider or other
namespace members, callback/ref/hook inputs, and values returned through those
paths must not expose an alias that can replace a listed factory member.
Direct static identifier-property namespace call results are treated as
independent only under this explicit author assertion. The analyzer propagates
explicit namespace provenance through assignments, local aliases, property
values, returns, and call arguments. An unrelated call or constructor result
with no such path is not treated as a namespace alias solely because its
runtime value is unknown; the author audit asserts that values used by admitted
callbacks cannot alias or replace listed members. Unknown syntax remains
conservative at publication and call-argument sinks. Binding cycles alone do
not establish namespace provenance; namespace and
unknown-value inputs to a cycle remain conservative. A static identifier-property
optional read directly from an identifier retains the receiver's namespace
provenance and the possible undefined result. Optional calls, computed optional
reads and chained receivers remain conservative; this value-flow rule does not
assert purity or remove accessor effects.
For call-argument checks,
the analyzer follows namespace values in the supplied argument expressions
and excludes the callee expression, which is not passed as an argument. A
value from an unknown call or constructor used as a static or dynamic write
receiver remains a possible namespace alias, including through local aliases
and member projections, and disables the contract. The checker does not prove
that arbitrary callback parameters or unrelated call results are disjoint
from the namespace; that non-aliasing guarantee is part of the author audit.
Under intrinsic integrity, adjacent writes to declared own data fields of a
checked fresh base-class instance do not invalidate a namespace contract.
The allocation and assignment values must run no user code, and the instance
must remain private throughout that run. Unsupported constructors, fields,
receivers, dynamic scope, and intervening statements retain the conservative
write checks.
For that exact top-level own-field write, the same proof excludes a setter
effect from unknown-call reachability. The assignment remains an actual
top-level effect; nested callable writes and later receiver mutations retain
their usual accessor checks.
For a fresh top-level object literal that visibly contains the namespace and
has static ordinary own data properties,
the checker distinguishes the container's identity from namespace values held
in its properties. A read of a verified own property follows that property's
retained value; storing a namespace in the container still counts as
publication at a sink. An adjacent write to an existing own data property may
use the same private fresh-value proof when its value runs no user code.
Getters, setters, computed keys, spreads, `__proto__`, unknown receivers, and
unsupported property reads retain conservative namespace checks. Literals
without an explicit namespace path keep the conservative receiver rule.
Class publication tracking also follows retained static fields through a
uniquely resolved local class superclass on the constructor surface. Unknown,
imported, reassigned, or non-class bases and cycles remain conservative;
instance inheritance, static accessors, decorators, and unsupported class
surfaces remain opaque.
Adjacent static property writes to the fresh ordinary result of a direct
audited factory call with one inline callback also retain the contract. Their
values must run no user code; prototype writes and intervening statements end
the proof. Rejecting the producing namespace's factory contract also removes
this proof from its results.

Under intrinsic integrity (A11), a sequence expression assigning a direct global
`Error(...)` result to a local binding can retain the namespace contract through
immediately following static property assignments to that binding. Values must
be inert or declared local reads; `__proto__`, `stack`, and intervening
expressions end the proof. Every matching write in that source unit and callable must
be proved. Calls, argument evaluation, and namespace publication through assigned
values retain their ordinary checks. Shadowed or visibly replaced `Error` and
dynamic scope remain conservative; see [design details](https://github.com/agentydragon/ducktape/blob/devel/devinfra/js/debundle/docs/design.md#conditions-on-the-input-chunk-assumptions-a1a11).

Within a literal traversed inside an admitted inline callback, a direct static
namespace member used as a call callee is analyzed as the callee expression,
not as a captured namespace value. Call arguments and effects remain analyzed;
computed and chained callees stay conservative.

Inside callback result object data properties, the analyzer also treats a
one-hop read such as `namespace.member` as a deferred member value. Only an
ordinary data property qualifies; the object-literal `__proto__` prototype
setter does not. That direct shape does not prove the member value's identity;
the author must audit its provider branches and any accessor, proxy, or
callback behavior reachable through it. Computed reads, chains, calls, spreads,
arrays, and other expression shapes remain conservative. Direct namespace
values and aliases remain rejected.

Without an assertion, source inference recognizes only immutable exported
function declarations with one simple identifier parameter. The body must
only return a zero-argument function or arrow that only calls that parameter
with no arguments. Reassigned bindings and dynamic scopes remain opaque.

### Source callable identity for namespace members

`chunk_export_purity.<defining chunk>.source_callable_members` maps namespace
exports to static members whose lookup during an importing chunk's synchronous
initialization is audited to resolve to one source callable. Every listed
member must also appear under the same export in
`namespace_independent_members`; that separate contract protects lookup from
foreign code and namespace mutation.

For each matching direct static lookup that execution reaches, the author
asserts that receiver evaluation reads an initialized namespace binding,
lookup completes normally, and the result is the unique source function
selected for that exported object member by canonical structural property-value
flow. The lookup must use an ordinary own data property or an inert forwarding
getter that reads only an initialized provider declarative binding. The getter
cannot read or write heap, global, or host state, invoke foreign code, or have
another observable effect.
If the provider member is vendor-swapped, the existing replacement-equivalence
contract remains a separate requirement for the callable used at runtime.

This asserts identity only, not body behavior. The body still runs through
shared `LocalTransfer` with the actual initialized-binding proof; the identity
assertion establishes no body effects, purity, no-throw behavior, retention,
or operand completion. Structural value flow gives possible origins only: it
does not prove that a member is present on every runtime path or that a
conditional or dead assignment executes. Authors must not assert identity when
an altered alias/getter or conditional assignment leaves lookup behavior
uncertain, or when source flow has multiple or unknown callable origins.

### Deferred browser-global publication

`chunk_analysis_options.<chunk>.deferred_global_publications` names audited
browser-global data slots, for example `{globalThis: [inspectState]}` or
`{window: [inspectState]}`. A listed slot must be an ordinary inert own data
property, or an absent slot whose creation cannot invoke an inherited setter,
at the write. The object must permit the write. No host, importer, or source
consumer may use its stored function or class during synchronous
initialization of this chunk.
The analyzer admits only a whole top-level direct assignment of a known local
function/class, or the same `window.slot = value` assignment behind a checked
`typeof window` guard. It rejects ambiguous aliases, visible global-root
replacement, dynamic global access, additional source mentions of the slot
(including through another builtin
global alias), and visible global-object escapes. The certificate delays only
the callback-availability consequence of that exact write. RHS evaluation,
eager reads, property mutation, ordinary effects, and source-order constraints
remain. Independent exports or other publications of the same value retain
their own reentry behavior. A spec author must audit the host's property
descriptor and all consumers; source checks alone cannot establish either
host fact.
An earlier anonymous, zero-argument synchronous function IIFE with a discarded
result may use a local global-object alias before the certified write if that
alias and every derived value stay inside the invocation. Ordinary calls with
the alias, nested
callbacks, returns, dynamic direct member access, and certified-slot access
through the alias retain the conservative fallback.
Primitive-valued operator results cannot carry the global-object identity.
The synthetic protocol call for a literal-key `in` check on a direct builtin
global root does not publish that root as a value; its possible host or proxy
effects remain in synchronous effect analysis unless the independent audited
membership contract below applies. Dynamic keys, aliases, ordinary
calls, and visible root replacement retain the conservative publication guard.

## Audited builtin-global membership probes

`chunk_analysis_options.<chunk>.non_reentrant_global_membership` maps builtin
roots (`globalThis` or `window`) to literal string keys. For example:

```yaml
non_reentrant_global_membership:
  globalThis: [featureEnabled]
```

This separately certifies the host's `[[HasProperty]]` protocol at every admitted
probe. The author must audit that the exact global object and its complete
prototype/alias surface remain intact throughout synchronous initialization,
including code in other chunks and host setup. The probe must neither invoke
or publish caller/importer/host callbacks nor access importer-owned lexical
cells through getters, proxies, modified prototypes or other hidden routes.
A browser-global name alone establishes none of these facts. Clean native
host behavior is a strong per-probe author premise, not inferred builtin trust.

Admission requires a listed literal string on the left of `in` and a direct,
unshadowed listed root on the right (parentheses are transparent). Keys with
unpaired surrogate code units cannot match the UTF-8 assertion and keep fallback.
Parameters, block-local bindings, imported roots, aliases, other roots, effectful receivers
and nonliteral keys keep their conservative protocol behavior. Recognized
root replacement, prototype writes, mutated Object intrinsics, dynamic global
member access, dynamic scope or global-root publication/escape also disable
the certificate. An earlier discarded synchronous function IIFE may use the
existing mechanically checked confined alias rule; its derived values cannot
escape or touch the protected keys or prototype/identity surface. Unsupported
or hidden alias/prototype mutations remain part of the author's audit burden.

A computed write to another object reached through a global property, such as
`globalThis.cache[key]`, does not alone identify a root/prototype mutation.
Explicit `__proto__`, `prototype` and `constructor` writes still reject, as do
computed writes on recognized global identity or constructor/prototype paths
(including `window.parent`, `document.defaultView` and `Window.prototype`).
Stored direct/recognized identity back-aliases (including nested record values)
remain rejecting. Storing a recognized prototype/constructor value on a
global-rooted receiver also rejects; merely holding that value in an unrelated
private object does not. The other root-exposure guards remain rejecting.
The author must establish that an otherwise admitted descendant cannot alias
the protected root or prototype surface; no host-property name proves this.
Indirect aliases through untracked variables, calls or host fields still need
the complete author audit; the path classifier does not prove their absence.
The descendant write retains its normal execution effects and callback checks.

Only protocol callback reentry is suppressed. Both operands and their existing
reads/effects are still evaluated and indexed in source order. Purity,
possible errors/throws, global-property dependencies and unrelated call effects
retain their existing treatment. The emitted JavaScript still executes the
original operator. Unlisted probes remain conservative even in the same chunk.
`deferred_global_publications` certifies a different timing boundary and never
implies this membership contract, or vice versa.

## Local-effect decorator annotations

`effect: typescript_decorate_helper` asserts target-local mutation; it does not
assert callback timing. `effect: typescript_metadata_decorate_helper` additionally
certifies that every admitted application, including every helper branch,
intrinsic alias and supplied decorator, only writes inert target metadata.
Decorators run synchronously, but neither they nor the helper may invoke target
methods, getters, setters or constructors; publish callbacks outside the target
graph; reenter unrelated caller/host/export callbacks; or access caller cells
through hidden routes. Provider-private constant/metadata reads are allowed;
observable writes outside the target graph are not. The author audits ordinary
intact targets, prototypes, descriptors and intrinsics. Existing descriptor,
prototype and callable behavior must remain unchanged. Imported decorators need
this explicit audit; purity or import shape does not imply their behavior.

The stronger summary applies only to a whole expression statement shaped as
`helper([bare identifiers], LocalClass.prototype, literalStringKey, 1|2)`, with
a nonempty spread-free array and a key other than `"__proto__"`. Analysis must
prove exact stable local function/class identities and initialized bindings.
Helpers with non-top-level captured state, dynamic scopes and unsupported forms
keep conservative timing. Eager argument reads,
the helper's and local decorators' syntactic lazy read/rebind dependency closure,
and target-local mutation/co-location edges remain. This is not purity.

`effect: typescript_metadata_decorate_helper_with_member_decorators` adds one
static member lookup (`provider.decorator`) to the admitted decorator array.
The provider root must be an imported binding without any direct member write
or rebind in the analyzed chunk. The author additionally certifies that each
admitted provider lookup is an ordinary, stable own data property throughout
synchronous initialization: no getter, proxy, alias mutation, or hidden
callback reentry can replace or observe it. The selected member must itself
be a metadata-only decorator under the contract above. This assertion covers
all supported applications of the helper, not just a single call. Computed
keys, chained members, spreads, and local providers remain conservative.
The existing bare-identifier helper effect does not acquire member support.

`effect: typescript_metadata_decorate_helper_three_args` explicitly asserts
the same metadata-only behavior for
`helper([bare identifiers], LocalClass.prototype, literalStringKey)`.
The helper determines the descriptor without a fourth kind argument. All
helper branches and supplied decorators must satisfy the contract above;
reading and restoring an ordinary own descriptor must preserve its behavior.
For a subclass target, inherited metadata must also be ordinary inert data:
copying it may neither invoke accessors nor mutate the base class's metadata.
Stable initialized identities, dependency closures and target co-location
checks still apply. Extra arguments and unsupported decorator/key expressions
remain conservative. Earlier effects retain their existing accepted shapes.

## Synchronous execution diagnostics

`synchronous_execution.inference_uses` records construction certificates
actually consumed by timing analysis. Each entry has a typed `conclusion`, the
`derivation_rules` involved in its proof, and the exact `value_input_refs`.
Guarded native call summaries also report `native_call_identity`, whose
derivation includes `native_integrity` for the global object and operation
family. The completed census also tracks `Function` so consumers that invoke
through `Function.prototype.call` can require its integrity explicitly.
Stable callable and returned-record conclusions use their corresponding rule
names when consumed.
The local-class chain includes `native_integrity`, `native_call_identity`,
`empty_collection_construction` and `local_class_construction` when native
fields are present; a source-only class has no unrelated native premises.
Primitive Set iteration composes `native_integrity` with
`primitive_set_iteration`. These diagnostics do not replace the purity or heap
dependency fields. Internal context and scheduling: <docs/inference.md>.

Dependency-graph source units carry `synchronous_execution` when synchronous
analysis ran. `complete` is the analyzer's existing effect-completeness verdict,
independent of `purity`. `precision_losses` retains the reasons observed when
execution became conservative: unresolved call targets, unsupported operations,
unsupported local-class constructions, recursive invocations, and depth,
expansion, or callback-pool limits. Limit records include the configured bound;
a callback-pool record also gives the pool size. An `unsupported_construction`
record names the first construction check that failed as a typed `rejection`
(for example `field_may_exist_on_object_prototype` with the field, whose census
entries are in `native_prototype_property_checks`); later checks may fail too.
`missing_call_result` identifies an ordered call whose synchronous-effect proof
completed without supplying a return-value transfer. The body still truncates
conservatively at that call. This does not report an unknown target or certify
the rest of the body; a result transfer using the already evaluated operands is
required to continue. A call rejected by the ordered operand path retains its
`unsupported_operation` / `opaque_execution` reason and may carry the typed
`ordered_call_admission_refusal`: `unknown_target`, `multiple_targets`,
`non_local_target`, or `unsupported_receiver`. It names
the first ordered admission check that refused the already evaluated operands.
Unknown-target classification remains a separate precision loss where the
existing target rules produce it. Incomplete invocation and missing result
transfer do not carry this admission reason.
An `unsupported_operation` / `member_read` reason may also carry the optional
`ordered_field_read_refusal` field from the shared ordered own-data guard:
`receiver_not_singleton`, `receiver_not_object`, `borrowed_receiver`,
`unsupported_record_shape`, `allocation_not_fresh`, `unsettled_receiver`,
`missing_field`, `non_data_field`, or `missing_heap_value`. It identifies the
first refusal condition at that operation's original source span. For
`allocation_not_fresh`, the analysis does not distinguish a reused allocation
site from another reason freshness was not established. This explanation does
not change effect promotion.
The ordered call-admission explanation does not change target admission,
fallback effects, reachability, gate acceptance, or emitted JavaScript.

An annotated local constructor-factory call kept on the conservative path records
its first typed refusal in `synchronous_execution.local_record_factory_rejections`.
The identity checks distinguish a non-binding callee, a missing hoisted
function, an invalid result initializer, an exported or escaped factory, and
whole-chunk versus current-prefix identity mismatches. For `factory_escaped`,
`first_callback_publication_owner` may name the first source owner observed
inserting that function into the escaped-callback set through `publish_values`.
It is omitted when that insertion was not recorded, and is diagnostic context,
not proof authority. These reasons and the optional owner do not change the
conservative effects or emitted JavaScript.

When a checked returned-class construction is refused because its members
escaped, `first_class_members_publication` may identify the first actual
insertion of those `ClassMembers` into the escaped-callback set. Its typed
`writer` distinguishes the publication operation; optional `top_level_owner`
and `span_start`/`span_end` locate the operation in the analyzed chunk, and
`source_location` names the containing source unit when available. Missing
context remains unknown. For `bounded_invocation_fallback`, the optional
`bounded_fallback_input_path` records one deterministic retained-reference path
from an existing fallback seed: an abstract argument-vector position, captured
binding, escaped callback pool, named export binding, or default/runtime export
callback roots. An argument position is not proof of a runtime caller argument:
ordinary fallback uses its abstract invocation inputs, while callback-pool
fallback uses the original opaque-operation inputs rather than the synthetic
flattened callback-set argument. Opaque-operation inputs can include the method
receiver appended after explicit arguments.
Duplicate abstract values keep the first seed in argument, captured-binding,
escaped-pool, then export order. Typed path edges reuse the callback walk's
stored-reference carriers; array elements are reported only for canonical
array-index property keys when the abstract reference has an array allocation
kind. Object-backed array-literal storage remains a stored-property edge
because its array shape is not represented on the retained reference. The path
omits runtime allocation identities and indices into abstract value sets. If a
path cannot be rooted, it is reported as
`unknown` with a reason. This is one valid abstract path, not a unique cause of
publication. The optional `bounded_fallback_callsite` separately locates the
opaque invocation that entered the fallback when its call span is known; its
offsets locate that operation expression, while `source_location` identifies
the active top-level owner context and may differ from a nested callable's
lexical location. It does not replace the writer span or claim that the opaque
caller itself performed the publication. Both fields are diagnostic only and
do not affect the construction proof or conservative effects.

Each record's `reach` classifies the code the conservative path stands for.
`{"kind": "bounded"}`: known source code, namely a rejected local construction
or an invocation truncated at a recursion, depth or expansion bound, whose
inputs and synchronous read closure reach nothing the interpreter cannot name.
Otherwise `{"kind": "global", "cause": …}` names the rule that applied, checked
for dynamic scope first, then for each input, then over the read closure:

- `dynamic_scope`: the chunk has direct `eval` or `with`.
- `borrowed_receiver`: the constructed class is used outside `new`, exported
  under export reentry, mutated, or escaped to opaque code; a class in the
  read closure is mutated or escaped; or the truncated callable itself
  escaped.
- `tainted_input`: an input, through stored properties, captures and
  accessors, is an unknown value, a global object alias, a tainted or unsettled
  ordinary object, or
  a callable already escaped to opaque code.
- `foreign_record`: an input or the callee is a foreign deferred record.
- `unknown_callee`: the read closure of the code and its inputs' callables
  contains a call whose callee value flow does not resolve to local callables
  or a builtin row, a member or protocol operation on an import, global or
  otherwise unknown binding, or an opaque body. An `unknown_call_target` is
  `unknown_callee`, except a foreign record's call (`foreign_record`).
- `unmodelled_operation`: every `unsupported_operation`.
- `escaped_callback_pool`: a bound hit while running the callbacks opaque code
  may invoke, whose pool is the escape set accumulated over the prefix.

A site reached more than once keeps one record, `global` if any reach was.
The classification is a report: both reaches promote the same effects.

Ordinary-object allocations, including dense arrays, reached by skipped local
execution lose their settled property values and descriptor/prototype
guarantees. A later read, including a method lookup,
returns an unknown value and retains possible getter effects with a global
`unsupported_operation` / `member_read` loss. An earlier incomplete statement's
ordering does not certify that later read. Descriptor, native-array and ordinary
object proofs also reject those unsettled objects. Intrinsic-integrity premises
do not exempt ordinary objects from these checks. The existing closed-enum
certificate retains its stronger whole-lifetime `PlainData` guarantee; later
mutation or publication disqualifies it (<docs/purity_soundness.md> § Settled
heap taint).

Property-specific Object prototype premises appear in
`synchronous_execution.native_prototype_property_checks`: each entry names the
property, the absence verdict, and the number of blocking source operations.
`program_graph.json.gz` shares those operations once per run under
`native_prototype_mutations`, with
chunk, source unit, source location, kind and possible property names. A null
property set means any name; an empty set means Symbol keys only. These are
conservative possible writers, not proof that a mutation executes. `describe`
selects blockers for checked names and applies its ordinary section limits.
Native-call identity checks and their witnesses remain separate.

`dependency_graph.json.gz` includes `imported_callable_summaries` for directly
called bare imported callback-factory summaries used by synchronous analysis.
Each shared entry names
the importer's local binding and preserves all `callback_factory_evidence` from
the defining export, including asserted versus inferred origin. A source unit's
`synchronous_execution.callback_factory_uses` references the shared entry and
names the semantic use: `call_timing` suppresses opaque callback promotion for
an eligible call, `return_shape` retains its callback in a deferred result, and
`closed_result_root` omits a private initializer result from reachability roots.
Each use gives the lexical source-unit ordinal and call-site `source_location`;
the containing graph node is the top-level source unit whose initialization
analysis consumed it. Nested and repeated direct calls retain their lexical site and
deduplicate equal uses within that top-level analysis. Older graphs can omit
these fields. A recorded use means this analyzer applied the named capability;
it does not prove that the annotation was minimal or load-bearing for the gate,
nor enumerate other author-contract dependencies. Calls through local aliases,
joined values, and namespace-member contracts are outside this reporting scope.
An absent use is not a global unused-contract verdict.

Each loss identifies its lexical `statement_ordinal`. Its `source_location`
identifies the exact indexed operation when available; otherwise it identifies
the truncated callable or source unit. Nested execution carries its reasons to
the active top-level source unit; repeated and cached outcomes deduplicate equal
reasons at the same source site. A record describes an analysis fallback, not
proof that the operation executes on every runtime path. Historical graphs may
omit `synchronous_execution`. The loss list is diagnostic evidence, not an
exhaustive proof trace; `complete` remains authoritative even when the list is
empty.

## Realizability cut explanations

When the full intra-chunk import graph is acyclic, entry imports prefer each
module's earliest source unit. Dependency traversal still evaluates imported
modules first. Cyclic graphs retain their rescue order except for entry-only
leaves: modules imported only by entry with no imports of their own. These leaves
can be interleaved by source unit without changing other modules' relative
order. The candidate's exact emitted order still requires the ordinary
relocation assessment.

Each `cycles.json` entry names its `rejection`. `mutual_constraining_cycle`
is a Pass-1 cycle of constraining edges. `esm_evaluation_tdz` is a Pass-2 TDZ
proved under the materializer's linker order. Pass 2 runs only when Pass 1
finds no cycle: a cyclic constraining subgraph has no linker order, hence no
import order to check. A rejection with Pass-1 cycles instead adds
`tdz_after_collapsing_pass_one` entries: Pass 2 on the same assignment with
each Pass-1 SCC collapsed into one module, listing the original modules. They
show what would still block once the Pass-1 SCCs are co-located; they are
conditional and do not change the verdict. The bail summary opens with the
Pass-1 root cause and labels each cycle's pass; `gate list` and
`gate describe` show the same label.

Every blocking SCC in `cycles.json` carries a `cut`: the constraining edges
whose removal (co-locating each binding pair) makes the SCC realizable. Each
cut edge names both module endpoints (`from`, `to`), both actual source-unit
endpoints (`from_source_unit_id`, `to_source_unit_id`, matching graph node IDs
and the dependency edge's source and target), the binding labels
(`from_binding`, `binding`), and its `kind`. `statement_ordinal` is edge-reason
provenance, not an endpoint identity. For a `sequenced` edge, the source side's
impure initializer appears in `sequenced_source_unit`. The edge's
`explanation` states why the gate treats it as constraining, keyed by
`explanation.kind`:

- `at_init_read` (an `eager_use` edge): `position.kind` is `direct` when the
  statement reads `binding` in its own eager position, `promoted_at_init` when
  the read was lifted from the first-order body of `callee_source_unit`
  (`callee_binding` when that unit declares one), a chunk function the
  statement calls at init, or `unresolved_at_init_call` when the read is in the
  conservative read closure of an at-init call the promotion cannot resolve.
- `sequenced`: `cause.rule` is the emission rule that produced the edge, with
  the endpoint classification it compared; `target_source_unit` is the target
  side's impure initializer in the same shape as `sequenced_source_unit`,
  omitted when the target is classified pure (an array allocation or a pure
  content reader). Rules: `strict_chain` (adjacent impure statements under the
  strict S-chain), `shared_var_access` (`binding`, `source`, `target`:
  consecutive at-init accesses of a binding declared at several `var` sites),
  `binding_timeline` (`binding`, `source`, `target`, `order` ∈
  `writer_after_writer`, `reader_after_writer`, `writer_after_reader`: the
  at-init writer/observer timeline), `uninitialized_observation` (`binding`,
  `observation`: a synchronous callee observed the binding before its
  declaration), and `cell_order` (`from`, `to`,
  `conflicts`). A `cell_order` endpoint is `summarized`, `opaque_barrier`
  (not dataflow-summarizable; `causes` ⊆ {`member_write_or_alias`,
  `dynamic_cell_writes`, `global_object_alias_escape`,
  `opaque_at_init_call`}), `heap_allocation`, or `heap_frontier` with its
  `causes` ⊆ {`array_content_effects`, `non_summarizable_impure`,
  `incomplete_synchronous_execution`, `callee_heap_effects`}. Each conflict is `read_after_write`,
  `write_after_write`, `write_after_read`, `use_after_allocation` or
  `frontier_after_cell` with the `cell` (`binding`, `global_prop`,
  `array_contents`, `set_contents`, `lexical_cell`, `object_shape`, `descriptor_map_contents`,
  or `own_property` with `origin` and `property`), or `after_barrier` / `barrier_after_prior`
  for the opaque-barrier dependence. `source`, `target` and `observation` are
  the provenance of that endpoint's binding effect (§ Commuting statements):
  `{"kind": "observed"}` or `{"kind": "fallback", "reason": {"path": …}}`
  naming the conservative path; the `explanation` text names each assumed
  side. For `possible_getter`, that reason also carries its lookup site and one
  lazy-owner or raw-callback promotion witness. A fallback effect retains one
  deterministic representative reason; it does not enumerate every path that
  could contribute the same binding effect.

Sequenced endpoint context (`sequenced_source_unit`, `target_source_unit`) is
emitted for every impure initializer and for a pure initializer with available
non-empty `heap_frontier` evidence. It carries `synchronous_execution_complete`
when synchronous analysis ran; `false` marks a statement whose sequencing
edges are the conservative flood of an incomplete execution rather than a known
ordering fact. A pure endpoint without available non-empty heap evidence has no
context for that side but keeps its `explanation`. Missing endpoint context or
`heap_frontier` can mean synchronous evidence was unavailable; it does not
prove zero heap effects.

When non-empty, `heap_frontier` on a source-unit synchronous report and its
sequenced endpoint context separates `own_at_init` from
`composed_synchronous_calls`, and includes `non_assumed_heap_accesses`. Those
heap accesses were recorded outside an active `AssumedEffectReason` path at
the recording or merge point; other premises, including program and
native-input premises, may still have been consumed. Each cell set reports its
unique count and a stable sample of at most eight exact cells. The existing
opaque bit has no per-operation provenance; exact escaped cells remain
independent evidence alongside it. This diagnostic does not prove heap
locality and changes no ordering or gate verdict.

The explanation is retained when the dependency graph is built
(`dependency_graph.json.gz` `edges[].sequenced_cause` for `sequenced` edges; the
edge `role` for at-init reads), never recomputed at report time. Where several
rules would emit the same source-unit pair, the first emitted cause is kept.
`explanation` is absent for the other edge kinds, whose `kind` and `binding`
already state the constraint, and for `sequenced` edges of graphs written
before the cause was retained. The bail summary and `gate describe`/`gate cut`
text views render the same data under each binding-pair row, and list the
SCC's `inert_commuting_edges` (§ Commuting statements) with their premise and
cause after the cut. The explanation changes no gate verdict.

## Vendor split-brain evidence

A partial vendor swap rejected for shared retained implementation reports both
its swapped-package ownership path and its residual liveness path. Ownership
traces distinguish dependencies of stripped exports from dependent items absorbed
into the package island. These explain the stripper's conservative classification;
they do not assert that every reported dependency executes synchronously.

## Native collection callback effects

Direct native calls and constructions the interpreter can summarize without
running user code are the rows of `facts/abstract_domain/builtins.rs`
(§ Synchronous analysis limits). Array iteration methods are that file's
`ITERATION_ROWS`; `push` has no row yet.

An unexported `const` facade built by the exact nested
`Object.freeze(Object.defineProperty({__proto__: null, ...ownData},
Symbol.toStringTag, {value: primitive}))` shape retains its stored values
without invoking them. The `Object` and `Symbol` intrinsics must be intact;
keys and descriptor fields must be static own data, with no spread, accessor,
computed key, or intervening call. The analyzer keeps the facade's object
identity and property values, so a later call or publication still exposes its
stored callbacks. The proof changes only construction-time callback reachability;
ordinary eager dependencies, effects, and ordering remain.

Bounded synchronous analysis can execute a known callback for a direct
`forEach`, `map`, `filter`, `flatMap`, `some`, `every`, `find`, `findIndex` or
`reduce` call over a retained untainted dense ordinary array: one callback, no
`thisArg`, no spread; `reduce` also takes an initial value. The callback runs
once per element, in index order, with the retained element, a primitive index
and the source array (`reduce` passes the accumulator first). Its reads,
writes, coercions and publication are observed effects of the statement.

- **Early exit.** `some`, `every`, `find` and `findIndex` can stop at any
  element. The analysis still runs the callback for every element: effects
  only join, so the extra invocations add possible effects and remove none.
- **Results.** `map`, `filter` and `flatMap` return a fresh array that retains
  the element and callback-result aliases. A `flatMap` result whose positions
  are unknown keeps its values as contents and is not iterated precisely
  later. `find` returns an element or `undefined`; `reduce` returns the last
  accumulator, and on an empty array without an initial value it throws.
  Iteration and spread of a result do not invoke stored functions.
- **Constructed class inputs.** A dense input slot may carry a checked local
  base-class construction while its class, receiver and array protocol remain
  untainted and unpublished. This proves the receiver's class kind, not a
  distinct allocation. A value projected from an arbitrary instance member
  carries possible callbacks without inheriting that kind proof. Member-read
  effects and source and element validity are checked before each callback.
- **Opaque mapped values.** When a completed `map` callback returns an instance
  capability whose exact result position cannot be represented, the fresh
  result retains returned and source callback capabilities as unknown contents.
  It loses exact slots and primitive-array receipts before another callback
  runs; an opaque consumer can still publish those capabilities. `Top`,
  unavailable and other unrepresentable returns take conservative effects.
  Completion and result values alone do not establish whole-owner coverage.
- **The array as an argument.** The source array reaches the callback as its
  last argument under its own allocation, so a write through that parameter
  is a write to the source.
- **Mutation.** Before each next element the analysis re-reads the source.
  Unless the source is the same untainted dense array with the same element
  values, and no element was tainted, the call falls back to conservative
  whole effects.
- **Holes, `thisArg`, own method slots.** A sparse or non-literal source is not
  dense, a `thisArg` is not admitted, and an own `forEach`-style property on
  the array taints it. Each is the conservative path.
- **Integrity.** Each row's `requires` slots must be intact: the method's own
  property of `%Array.prototype%` and, for a fresh array result, its
  `constructor` and `%Array[@@species]%` (§ Native integrity inputs). An
  intrinsic change, an unsupported operation in the callback, or exhausted
  invocation limits also keep conservative effects.

See [the proof and fallback conditions](https://github.com/agentydragon/ducktape/blob/devel/devinfra/js/debundle/docs/purity_soundness.md#retained-dense-array-iteration-effects).

A direct static `binding.push(...)` with non-spread primitive arguments may
also use a call-timing no-reentry proof when the binding resolves at
the current prefix to one untainted dense ordinary array. The existing Array,
Object and global native-integrity guards must hold. Callee/argument effects,
throws and heap ordering remain represented; the call remains impure and its
result remains unknown. This is not an array-like-object or general method
model. Own-method changes, prototype changes, unknown publication/escape,
spreads and uncertain arguments retain conservative callback effects.

After a proved push, the array's precise surface is invalidated and cached
invocations observe a new prefix revision. Its existing contained callbacks
remain reachable by later reads or opaque consumers. Subsequent array
operations use the conservative path rather than stale pre-push slots.
The independent descriptor/length witness can admit subsequent primitive pushes
through the shared ordered transfer described above; it cannot restore element
snapshots or certify other array methods.
The ordered root also admits standalone top-level calls through this checked
transfer. Whole-owner coverage still requires method selection, argument
evaluation and the post-argument descriptor/prototype checks to compose their
actual operation receipts.

## Runtime resource destinations

The browser harness `reports/runtime.json` lists statically recognized dynamic
imports, worker constructors and `new URL(..., import.meta.url)` uses from the
original shallow source analysis. Destinations distinguish emitted JavaScript,
captured non-JavaScript assets, planned package replacements, external URLs and
unresolved resources. An absent capture is distinct from a captured JavaScript
file that has no emitted destination. Nonliteral uses remain explicit.

This inventory is separate from semantic gate acceptance. It does not certify
network availability, execute conditional loaders, enumerate CSS dependencies,
or prove that a planned package replacement redirects every dynamic consumer.
External HTML module entries and preloads are preserved. They do not require a
local capture merely to construct a harness.

Lowered dynamic-import and import-meta URL literals consume destinations resolved from the same source
facts. Captured JavaScript targets point at their emitted entries; other local
resource paths are rebased from their original source file. Rebasing an
unverified path does not claim that a corresponding file exists. The inventory
currently reports URL query/fragment and encoded path spellings conservatively rather than
certifying their captured identity.

Bare Worker-string URLs use the document base in browsers. Their closure remains
unresolved (`document_relative_worker`); the existing worker rewrite is outside
this source-file URL guarantee. Worker constructors using `new URL` have a
separate URL record, without certifying worker execution.
