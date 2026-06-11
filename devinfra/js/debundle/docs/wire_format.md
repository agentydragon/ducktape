# Wire format conventions (per-chunk JSON reports)

The per-chunk JSON files debundle writes under `reports/tree/<chunk_id>/`
are consumed by:

- spec authors with `jq` poking at cycle / owner-graph / atomic-unit reports;
- CLI tooling (top-level `debundle modules propose` / `atoms` /
  `coverage` / `describe` / `show-source` / `scc` / `cluster` /
  `graph-summary`; deprecated `debundle peel <...>` aliases may still
  exist but new docs and scripts should not use them);
- humans reading the files during debugging.

This doc states the convention these files follow.

## Convention: `Atom`-only on the wire

Every JSON file carries binding identities as `Atom`s (interned
strings), **not** as SWC `Id = (Atom, SyntaxContext)`. The
`SyntaxContext` half is dropped at serialization.

| File                         | Field carrying binding identity                                                                                                  |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| `owner_graph.json`           | `nodes[].declared_bindings[].binding: Atom`, `edges[].binding: Option<Atom>`                                                     |
| `cycles.json`                | `cut[].binding: Option<Atom>`, `cut[].from_binding: Option<Atom>` (evidence is recomputed on demand by `debundle gate describe`) |
| `atomic_unit_conflicts.json` | `claims[].binding_names: Vec<Atom>`                                                                                              |

`Atom` serializes as a plain JSON string via `swc_atoms`'s own
`Serialize` impl. That impl writes the **string content** — interned
in memory, but the wire form is the string itself, which is portable.

### Why the convention works for these files

These files all carry **post-filter** data — data that has already
passed through `binding_owner.get(binding)` at `graph.rs:598`. That
lookup is keyed by the full `Id`. Anything whose `SyntaxContext` does
not match a chunk-top-level binding (closure-local reads with inner-
scope marks, globals with `SyntaxContext::empty()`, etc.) **misses the
lookup and never becomes an edge**.

Consequence: the only `Id`s that survive into the owner graph have
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

We don't serialize them. An earlier design proposed an `Atom`-only
shape that reconstructed `Id`s via `top_level_id(name, fresh_mark)`,
but that's unsound under shadowing (a closure-local `counter` would
collide with a top-level `counter`). The other paths considered (a
SWC hygiene-snapshot replay; pre-filtering inner-scope reads) didn't
pay for themselves. See
`docs/lessons_learned/cross_process_stage_b.md`.

## Reader audiences

| Consumer                                                                                                             | Reads                                                           | Cross-process?          |
| -------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- | ----------------------- |
| Spec author with `jq`                                                                                                | `owner_graph.json`, `cycles.json`, `atomic_unit_conflicts.json` | yes (Atom-only)         |
| `debundle atoms` / `coverage` / `graph-summary` / `scc` / `cluster` / `describe` / `show-source` / `modules propose` | `owner_graph.json`, source bytes + spec                         | yes (Atom-only)         |
| `debundle bindings assign` / `bindings rename` / `modules merge`                                                     | spec YAMLs + `owner_graph.json` (gate)                          | yes                     |
| Materializer (`debundle run`)                                                                                        | spec + chunk bytes + everything in-process                      | N/A — always in-process |

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
`"<chunk_id>::"` (the in-process `LogicalModule.id` spelling minted in
`lowering/plans.rs`), lowercases, and normalizes separators. Two
spellings of one module therefore collapse to a single value, so `==`
is an honest identity test — this is what makes the peel factorizer's
self-merge bug structurally impossible.

The internal array handle `ModuleId(LogicalModuleIndex)` is an
in-process index for O(1) lookups; it is distinct from `ModulePath` and
is not the public identity.

## Convention: interned module references + a single module table

Module identity is **interned** on the wire. `owner_graph.json` (a.k.a.
`module_graph`) carries exactly one module table — `module_graph.nodes`,
a list of `ModuleEntry { key, path, residual }` — and the table is the
**single source of truth** for each module's path and residual flag.
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
and residual flag live once in the table, and a consumer resolves a key
via `OwnerGraphReport::module(key)` / `is_residual(key)`. The former
`ModuleReportRef { id, label, residual, index, target_file }` — which
spelled one identity five ways — and the parallel `sccs[].labels` are
gone. Residual-ness is read from the table's authoritative `residual`
flag, never inferred from a key string.

## Module identity everywhere else: `ModulePath` / `ModuleRef`

Every artifact outside `owner_graph.json` denotes modules by
canonical `ModulePath` — never by interned key, and never by the
in-process `"<chunk_id>::<path>"` spelling (`LogicalModule.id`,
which exists only in memory). Per-chunk files use the bare path (the
chunk is implicit); tree-wide files qualify it with the chunk id as
a `ModuleRef { chunk_id, path }` object.

| File                         | Module-identity fields                                                                   |
| ---------------------------- | ---------------------------------------------------------------------------------------- |
| `cycles.json` (per chunk)    | `modules[]`, `cut[].from`, `cut[].to` — `ModulePath`                                     |
| `atomic_unit_conflicts.json` | `claims[].module` — `ModulePath` (owners are `"owner:N"` strings)                        |
| `modules.json` (per chunk)   | `final_module_contents[].path`, `requested_logical_modules[].target_path` — `ModulePath` |
| tree `index.json` (per dir)  | `modules[]` — `ModuleRef`; per-file rows carry `module: ModuleRef`                       |
| chunk summary `linker_order` | `ModulePath`                                                                             |
| stderr rejection summaries   | every module named in cycle / atom-split / unmatched-claim blocks                        |

Cross-file join recipe (what `debundle gate describe` does): resolve
each owner's `destination` key through the owner graph's module table
to its `path`, then intersect those paths with the SCC's
`cycles.json` `modules` set. Both sides carry the same canonical
`ModulePath` values, so the join is plain equality — no prefix
stripping or string surgery.

## Related documents

- `docs/design.md` §"Two classes of atom" — the realizability theorem
  these wire formats serialize evidence for.
- `docs/lessons_learned/cross_process_stage_b.md` — why we don't
  serialize pre-filter facts.
- `ARCHITECTURE_BACKLOG.md` — current architectural backlog.
