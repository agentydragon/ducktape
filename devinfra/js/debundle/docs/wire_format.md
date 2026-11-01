# Wire format conventions (per-chunk JSON reports)

The per-chunk JSON files debundle writes under `reports/tree/<chunk_id>/`
are consumed by:

- spec authors with `jq` poking at cycle / dependency-graph / atomic-group reports;
- CLI tooling (top-level `debundle modules propose` / `atoms` /
  `coverage` / `describe` / `inspect-source --id` / `scc` / `cluster` /
  `graph-summary` / `gate diagnose`);
- humans reading the files during debugging.

This doc states the convention these files follow.

## Convention: `Atom`-only on the wire

Every JSON file carries binding identities as `Atom`s (interned
strings), **not** as SWC `Id = (Atom, SyntaxContext)`. The
`SyntaxContext` half is dropped at serialization.

| File                          | Field carrying binding identity                                                                                                                                                                                                              |
| ----------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `dependency_graph.json.gz`    | `nodes[].declared_bindings[].binding: Atom`, `edges[].binding: Option<Atom>`, `edges[].sequenced_cause` binding/cell names                                                                                                                   |
| `cycles.json`                 | `cut[].binding: Option<Atom>`, `cut[].from_binding: Option<Atom>`, `cut[].explanation` binding/cell names (evidence is recomputed on demand by `debundle gate describe`); `rejection` names the pass (SPEC § Realizability cut explanations) |
| `atomic_group_conflicts.json` | `claims[].binding_names: Vec<Atom>`                                                                                                                                                                                                          |

`Atom` serializes as a plain JSON string via `swc_atoms`'s own
`Serialize` impl. That impl writes the **string content** — interned
in memory, but the wire form is the string itself, which is portable.

## Program graph

Full graph files use `.json.gz`: deterministic gzip level-1 streams containing
the complete existing compact JSON payload. Writers finish the compression
footer and flush the file before reporting success; no plaintext full graph
is staged or emitted. Small JSON reports and JSONL are unchanged. Saved-graph
readers accept gzip and historical explicit plain JSON by content and validate
the complete compressed stream, including checksums and trailing bytes. For
shell inspection, use `gzip -dc dependency_graph.json.gz | jq ...`.

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

### Export-reentry diagnostics

`export_reentry.json` is a per-selected-chunk diagnostic, not analyzer input.
Its `host_behavior` echoes the spec's typed block as read (SPEC.md § Host
behavior; omitted keys absent, `synchronous_module_loader` always present),
and `policy` is `conservative`, `no_static_importer_reentry` or `{"reentry_through": [names]}`. `loader` is
the loader derivation (SPEC.md § Loader-closed derivation), tagged by `rule`:
`no_synchronous_loader`, `no_host_loader_sites`, or `loader_closure` with
`closed`, `host_keys` (keys a closed certificate consumed), `requests`
(resolved literal requests per source chunk, specifier and `target`
`{"builtin": id}` or `{"chunk": {"chunk", "file"}}`, with `request_count`),
`loaded_chunks`, `suspended_sites` (mandatorily suspended sites excluded under
`synchronous_load_drains_microtasks: never`), `unreachable_sites` (sites no
reachable code can enter), and `failures`. Each failure has a `reason`, its
`occurrences`, and the `first` witness in chunk-name then site order: either
`{"host_behavior": key}` or `{"site": {...}}`, a site naming its
`source_chunk` and `form` (`interop` with the census `kind`, `item`, `line`,
`specifier` and `resolved_target`; `static_request` with `specifier` and
`resolved_target`; `loader_binding_use` with `item`, `line`, `call`,
`specifier` and `resolved_target` for a use of an exported loader alias;
`namespace_request` with `item`, `specifier` and `resolved_target`;
`top_level_await`; `vendor_chunk`).
`host_builtin_leaves`, when nonempty, lists each consumed
`reentry_free_builtin_modules` or `reentry_free_bun_modules` static request;
`builtin` holds the module ID either way. The typed
`global_blockers` and `closure_blockers` arrays use a `reason` tag. Chunk
references are canonical chunk IDs; static-request entries carry the source
chunk and original specifier, with `resolved_target: {"chunk", "file"}` when
the index resolves one. The global predicates are collected once per
materialization; closure predicates are checked for each selected target even
when a global blocker already forces the conservative policy. The report
never relaxes the proof.

```json
{
  "host_behavior": { "synchronous_module_loader": "present" },
  "policy": "conservative",
  "loader": {
    "rule": "loader_closure",
    "closed": false,
    "failures": [
      {
        "reason": "undeclared_host_behavior",
        "occurrences": 2,
        "first": { "host_behavior": "loader_result_for_evaluating_module" }
      },
      {
        "reason": "opaque_loader_site",
        "occurrences": 1,
        "first": {
          "site": {
            "source_chunk": "static/provider",
            "form": "interop",
            "kind": "global_this_require",
            "item": 0,
            "line": 1
          }
        }
      }
    ]
  },
  "global_blockers": [],
  "closure_blockers": [
    { "reason": "open_replacement_bundle", "source_chunk": "static/provider" },
    { "reason": "target_cycle", "target_chunk": "static/provider" }
  ]
}
```

### Translation validation

`effect_translation_validation.json` contains conservative effect-set
diagnostics beside the unchanged legacy gate and edge-TV reports. It is
report-only: `legacy_gate_accepts` and `legacy_tv_accepts` retain the current
verdicts. `counts` includes every conflicting swapped pair, cell incidence,
Havoc pair, existing premise disposition and observed contradiction. The
legacy comparison counts ordinary swapped edge conflicts separately from
the legacy report's forward-edge and placement diagnostics.

`coverage.unknown_coverage` counts missing global/property/host domains
independently of legacy dataflow opacity, execution incompleteness and heap
opacity. Unknown domains remain global Havoc; `full_semantic_precision` is
false. `examples` retains at most twenty deterministic pairs, each with at
most twenty conflicting cells and their access provenance. Each sampled pair
retains its exact `cell_count` and `cells_truncated` marker;
`examples_truncated` makes omitted pairs explicit. Counts remain exact when
examples are truncated. `full_detail_export_available` is true: request
`debundle run --effect-conflicts-jsonl` for complete per-chunk rows in
`effect_conflicts.jsonl`. The default does not write this file.
Repeatable `--effect-conflicts-chunk <chunk-id>` selects only JSONL sinks;
it leaves every graph, bounded summary, source input and census scope intact.

Each JSONL line is one pair in the same deterministic order used to select
summary examples. It includes `runs_first`, `runs_later`, every `cells` entry
with access provenance, exact `cell_count`, `havoc`, typed `disposition`,
`premise_locations`, and `legacy_edge_conflict`. `first_opacity` and
`later_opacity` retain each endpoint's unknown coverage, dataflow opacity,
execution completeness/losses and heap opacity. Cells serialize one at a time;
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
report-only edge-TV verdict;
the gate remains authoritative. `gate_accepts`, `accepts` and `swapped_pairs`
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

## Why pre-filter facts (`StatementFacts`) aren't on the wire

`StatementFacts` is **pre-filter** raw analyzer output —
`StatementFactsCollector::visit_ident` records every `Ident::to_id()`
the visitor encounters, including reads inside nested function bodies
with inner-scope marks. Those inner-scope `Id`s are `Globals`-bound:
their `ctxt: u32` is meaningful only inside the SWC `Globals` that
minted them.

We don't serialize them. An `Atom`-only shape that reconstructs `Id`s via
`top_level_id(name, fresh_mark)` is unsound under shadowing (a closure-local
`counter` would collide with a top-level `counter`); a SWC hygiene-snapshot
replay and pre-filtering inner-scope reads don't pay for themselves. See
`docs/lessons_learned/cross_process_stage_b.md`.

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
| `cycles.json` (per chunk)     | `modules[]`, `cut[].from`, `cut[].to` — `ModulePath`                                     |
| `atomic_group_conflicts.json` | `claims[].module` — `ModulePath` (source-unit IDs are `"unit:N"` strings)                |
| `modules.json` (per chunk)    | `final_module_contents[].path`, `requested_logical_modules[].target_path` — `ModulePath` |
| tree `index.json` (per dir)   | `modules[]` — `ModuleRef`; per-file rows carry `module: ModuleRef`                       |
| chunk summary `linker_order`  | `ModulePath`                                                                             |
| stderr rejection summaries    | every module named in cycle / atom-split / unmatched-claim blocks                        |

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
