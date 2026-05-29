# Debundle naming/owner harmonization

Strong types + one exact convention + zero ambiguity for the
module-identity and owner concepts. Replaces stringly-typed names that
caused the `1eb2cd2` self-merge bug.

## Decisions (locked)

- **Module identity → Convention A (path-as-identity).** A `ModulePath`
  newtype is the single module identity. Canonical spelling: relative,
  slash-separated, lowercase; **`id == dest path`** (dest file = path +
  extension, no derivation step). The constructor is the only way to
  build one and normalizes at the boundary (strips any `"<chunk_id>::"`
  prefix, rejects backslashes / leading-trailing `/` / `..`).
- **Destination id → eliminated (not just typed).** The owner-graph
  `ModuleReportRef.destination` currently carries four encodings of one
  identity: `id: String` (`"logical:N"`), `index` (the same
  `LogicalModuleIndex`), `label` (the path, sometimes chunk-prefixed),
  `residual: bool`, and `target_file`. `id`/`index` exist only because
  `label` wasn't canonical, forcing `spec_module_groups` to group by the
  stable `"logical:N"` string. Once `label` is a canonical `ModulePath`,
  grouping keys on the path; `residual` is `path.is_residual()`;
  `target_file` is `path.dest_file(ext)`. So `ModuleReportRef` collapses
  to `{ path: ModulePath }` and the `"logical:N"` destination id is
  **removed from the wire**, not re-typed. If the materializer needs a
  stable pre-path handle internally, that stays as the existing typed
  `ModuleId` (`ids.rs`) and is never serialized.
  - **Verification gate (do before deleting):** confirm no consumer
    references a module by index independently of its path (a `ModuleId`
    handle held before the path is known). All sites read so far
    (`peel/factorize.rs`, `spec_modules.rs`) use it only as a
    grouping/dedup key, which `ModulePath` subsumes. Grep
    `destination.id`, `destination.index`, `"logical:`, and every
    `ModuleReportRef` construction before removal.
- **`owner` concept → element-only rename to `Node`.** Graph element
  becomes `Node`/`NodeId` (wire `"node:N"`); the colliding binding field
  `BindingKind::Owned { owner: ModuleId }` becomes `{ module: ModuleId }`.
  The graph **keeps** its established name "owner graph" /
  `owner_graph.json` (no wire/doc rename).
- **Binding name → discriminated newtype** distinguishing the minified
  name from a readable rename, replacing the conflated `String` query
  surface in the CLI.
- **Scope → full harmonization sweep**, executed as the staged,
  individually-compiling commits below.

## Root-cause framing

`peel/factorize.rs` derives a module's identity as a bare `String` via a
4-way fallback (`active_claim → destination.label → destination.target_file
→ destination.id`). Production owner-graph destinations spell the label
`"<chunk_id>::<path>"`; active claims spell it `"<path>"`. Two spellings
of one module → a bogus `merge_into` self-merge. The shipped fix
(`canonical_module_label`) strips the prefix at one call site — a
band-aid. Making `ModulePath`'s constructor normalize means the two
spellings collapse to one value at construction, `==` can no longer lie,
and the band-aid is deleted. Same reasoning removes the latent
bug-class anywhere else labels are compared as strings.

## Stages (each compiles + tests green before the next)

### Stage 1 — `ModulePath` newtype + root self-merge fix

- Add `ModulePath` to `ids.rs`:
  - `struct ModulePath(String)` (canonical slash/lowercase form), `serde(transparent)`.
  - `ModulePath::parse(raw: &str, chunk: ChunkId, table: &ChunkTable) -> Result<Self>`
    — strips `"<chunk_name>::"` prefix, normalizes separators, validates
    charset, rejects `..`/absolute/empty.
  - `fn dest_file(&self, ext: &str) -> String`, `fn is_residual(&self) -> bool`
    (folds in `spec_modules::is_residual_module_path`), `Display`.
- Thread `ModulePath` through the proposal path:
  - `spec_modules::load_active_claims` → `BTreeMap<BindingName, ModulePath>`
    (keys typed in Stage 3; values typed here).
  - `peel/factorize.rs`: `active_module_label` returns `ModulePath`;
    `class_to_labels: BTreeMap<ClassId, BTreeSet<ModulePath>>`;
    `spec_module_groups` keys by `ModulePath` (replacing the
    `node.destination.id` grouping — this is what removes the
    destination id's reason to exist); proposal
    `source`/`target`/`merge_into` carry `ModulePath`.
  - `ModuleReportRef` (analysis crate) collapses to `{ path: ModulePath }`;
    drop `id`/`index`/`residual`/`target_file` once the verification gate
    passes. This is a wire change to `owner_graph.json` (the `destination`
    object loses `id`/`index`/`residual`/`target_file`, keeps the path);
    update the checked-in specimen + any `jq` consumers in the same stage.
  - **Delete `canonical_module_label`** (normalization now lives in the
    constructor) and its band-aid `chunk_id` plumbing.
- Keep the `1eb2cd2` regression test
  (`chunk_prefixed_and_clean_label_of_one_module_do_not_self_merge`);
  add a constructor unit test asserting both spellings parse equal.
- `module_path_from_file` returns `ModulePath`.

### Stage 2 — `Node` rename (mechanical)

- `OwnerIdx` (peel) and `OwnerId` (graph.rs), if both exist, fold into a
  single `NodeId` in `ids.rs`. Update `graph.nodes`, `OwnerGraphNodeReport`
  consumers, `quotient.class_of`, etc.
- Wire id strings `"owner:N"` → `"node:N"` (parse + format in one helper
  on `NodeId`; this is internal to `*Report.id`, not a file rename).
- `BindingKind::Owned { owner: ModuleId }` → `{ module: ModuleId }`;
  update all match sites.
- Names that legitimately stay: "owner graph", `owner_graph.json`,
  `compute_owner_graph_and_units`, `declared_bindings`.

### Stage 3 — `BindingName` discriminated newtype

- `enum BindingName { Minified(Atom), Readable { minified: Atom, name: Atom } }`
  (placement: `ids.rs`).
- Replace the dual minified/readable `String` matching in
  `cli/binding.rs::find_matches` with typed dispatch; ambiguity error
  unchanged in behavior.
- Type the `load_active_claims` / `ModuleClaims.bindings` keys.

### Stage 4 — docs + backlog reconciliation

- `WIRE_FORMAT.md`: document `"node:N"` element ids and the `ModulePath`
  canonical spelling (note `owner_graph.json` name retained).
- `ids.rs` doc comments for each newtype; one short note in `README.md` /
  `AGENTS.md` "naming model".
- Strike the now-resolved entries in `ARCHITECTURE_BACKLOG.md` /
  `CODE_REVIEW.md` (label-spelling smear, `owner` overload,
  `SourceImportResolution` tuple if touched).
- Tombstone-free: this is an atomic in-repo API change, all callers
  updated in the same commits.

## Build / test loop (per stage)

```
bbr build //devinfra/js/debundle/...
bbr test  //devinfra/js/debundle/...
```

Run with `dangerouslyDisableSandbox: true` (Bazel + RBE). The crate has
~53 e2e tests under `e2e/` and snapshot-ish CLI tests; several spell
`"owner:N"` ids and module-label strings and will need updating in the
same stage that changes them. No tests skipped.

## Risks / call-outs

- **Wire compatibility:** `"owner:N"` → `"node:N"` changes
  `owner_graph.json` element ids. The checked-in specimen at
  `props/specimens/ducktape/2026-05-20-00/code/...` is a frozen RE
  fixture — confirm whether it must be regenerated or left as a
  historical snapshot before touching it.
- **Atom-only wire invariant** (`WIRE_FORMAT.md`) is preserved —
  `BindingName` serializes to the same `Atom` string on the wire; the
  enum is an in-process distinction.
- `ModulePath` must round-trip the existing spec on-disk layout exactly
  (the `module_path_from_file` tests pin `"ui/list"` etc.) so no spec
  YAML files move.

## Delivery

Branch `claude/laughing-bardeen-TUOIa`; one commit per stage; push after
each stage compiles + tests green. No PR unless requested.
