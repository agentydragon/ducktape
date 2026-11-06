# Wire format conventions (per-chunk JSON reports)

The per-chunk JSON files debundle writes under `reports/tree/<chunk_id>/`
are consumed by:

- spec authors with `jq` poking at cycle / dependency-graph / atomic-group reports;
- CLI tooling (top-level `debundle modules propose` / `atoms` /
  `coverage` / `describe` / `inspect-source --id` / `scc` / `cluster` /
  `graph-summary` / `gate diagnose`);
- humans reading the files during debugging.

This doc states the convention these files follow.

## Graph report selection

Default runs write `graph_summary.json` per chunk: exact source-unit, edge,
edge-kind, atomic-group/conflict and cycle counts, gate/legacy-TV verdicts, and
whether full graphs were exported. It has no node/edge table. Full dependency
and program graphs require `run --graphs` or Bazel's `dependency_graphs` output
group. Their schema and provenance checks remain authoritative for graph-backed
queries; a summary is not a substitute graph. This selection is independent of
full diagnostic evidence and effect-conflict JSONL.

For an ownership-subset run, `graph_summary.json` and `modules.json` include
`ownership_subset` as an array of `{ "module": <chunk-local path>,
"bindings": [<readable member names>] }` objects. A missing `bindings` field
means all claims in that module were retained; an empty array means only its
anonymous-statement claims were retained. An empty `ownership_subset` array
means no explicit module was selected. This records the requested ownership
selection, while final module contents show any bindings co-located by the
ordinary placement rules.

The staged per-chunk `SynchronousCalls` fact now carries
`non_assumed_heap_accesses` as typed `reads` and `writes` sets of heap-content
cells. It is analysis input to the commuting-premise check, not a new public
report field. `dependency_graph.json.gz` retains the existing
`premise_verdict.conflict` shape with `kind: shared_cell` when the verdict is
`known`; the cell
may now come from a directly invoked callee. Saved analysis from an earlier
engine must be recomputed before this field can be read.

## Native input identity in saved analysis

The staged analysis projection stores native operation premises separately from
`chunk_analysis_inputs`. This is the source-analysis view of the authored
`chunk_analysis_options` map; graph-only settings remain in graph construction
and do not enter the saved analysis identity. A resolved-member selector becomes a typed
`declaration` with the original `binding` and `body_index`, keyed by provider
chunk. Saved native input evidence uses the same source-only declaration;
rendered reports restore the current `resolved_member` selector. Structural
selectors remain `source_match`. Fresh analysis is required after this compiled
schema change. The authored YAML field and its values are unchanged.

Foreign callable products carry `native_operations` entries with `invocation`,
`intrinsic` and an optional `property`. Each identifies the canonical row whose
complete native slots must remain intact across all analyzed chunks. This
replaces constructor-family-only metadata; it supplies no body-effect coverage.
Engine-mismatched saved analysis must be regenerated, rather than reinterpreting
old constructor metadata as proof of static native operations.

## Convention: `Atom`-only on the wire

Every JSON file carries binding identities as `Atom`s (interned
strings), **not** as SWC `Id = (Atom, SyntaxContext)`. The
`SyntaxContext` half is dropped at serialization.

| File                          | Field carrying binding identity                                                                                                                                                                                                              |
| ----------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `dependency_graph.json.gz`    | `nodes[].declared_bindings[].binding: Atom`, `edges[].binding: Option<Atom>`, `edges[].sequenced_cause` binding/cell names                                                                                                                   |
| `cycles.json`                 | `cut[].binding: Option<Atom>`, `cut[].from_binding: Option<Atom>`, `cut[].explanation` binding/cell names (evidence is recomputed on demand by `debundle gate describe`); `rejection` names the pass (SPEC § Realizability cut explanations) |
| `atomic_group_conflicts.json` | `claims[].binding_names: Vec<Atom>`, `witness.paths[].steps[].edge.binding: Option<Atom>`                                                                                                                                                    |

`Atom` serializes as a plain JSON string via `swc_atoms`'s own
`Serialize` impl. That impl writes the **string content** — interned
in memory, but the wire form is the string itself, which is portable.

## Program graph

Full graph files use `.json.gz`: deterministic gzip level-1 streams containing
the complete existing compact JSON payload. Writers finish the compression
footer and flush the file before reporting success; no plaintext full graph
is staged or emitted. Small JSON reports and JSONL are unchanged. Saved-graph
readers require gzip streams and validate the complete stream, including
checksums and trailing bytes. Graph consumers use the gzip-only reader; the
generic JSON report reader accepts plain JSON for non-graph reports. For shell inspection, use
`gzip -dc dependency_graph.json.gz | jq ...`.

Sections that are the same for every chunk are written once per run, as
`program_graph.json.gz` at the report root (`reports/tree/` beside the chunk
directories): `provenance`, `native_integrity_witnesses` and
`native_prototype_mutations`. Each `dependency_graph.json.gz` names it in
`program_graph`, a path relative to the graph's own directory
(`../program_graph.json.gz` for chunk `main`). A tool that copies a graph copies
the program graph at the same relative location.

## Saved-graph input provenance

`program_graph.json.gz.provenance` records the run's file inputs by named
root and SHA-256. Its `present_directories` entries record package subpaths
whose directory presence affected analysis; they assert only that the path is a
directory, not that its contents were read. `debundle snapshot create` recreates
these as empty directories, while file bytes remain individually captured in
`files`. Readers default a missing `present_directories` field to an empty
list for older provenance records.

## Atomic graph reports

`dependency_graph.json.gz.atomic_graph.nodes[]` contains atomic groups. Each node
represents one SCC of the constraining-edge subgraph over source-unit nodes; its
`source_unit_ids` and `members` identify the source units in that group.
Each source unit can move between modules, while an atomic group can contain one or
more source units that the spec must keep in the same module. `atomic_graph.edges[]`
connects group IDs and retains the direction of constraining edges between
groups.

The top-level `debundle atoms --format json` report uses `atomic_groups` for
these nodes. With `--by-destination`, it also includes `destination_groups`,
whose entries contain a destination path and the `atomic_group_ids` assigned
there. `describe` reports selected groups under `atomic_groups` as well.
`graph-summary` uses `atomic_group_count`, `residual_atomic_group_count`, and
`largest_residual_atomic_groups`. `coverage` rows use
`atomic_group_ids`, `complete_atomic_group_ids`, and `split_atomic_group_ids`,
with statuses `complete_atomic_groups` or `split_atomic_groups` when the
requested source units cover complete groups or split them.

Conflicts during `run` are written to `atomic_group_conflicts.json`. The
analysis and CLI report fields name the groups explicitly, so each row can be
joined directly to `dependency_graph.json.gz.atomic_graph.nodes[].id`.
Each row also has `witness.claim_pair` and two `witness.paths`. The paths use
the same `G_atomic` projection as SCC construction, embed full dependency-edge
rows, and label each row's projection (`directed_init_order` or
`symmetric_colocation`) and orientation (`forward` or `reverse`). A reverse
orientation is only emitted for a symmetric co-location constraint. Paths keep
at most twelve leading steps and report their complete `total_step_count` and
whether those steps were truncated.

### Export-reentry diagnostics

`export_reentry.json` is a bounded per-selected-chunk diagnostic, never analyzer
input. `host_behavior` and `program_premises` echo the typed author inputs;
`policy` is `conservative`, `no_static_importer_reentry`, or
`{"reentry_through": {"total": N, "examples": [names], "truncated": bool}}`.
The last form is a presentation sample, not the interpreter's actual name set.

Every sampled collection has `{total, examples, truncated}`: `global_blockers`,
`closure_blockers`, `host_builtin_leaves`, and, for `loader_closure`, loader
`requests`, `loaded_chunks`, and optional `reentry_through`. Examples borrow the
first twenty entries in the complete derivation's deterministic order. `total`
is the exact collection size, not the number of underlying sites. Read totals,
not example lengths. Empty sampled collections remain explicit.
`host_builtin_request_count` is the exact sum of all builtin-leaf
`request_count` values. Loader `request_site_count` similarly sums all resolved
request counts, including rows beyond the examples.

`loader.rule` is `no_synchronous_loader`, `no_host_loader_sites`, or
`loader_closure`. The last form retains exact `closed`, consumed `host_keys`
and `premise_keys`, `suspended_sites`, `premised_sites`, `outside_window_sites`,
and `unreachable_sites`. `failures` already aggregates the complete occurrence
count and one deterministic first witness per finite failure kind, so remains
complete. Witnesses identify either a host behavior key or a source chunk and
typed site. No result is inferred from a sample's emptiness.

Request examples retain source chunk, specifier, typed target and
`request_count`. Builtin-leaf examples retain the static request's source,
specifier, builtin ID and count. Global/closure blocker examples retain their
existing typed `reason` and source/target evidence. Chunk references remain
canonical IDs. All global and closure predicates still run, including closure
predicates when a global blocker has already forced conservative policy.

`example_limit` is twenty. `full_detail_export_available` indicates a capability;
`full_detail_export_command` names `debundle run --full-report export-reentry`.
This explicit request streams compact `export_reentry.full.json` containing the
complete original `ExportReentryReport` schema: full policy name sets, full
loader requests/loaded chunks, full blocker/builtin arrays, and the original
omission rules for empty fields. It is not emitted by default. Serialization
and explicit flush must succeed before the bounded summary is written; failures
fail the run and can leave a partial full file. No summary field asserts that
an unrequested file exists. `--full-report-chunk` selects sinks only. Full graph
export and full diagnostics are independent.

The in-memory semantic report and staged analysis payload retain the complete
original report. The output-only borrowed view cannot be deserialized as an
interpreter policy or staged proof. Bounded diagnostics never change acceptance,
export-reentry classification, atomic/cycle checks, rejection markers, or stderr.

### Translation validation

`effect_translation_validation.json` contains a typed effect relocation
assessment beside the legacy graph and edge-TV comparison reports. The same
assessment runs even when report output is disabled. Its
`emitted_order` is the exact statement-unit order produced from the candidate
assignment and emitter import order. `decision` describes statement-order
commutation only: `accepted/no_reordering` is vacuous; a reordered `accepted`
proves that every inverted pair commutes under closed effect domains, or that
one endpoint is a statement proved to have no evaluation effects. An open
effect set is treated as Havoc against every nonempty partner, so crossing a
proved-empty endpoint needs no assumption about the open endpoint. `conflict`
means an uncovered modeled may-conflict or a contradiction with an existing
premise; `incomplete` means an uncovered Havoc pair. Exact authored premise coverage
can accept with `assumed_statement_order_commutation` and explicit typed
`premise_dependencies`; observed contradictions still block. A may-conflict does not prove a particular runtime trace
executes both accesses. The assessment does not prove cross-module rebinding,
vendor replacement, privacy or singleton behavior. `legacy_gate_accepts` and
`legacy_tv_accepts` retain diagnostic comparison verdicts, without a second veto.

`candidate_relocation_assessment.json` pairs the same emitted statement order
and single effect sweep with typed structural placement obligations from the
same assignment validation. `placement.cross_destination_rebinds` carries
typed dependency-edge and source-unit identities; `atomic_group_obligations`
retains each group's members, claims and causes. Pure `eager_use`/`sequenced`
conflicts are classified as semantic-order SCCs, recognized write/co-location
causes as structural constraints, and mixed or empty causes as explicit
blockers. `placement.projection_violations` contains typed source-unit, binding,
declarator, stable-interval and whole-statement identities for completed checks
that found an inconsistency. `placement.coverage.unresolved_checks` is the
canonical typed set naming source checks whose complete input domain was not
available: `destructuring_declarator_grouping`, `source_unit_to_body_mapping`,
`binding_and_shared_var_destination_projection`, and
`whole_statement_destination_projection`. Coverage is complete only when that
set is empty; completeness is derived and is not serialized separately. The
same non-deserializable source/destination projection produces this evidence
and the binding/statement maps consumed by the lowerer. An unmappable candidate
whose finalized assignment reaches the projection still emits its diagnostic
assessment, but it does not produce emitter maps. Conflicting destructuring and
nested-`var` ownership requests are retained for this projection, which checks
the hygienic binding catalogue against the retained source groups. They produce
typed projection violations in the candidate report and the same projection
refusal in normal emission. Request planning still uses the shared destructuring
owner classifier for fixed-point pulls when exactly one owner is explicit;
split claims remain intact for this final check.
The top-level candidate `status` distinguishes `blocked_by_effect`,
`blocked_by_placement`, and `blocked_by_effect_and_placement`; `incomplete`
means the effect decision or one of the required placement domains remains
unproved. `accepted_within_checked_scope` means the effect component accepted
and all four placement checks completed without a known structural blocker,
with `placement.emission_mappable: true`. This joint verdict authorizes
relocation lowering; vendor replacement, final export and reconstruction checks
still apply. `legacy_gate_ordering_veto` identifies semantic-order SCCs rejected
by the diagnostic graph comparison. An ordering-only comparison veto is neither
a structural placement blocker nor a second emission veto.
Its flattened `decision`, `emitted_order` and `swapped_pairs` are projections
of the same sweep reported in `effect_translation_validation.json`; the
flattened `decision.status` describes the effect component only.

`counts` includes every conflicting swapped pair, cell incidence, Havoc pair
and premise disposition. `legacy_edge_comparison` separately counts
`legacy_edge_only_pairs`, `effect_only_pairs` and `both_pairs`; it excludes the
legacy report's forward-edge and placement diagnostics.

`coverage.unknown_coverage` counts missing global/property/host domains
independently of legacy dataflow opacity, execution incompleteness and heap
opacity. Unknown domains remain global Havoc; `full_semantic_precision` is
false. Cell conflicts can include `lexical_cell`, `object_shape` or `descriptor_map_contents`
with `origin`, `own_property` with `origin` and `property`, and `private_slot`
with `origin`, `class` and `name`. For a private slot, `origin` identifies the
receiver allocation source, `class` identifies the private-brand source, and
`name` preserves the private name. These source identities can represent
multiple runtime generations and do not prove a one-shot allocation, strong
update, or private ownership. An `own_property` origin likewise includes all
runtime generations of its source allocation and supplies no strong-update or
privacy proof.
For `lexical_cell`, `origin` is the canonical binding declaration offset and
includes every activation of that binding; distinct declarations retain distinct
origins across saved analysis.
`coverage.covered_owner_dependencies` retains the typed conditions for
all owner coverage certificates in the analyzed facts. `relocation_dependencies`
retains the typed conditions for closed owners participating in candidate
inversions; `premise_dependencies` names authored commuting premises used for
conflicting inverted pairs. Neither dependency field turns a report into a
trusted input artifact.
`coverage.owner_coverage_refusals` counts typed reasons why an original
owner's whole-initialization certificate was not sealed. Bounded examples
include an optional refusal and existing source location for each endpoint;
JSONL carries the same refusal inside each endpoint's opacity record. Reasons
identify only the refused proof boundary, never source bindings or expressions.
They explain the report and do not change the certificate or coverage.

`dependency_graph.json.gz.nodes[].synchronous_execution.binding_effect_authority`
is a diagnostic projection of the graph's decision to skip the syntactic
at-init call fallback. Its report DTO is internally tagged by `authority` and
has `unclassified`, `legacy_execution` (with the existing `complete` value),
`author_source_only`, `ordered_root_initializer_uncovered`,
`ordered_root_expression_uncovered`, and `operation_receipt` forms. The receipt
reports the exact root owner and a typed `root_kind`: `binding_initializer`
retains its binding and initializer span; `expression_statement` retains its
expression span. Both have separate read/rebind prerequisites and flat local call
edges with caller/target bodies and merged conditions. The analyzer verifies
that those edges connect to the root body. This DTO is output-only; it cannot
be loaded as engine authority.
Call targets distinguish `native` invocation (intrinsic and optional property)
from `native_construction` (intrinsic). Checked empty collection construction
can contribute the latter edge in application execution. An imported body
retains its matched `imported_callable` edge and transports the provider's
constructor dependencies; its internal operations are not expanded into
application call edges. Both paths separately retain the constructor's global
binding read.
Staged analysis artifacts instead carry the engine-owned, externally tagged
saved `BindingEffectCoverage` fact. Its root binding uses the same saved
symbol and context ordinal as other staged facts. That field is required:
artifacts missing it fail to decode, and the normal analysis-engine and source
provenance checks still apply. `legacy_execution` is a temporary decision for
owners outside ordered-root execution, not evidence of covered operations.

`examples` retains at most twenty deterministic pairs, preferring rows with
modeled cells and filling unused slots with the earliest Havoc-only rows. The
selected pairs retain sweep order and each contains at most twenty conflicting
cells with their access provenance. Each sampled pair retains its exact
`cell_count` and `cells_truncated` marker;
`examples_truncated` makes omitted pairs explicit. Counts remain exact when
examples are truncated. `full_detail_export_available` is true: request
`debundle run --effect-conflicts-jsonl` for complete per-chunk rows in
`effect_conflicts.jsonl`. The default does not write this file.
Repeatable `--effect-conflicts-chunk <chunk-id>` selects only JSONL sinks;
it leaves every graph, bounded summary, source input and census scope intact.

Each JSONL line is one pair in the complete sweep order; summary examples are
selected from this stream without changing that order. A line includes
`runs_first`, `runs_later`, every `cells` entry
with access provenance, exact `cell_count`, `havoc`, typed `disposition`,
`premise_locations`, typed `premise_references`, and the report-only
`legacy_edge_conflict` comparison.
`first_opacity` and
`later_opacity` retain each endpoint's unknown coverage, dataflow opacity,
execution completeness/losses, heap opacity and an optional typed
`owner_coverage_refusal` with its existing source location when available.
Bounded examples expose the same optional information as
`first_owner_coverage_refusal` and `later_owner_coverage_refusal`. Cells serialize one at a time;
the export does not retain a full-row or global pair vector. Empty streams
are valid when the summary counts zero pairs. A write failure can leave a
partial file; compare row/cell counts to the sibling summary, which is written
only after the stream flushes. Gate-rejected chunks can have complete exports
even though the command exits nonzero.
These reports are not substitute semantic inputs for staged facts.

Global-cell access evidence can additionally contain `global_may_accesses`:
each entry has a `site` (lexical source-unit ordinal, chunk-relative SWC span
endpoints, read/write kind and static property) and `evidence` (typed proof
dependencies and conservative callback assumptions). The enclosing chunk
supplies the file scope. Physical operation identities join across repeated
value evaluation and invocation memo replay. These entries always describe
may-accesses. A cell with no independent observed evidence has
`kind: indexed_may_access` and cannot create an observed premise contradiction.
Existing observed/fallback cell evidence can retain these additional may-sites.

`global_may_access_count` is exact. Default samples retain at most
`global_access_example_limit` (twenty) entries per access and set
`global_may_accesses_truncated` when more were omitted. JSONL retains every
entry and serializes borrowed evidence without cloning a full provenance list
for each pair. Empty provenance/count and false truncation fields are omitted.
Static callee-global recording closes no coverage domain; dynamic keys,
ordinary heap, host effects, throws and reordered execution remain covered by
the existing conservative gaps.

`translation_validation.json`, beside `dependency_graph.json.gz`, is the bounded
diagnostic edge-TV verdict. The joint candidate assessment is the relocation
authority. `gate_accepts`, `accepts` and `swapped_pairs`
are exact. `counts` contains exact `conflicts`, `forward_edges_kept`,
`forward_edges_swapped`, `placement_splits` totals and `conflicts_by_kind`.
Each of those four evidence fields has `{ "total": N, "examples": [...],
"truncated": true|false }`; at most twenty examples are retained in the same
deterministic order as the complete checker. Conflict examples contain
`runs_first`/`runs_later` (unit ordinal and module), `dependency_edge` and
`kind`; other examples are dependency-edge keys. Read `total` or `counts`,
not the sample length, when comparing results.

`example_limit` is twenty. `full_detail_export_available` is a capability and
`full_detail_export_command` names `debundle run --full-report translation-validation`.
An explicit request writes `translation_validation.full.jsonl`; nothing writes
that stream by default. Each row's `category` is `conflict`, `forward_edge_kept`,
`forward_edge_swapped` or `placement_split`. Conflict rows flatten the complete
conflict example fields; other rows contain `dependency_edge`. Category row
counts equal the summary's exact totals, including when examples are truncated.
The stream follows the existing edge-classification loop then the existing
emitted-order conflict sweep, without a second evaluator. Full legacy pair
comparison and all verdicts consume complete results, never sampled fields.

Repeatable `--full-report-chunk` filters sinks only and rejects unknown chunk
IDs. Requests require report output; an accepted dry run refuses suppressed
output. Write/flush errors fail the run and can leave a partial stream. The
sibling final summary is written only after stream completion/flush; no field
asserts that an unrequested stream exists. Full diagnostic streams and complete
graph output are independent, including when requested through Bazel attrs.

### Why the convention works for these files

These files all carry **post-filter** data — data that has already
passed through the `binding_owner` lookup in `graph/build.rs`. That
lookup is keyed by the full `Id`. Anything whose `SyntaxContext` does
not match a chunk-top-level binding (closure-local reads with inner-
scope marks, globals with `SyntaxContext::empty()`, etc.) **misses the
lookup and never becomes an edge**.

Consequence: the only `Id`s that survive into the dependency graph have
`ctxt = top_level_mark.apply_to(empty)`, i.e. they're the chunk-top-
level bindings the resolver assigned that single shared context to. By
the time we serialize them, the `ctxt` is _redundant_ — it's the same
for every binding in the file — so dropping it loses no information.

A consumer reconstructing an `Id` from `Atom` does:

```rust
top_level_id(name, fresh_top_level_mark)
```

i.e. pairs the name with whatever `top_level_mark` the consumer's own
resolver assigned to the chunk. This works because both sides agree
that _every name in this file is a chunk-top-level binding_ — the
ctxt is determined by that role, not by the wire data.

## Why pre-filter facts (`StatementFacts`) are absent from public reports

`StatementFacts` is **pre-filter** raw analyzer output —
`StatementFactsCollector::visit_ident` records every `Ident::to_id()`
the visitor encounters, including reads inside nested function bodies
with inner-scope marks. Those inner-scope `Id`s are `Globals`-bound:
their `ctxt: u32` is meaningful only inside the SWC `Globals` that
minted them.

Public reports omit these raw facts. Staged analysis artifacts do retain them
through explicit saved data: every SWC binding is represented by its symbol and
a `ContextOrdinals` index, then restored against the consumer's freshly parsed
modules. This preserves closure-local shadowing without serializing a
process-local context number. Reconstructing all bindings as
`top_level_id(name, fresh_mark)` would make a closure-local `counter` collide
with a top-level `counter`. See `docs/lessons_learned/cross_process_stage_b.md`.

## Reader audiences

| Consumer                                                                                                                     | Reads                                                                    | Cross-process?          |
| ---------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ | ----------------------- |
| Spec author with `jq`                                                                                                        | `dependency_graph.json.gz`, `cycles.json`, `atomic_group_conflicts.json` | yes (Atom-only)         |
| `debundle atoms` / `coverage` / `graph-summary` / `scc` / `cluster` / `describe` / `inspect-source --id` / `modules propose` | `dependency_graph.json.gz`, source bytes + spec                          | yes (Atom-only)         |
| `debundle gate diagnose <binding>`                                                                                           | `dependency_graph.json.gz`, source bytes + module spec                   | yes (Atom-only)         |
| `debundle bindings assign` / `bindings rename` / `modules merge`                                                             | spec YAMLs + `dependency_graph.json.gz` (gate)                           | yes                     |
| Materializer (`debundle run`)                                                                                                | spec + chunk bytes + everything in-process                               | N/A — always in-process |

## Reconstruction recipe

For files following the Atom-only convention, a reader does:

```rust
let mark_b = /* the chunk's top_level_mark, set by the reader's own resolver pass */;
let global = (atom_from_wire, SyntaxContext::empty());
let top_level = top_level_id(atom_from_wire, mark_b);
```

## Convention: one canonical module identity (`ModulePath`)

A logical module has exactly one identity: `spec::ModulePath`, the
chunk-relative path the module emits to, in canonical form —
**relative, slash-separated, lowercase** (`domains/system/ids`). The
on-disk spec file (`modules/domains/system/ids.yaml`), the module
table's `path`, and the active-claim lookup all denote a module by
this same value.

`ModulePath::parse` is the only constructor: it strips a leading
`"<chunk_id>::"` (the in-process `PlannedModule.id` spelling minted in
`lowering/plans.rs`), lowercases, and normalizes separators. Two
spellings of one module therefore collapse to a single value, so `==`
is an honest identity test — this is what makes the peel proposer's
self-merge bug structurally impossible.

The internal array handle `ModuleId(usize)` is a dense in-process index
for O(1) lookups; it is distinct from `ModulePath` and is not the public
identity. It remains `#[serde(transparent)]`, so serialization is the
underlying number.

## Convention: interned module references + a single module table

Module identity is **interned** on the wire. `dependency_graph.json.gz` (a.k.a.
`module_graph`) carries exactly one module table — `module_graph.nodes`,
a list of `ModuleEntry { key, path, kind }` — and the table is the
**single source of truth** for each module's path and destination kind.
Everything that points at a module carries only the interned
`ModuleKey` (`"logical:N"`):

| Field                                     | Type          |
| ----------------------------------------- | ------------- |
| `nodes[].destination`                     | `ModuleKey`   |
| `module_graph.nodes[]`                    | `ModuleEntry` |
| `module_graph.edges[].source` / `.target` | `ModuleKey`   |
| `module_graph.sccs[].modules[]`           | `ModuleKey`   |
| `atomic_graph.nodes[].destinations[]`     | `ModuleKey`   |

There is no second encoding of a module: a reference is a key, the path
and destination kind live once in the table, and a consumer resolves a key
via `DependencyGraphReport::module(key)` / `is_unassigned_or_entry(key)`.
`kind` is `entry` for the physical entry destination, `specified` for an
explicit planned output, or `unassigned` for a planned output receiving
unclaimed source units. The authored `residual/` path namespace does not
determine the destination kind.
The entry's `path` is the historical report label `anon_residual_sentinel`;
the emitted entry filename is chosen separately by the chunk configuration.

## Binding-ordering effect provenance

`dependency_graph.json.gz` `edges[].sequenced_cause` for the `binding_timeline`
and `shared_var_access` rules carries `source` and `target`, and
`uninitialized_observation` carries `observation`: the provenance of that
endpoint's binding effect, `{"kind": "observed"}` or
`{"kind": "fallback", "reason": {"path": …}}`; `fallback_summary` carries
`loss` in the `synchronous_execution.precision_losses[].reason` shape and
`whole_body_promotion` carries `operation`. The fields are
required: a graph written before them does not deserialize, so the
commuting-statements gate never reads an unclassified effect as assumed.

A `possible_getter` reason additionally requires `lookup`, with the lexical
source-unit ordinal and SWC byte-position span, and `promotion`. The latter is
`{"kind":"lazy_owner","source_owner":N}` when one lazy source owner was
promoted, or `{"kind":"raw_callbacks"}` when raw callback promotion has no
single identified owner. The binding effect keeps one deterministic reason;
this witness does not enumerate all contributing lookups or owners. Global
access evidence coalesces possible-getter reasons to the lexicographically
smallest lookup and promotion witness for that access site, so its summary
size stays bounded by reason categories. The current reader requires this
shape; the former unit `possible_getter` reason has no legacy decoding path.

## Ordered call-result refusal

`dependency_graph.json.gz.nodes[].synchronous_execution.precision_losses[]`
uses `reason: {"code": "missing_call_result"}` when an ordered call's effect
proof completed but supplied no return-value transfer. The entry retains the
call's lexical statement ordinal, source location and fallback reach. It may
also appear as a `fallback_summary.loss` on assumed binding effects.
The ordered body still stops and promotes its conservative remainder.
Declining an ordered target does not establish this reason. Its
`unsupported_operation` / `opaque_execution` loss may carry a typed
`ordered_call_admission_refusal`; see [ordered call admission refusal](#ordered-call-admission-refusal).
Incomplete invocation remains a generic unsupported-operation loss, and
unknown-target classification remains a separate precision loss under the
existing target rules.

## Ordered call admission refusal

`dependency_graph.json.gz.nodes[].synchronous_execution.precision_losses[]`
may include `reason.ordered_call_admission_refusal` for a call whose already
evaluated operands were declined by the ordered path. The values are
`unknown_target`, `multiple_targets`, `non_local_target`,
and `unsupported_receiver`. The reason retains only the
first failed admission category, with the existing call source site and
fallback reach. It does not retain target identities or alter call admission,
effect promotion, reachability, or gate acceptance. A complete effects-only
call uses `missing_call_result`; an incomplete invocation does not use this
field.

## Ordered local member-read refusal

`dependency_graph.json.gz.nodes[].synchronous_execution.precision_losses[]`
may include `reason.ordered_field_read_refusal` for a conservative `member_read`.
Its values are `receiver_not_singleton`, `receiver_not_object`,
`borrowed_receiver`, `unsupported_record_shape`, `allocation_not_fresh`,
`unsettled_receiver`, `missing_field`, `non_data_field`, and
`missing_heap_value`. The entry retains the existing operation and source site;
the refusal identifies the first failed condition in the shared local
own-data guard. `allocation_not_fresh` does not identify a distinct runtime
allocation. This optional diagnostic does not change effect promotion or gate
admission. Older reports omit it.

## Shared purity causes

An impure known-function call has one `impure_function_call` reason at the
call site. Its optional `cause_ref` points to an entry in the same
`dependency_graph.json.gz` file's `purity_causes[]` table. Each entry has an `id`
and a `purity` verdict whose reasons may point to further entries. Follow
these links to inspect the callee's full cause chain; shared callees are
stored once rather than copied into every caller. A direct reason's
`source_location` names the source file where that reason arose, including
when the call crosses chunks. The table is absent when no source unit references
an impure known-function call. Older reports without the table or
`cause_ref` still deserialize.

## Callback-factory barrier diagnostics

`dependency_graph.json.gz.nodes[].callback_factory_barriers[]` is optional and
omitted for accepted calls. It records a matched retained callback-factory
signature that the conservative analyzer rejected. The currently supported
rejections are `rejection.code = "namespace_member_write"` for a static member
write and `"namespace_dynamic_property_write"` for a computed-key write
through the namespace binding or a recognized alias. Their `witness` identifies
the receiver, optional static property, graph `source_unit` ref,
`statement_ordinal`, and optional `source_location`. The
`"namespace_call_argument"` rejection records a direct namespace binding
passed as a non-spread ordinary top-level call argument. Its witness carries
the exact source unit, SWC call span, argument index, and namespace binding.
`opaque_call` is true only if a recorded `UnknownCallTarget` precision loss
matches that same source unit and call span. It does not claim an actual
mutation. The
`rejection.code = "unsupported_guard"` value preserves a rejected match whose
cause is outside this write-witness family. This diagnostic field does not
alter promotion or acceptance decisions. A computed-key write through a more
complex receiver remains `unsupported_guard` unless another supported write
also rejects the contract.

Each contract may include `callback_source_units`, a list of graph source-unit
refs and their `statement_ordinal`s for callback arguments resolved directly
through whole-chunk value flow. The field is omitted when the argument shape
cannot be linked without inference. `gate diagnose` treats the contract as
linked only when the promoted edge's `callee_source_unit` matches one of these
refs; a mismatch or unresolved source remains inconclusive. Contract rows
include only overloads applicable to the actual argument count and callback
positions.

`debundle gate diagnose` joins these rows to constraining edges tagged
`role.kind = "promoted_at_init"`, returns bounded traces with counts and
coverage status, and preserves the witness's graph source-unit reference and
ordinal. The wire format has no parsed statement ID: diagnose does not read or
validate source inputs against the graph-generation fingerprint, so consumers
must not apply a possibly stale `SourceUnitOrdinal -> StatementId` crosswalk.
The explicit-path inspection
command remains available to inspect the graph source-unit reference using the
supplied graph, module spec, and source root. `query_status` summarizes all
uniquely linked candidate promoted edges even when returned traces are bounded; the
trace list itself remains limited by `--limit` or the default. A local witness
does not imply that it is the only or globally earliest cause. If a source
unit has multiple candidate barriers, or the edge's callee source does not
match a recorded callback root, `gate diagnose` marks the link inconclusive
instead of cross-pairing a contract with a promoted callback edge. A write
witness is selected by source-unit order and is deterministic within its source
unit; it does not establish token order among writes in that unit.

`gate describe` can also emit `sequenced_source_inspections` when given both
`--modules` and `--source-root`. Each command is keyed by the already stored
impure source unit on a sequencing edge and uses `inspect-source` to navigate
its purity reason.

## Module identity everywhere else: `ModulePath` / `ModuleRef`

Every artifact outside `dependency_graph.json.gz` denotes modules by
canonical `ModulePath` — never by interned key, and never by the
in-process `"<chunk_id>::<path>"` spelling (`PlannedModule.id`,
which exists only in memory). Per-chunk files use the bare path (the
chunk is implicit); tree-wide files qualify it with the chunk id as
a `ModuleRef { chunk_id, path }` object.

| File                          | Module-identity fields                                                                   |
| ----------------------------- | ---------------------------------------------------------------------------------------- |
| `cycles.json` (per chunk)     | `modules[]`, `cut[].from`, `cut[].to`, `pass_one_witness.steps[].from/to` — `ModulePath` |
| `atomic_group_conflicts.json` | `claims[].module` — `ModulePath` (source-unit IDs are `"unit:N"` strings)                |
| `modules.json` (per chunk)    | `final_module_contents[].path`, `requested_logical_modules[].target_path` — `ModulePath` |
| tree `index.json` (per dir)   | `modules[]` — `ModuleRef`; per-file rows carry `module: ModuleRef`                       |
| chunk summary `linker_order`  | `ModulePath`                                                                             |
| stderr rejection summaries    | every module named in cycle / atom-split / unmatched-claim blocks                        |

## Module binding source-unit associations

Each `modules.json.final_module_contents[]` row has `binding_source_units`,
an array with one `{binding_name, source_unit_ids}` row per `binding_names`
entry. Rows follow the same input-binding-name order as `binding_names`;
`member_names` remains positionally aligned with that array. Each
`source_unit_ids` value is a sorted, deduplicated list of chunk-local graph IDs
encoded as `unit:N`, ordered by numeric `SourceUnitId`.

The association lists every graph source unit whose `declared_bindings` contains
that input binding. It includes every declaration site for redeclared bindings;
IDs follow the analysis's source-unit boundaries, including units created by
splitting a comma-list declaration. Reads, writes, and calls do not count as
declarations. A binding with no declaring source unit has an empty list. The
existing module-level `source_unit_ids` remains the aggregate of the named
bindings' declaration IDs and does not pair IDs with individual names.

`CycleEdge` rows in `cycles.json` (`cut[]`, `pass_one_witness.steps[]`, `lazy_closure[]`, and
`inert_commuting_edges[]`) carry `from_source_unit_id` and
`to_source_unit_id` as the actual dependency-edge endpoints, using the same
`unit:N` identities as `dependency_graph.json.gz`'s `nodes[].id`,
`edges[].source`, and `edges[].target`. These IDs identify the source units
whose destinations project to the row's `from` and `to`; `statement_ordinal`
remains edge-reason provenance and does not identify either endpoint. `gate
describe`'s `cut` and `evidence` rows and `spec propose-commutations`' nested
`edge` rows use the same endpoint fields.

Cross-file join recipe (what `debundle gate describe` does): resolve
each source unit's `destination` key through the dependency graph's module table
to its `path`, then intersect those paths with the SCC's
`cycles.json` `modules` set. Both sides carry the same canonical
`ModulePath` values, so the join is plain equality — no prefix
stripping or string surgery.

## Related documents

- `docs/design.md` §"Two classes of atom" — the realizability theorem
  these wire formats serialize evidence for.
- `docs/lessons_learned/cross_process_stage_b.md` — why we don't
  serialize pre-filter facts.
