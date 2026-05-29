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

  **Revised after grounding (2026-05-29):** a `grep` showed "owner" is
  used ~1158× and is internally coherent — an _owner_ IS a graph node
  (a top-level statement that owns binding declarations); `OwnerId`,
  `OwnerGraph`, `OwnerGraphNodeReport`, and `owner_edge` all hang
  together. Since we keep the name "owner graph", renaming the element
  to `Node` while keeping `OwnerGraph*` would make the codebase _less_
  consistent, not more. So the `OwnerId → NodeId` / `owner:N → node:N`
  rename is **backed out** (recorded as a future TODO below). The only
  genuine collision is the field `BindingKind::Owned { owner: ModuleId }`,
  where `owner` means _owning module_ — a different sense from
  "owner = node". That one field is renamed to `module`.

- **Binding name → discriminated newtype** distinguishing the minified
  name from a readable rename, replacing the conflated `String` query
  surface in the CLI.
- **Scope → harmonization sweep** (module identity + the narrow
  `owner`-field de-ambiguation + binding name), executed as the staged,
  individually-compiling commits below. The wholesale `owner → node`
  rename is explicitly out of scope.

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

### Stage 2 — De-ambiguate the `owner` binding field (non-wire)

The wholesale `Node` rename is **out of scope** (see decision above). The
single genuine collision is the binding's destination field:

- `BindingKind::Owned { owner: ModuleId }` (`ids.rs`) → `{ module: ModuleId }`;
  update all match/construction sites. Here `owner` meant _owning
  module_, which collided with "owner = graph node"; `module` is
  unambiguous.
- No wire change: this enum is in-process only (the wire carries
  `BindingKind` indirectly via reports that already use `ModuleReportRef`).
- Everything else keeps the `owner` term: `OwnerId`, `OwnerGraph`,
  `OwnerGraphNodeReport`, `owner_edge`, `owner:N`, `owner_graph.json` —
  all coherent under "owner = node that owns bindings".

### Stage 3 — `BindingName` discriminated newtype

- `enum BindingName { Minified(Atom), Readable { minified: Atom, name: Atom } }`
  (placement: `ids.rs`).
- Replace the dual minified/readable `String` matching in
  `cli/binding.rs::find_matches` with typed dispatch; ambiguity error
  unchanged in behavior.
- Type the `load_active_claims` / `ModuleClaims.bindings` keys.

### Stage 4 — docs + backlog reconciliation

- `WIRE_FORMAT.md`: document the `ModulePath` canonical spelling
  (`owner_graph.json` element ids and name unchanged).
- `ids.rs` / `spec.rs` doc comments for each newtype; one short note in
  `README.md` / `AGENTS.md` "naming model".
- Strike the now-resolved entries in `ARCHITECTURE_BACKLOG.md` /
  `CODE_REVIEW.md` (label-spelling smear; `SourceImportResolution` tuple
  if touched).
- Tombstone-free: this is an atomic in-repo API change, all callers
  updated in the same commits.

## Out of scope / future TODO

- **`owner → node` rename.** Folding `OwnerId`/`OwnerIdx` into one
  `NodeId`, renaming `OwnerGraph*` → `NodeGraph*`, and the wire ids
  `owner:N → node:N`. Backed out (2026-05-29): "owner" is a coherent,
  pervasive term (~1158 uses) and an owner genuinely _is_ a graph node;
  a half-rename would worsen consistency, and a full rename is
  ~1100+ sites, breaks the wire format, and diverges the frozen
  specimen. Revisit only as a deliberate wholesale rename, not as part
  of this sweep.

## Build / test loop (per stage)

```
/tmp/bz.sh build //devinfra/js/debundle/...
/tmp/bz.sh test  //devinfra/js/debundle/...
```

`bbr` is unusable in this environment (git-state mirror exceeds the
75MB gRPC cap because `props/specimens/` embeds a full repo copy), so we
drive `bazelisk` locally with the session bazelrc (JVM truststore for
proxy TLS) + the sops BuildBuddy key + `--config=nolint` (the
python/node lint aspects need network-bound stub wheels; CI runs full
lint). All Bash with `dangerouslyDisableSandbox: true`. No tests skipped.

## Risks / call-outs

- **Specimen is frozen** (decision 2026-05-29): the checked-in
  `props/specimens/ducktape/2026-05-20-00/code/...` RE fixture is **not**
  touched by this sweep. Its embedded copy of the tool diverges from the
  live tool; that is accepted.
- **Atom-only wire invariant** (`WIRE_FORMAT.md`) is preserved —
  `BindingName` serializes to the same `Atom` string on the wire; the
  enum is an in-process distinction.
- `ModulePath` must round-trip the existing spec on-disk layout exactly
  (the `module_path_from_file` tests pin `"ui/list"` etc.) so no spec
  YAML files move.

## Delivery

Branch `claude/laughing-bardeen-TUOIa`; one commit per stage; push after
each stage compiles + tests green. No PR unless requested.
