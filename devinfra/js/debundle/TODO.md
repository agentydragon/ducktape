# Debundler — Open Work Items

Forward-looking gaps in the Rust debundler. Items are written to be removed
once closed; this file is not a changelog.

## Excalidraw live-browser smoke

Build an open-source live-browser smoke test for the debundler against
a Bazel-managed Excalidraw bundle. The motivation: when a debundler
issue surfaces against a private upstream corpus (proxy crash, AST
corruption, missing chunk, emit shape regression, optimisation
behaviour bug), reproducing the failure on Excalidraw lets us share
the repro in a public bug report, write a regression test that runs
in ducktape's open CI, and avoid leaking proprietary upstream bundle
detail. Excalidraw is open-source and broadly representative of "real
React + vendored chunks + dynamic imports + service worker."

### Bundle build

Use a Bazel-managed Excalidraw build. Two viable paths:

- **Pull a prebuilt deploy** (snapshot a specific `excalidraw.com`
  publish). Requires keeping the snapshot fresh enough that
  upstream's auxiliary endpoints (if any) still work. Easier to
  bootstrap.
- **Build from source under Bazel** — Excalidraw's `excalidraw-app/`
  builds with Vite; reproduce that build (npm + vite via
  aspect_rules_js, or via a `genrule` shelling to npm) and feed the
  output into the pipeline. More work upfront, but the bundle is
  reproducible from a single git pin and we control the optimisation
  level / minifier settings.

Either way, the build configuration must produce a realistic
production bundle: minify on, identifier renames,
production-tree-shaken, real chunk-split boundaries. A development
build (with un-mangled names and source maps inlined) won't exercise
the debundler's RE-relevant code paths.

### Spec scope

Not the round-trip minimum — the spec should exercise realistic-ish
module extraction and rename paths, the same shape a private-corpus
spec runs. Concretely:

- `apply_vendor_annotations` + `swap_vendor_chunks` over a couple of
  Excalidraw's actual vendor chunks (React, Roughjs, Pointers, etc.)
  so vendor-swap edge cases (`named_from_default`,
  `named_from_module_default`, default-only, JSON-default) get covered
  on a real bundle, not only synthetic fixtures.
- `materialize_logical_modules` over a handful of pre-identified
  Excalidraw source modules — pick ones whose shape is recognisable
  in the compiled output (a clearly-bounded React component, a pure
  geometry helper, a state slice). Goal: prove the materialiser
  recovers approximately the right symbols/files from a real
  scrambled bundle.
- A small set of `logical_modules` rename entries on identifiers
  visible in the compiled output. Goal: exercise the rename pipeline
  at realistic aggressiveness.
- `emit_browser_harness`, relying on the always-on
  `rewrite_chunk_entry_specifiers` transform, so the output is a
  runnable app the live proxy can serve.

The exact module list / rename list is part of the implementation —
pick stable shapes that are unlikely to drift wildly when Excalidraw
upgrades. Stale picks become a self-test: if the materialiser fails
to find them, that's a real signal (either the bundle moved or our
matchers regressed).

### Smoke target contract

- runs `bazel test //devinfra/js/debundle/excalidraw:load_test` (or
  similar);
- builds the Excalidraw bundle through the shared `debundle_pipeline`
  rule (<pipeline.bzl>);
- starts the live-proxy binary against the resulting harness;
- drives a headless Chromium through the proxy, asserts:
  - no failed asset requests,
  - no console errors,
  - the canvas toolbar is visible (e.g. `[data-testid="toolbar"]`
    or whichever stable selector Excalidraw exposes),
  - a small interaction works (click the rectangle tool, click on
    the canvas, verify a shape was added — proves the React app is
    reactive after debundle).

### Hosting

Self-hosted, no MITM. Private-corpus smokes generally have to MITM the
live host because their auth/data is server-side; Excalidraw runs
entirely in the browser, so a self-hosted bundle is a fully working
app. Self-hosting removes the network dependency and CDN-rotation
flakiness (the test stays green even when excalidraw.com is down) and
matches the "reproduce against Excalidraw" workflow goal — a public,
deterministic smoke that doesn't depend on third-party uptime.

### Workflow rule

When a private-corpus debundler issue is tractable on Excalidraw too,
prefer landing the regression test on this Excalidraw target (or a
smaller minimised e2e under `devinfra/js/debundle/e2e/`) rather than
only fixing it behind the private repo. The latter loses the
public-CI signal and the public-bug-report leverage.

## Anonymous selector indexing in graph dumps

Today `anonymous_statements:` selectors resolve purely by AST-shape match
against the chunk's top-level statements (`match` / `source_match`); the
spec format has no owner field for them, so nothing leans on
author-provided owner hints. That keeps the format honest, but every edit
gate / coverage check touching anonymous statements has to re-parse source.

If that source resolution ever becomes too expensive, extend
`owner_graph.json` with enough machine data to resolve anonymous selectors
from the dump itself — a canonical emitted-JS string or AST fingerprint per
anonymous owner, keyed by owner id and statement ordinal — so CLI tools can
match `anonymous_statements[].match` against graph-owned statements without
reading source files. This is conditional on hitting that cost; not yet
observed. (The `owner:<id>`-in-spec-notes framing that used to live here was
stale — no such mechanism exists; `owner:N` ids are machine-generated graph
keys and a `describe`/`show-source` input, not a spec authoring hint.)

## Coverage views ignore `source_match` member claims

The CLI edit gate now resolves `members[].selector.source_match` and
`binding_groups:` claims through the same source-backed path `debundle run`
uses (`spec_modules::ModuleClaims::{member_selectors,binding_groups}` +
`anonymous_resolution::resolve_member_selector_claims`). The read-only
coverage/describe views in `peel/plan.rs` still consume only
`claims.bindings` + `claims.anonymous_selectors`, so a module whose members
are claimed via `source_match` under-reports in `debundle coverage` /
`describe <module>`. Diagnostics-only (no soundness impact); wire the same
resolution through those views when it next matters.

## Structural selector language

`source_match` and same-module `binding_groups` exist, but the selector
language still needs more shape when porting real minified bundles. Keep
new features generic and backed by synthetic e2e fixtures; do not encode
private corpus details in Ducktape.

- **Contextual selectors.** Add readable `before` / `after` / `near`
  anchors for cases where the selected statement or declaration is
  helper-boilerplate that appears multiple times. This should replace
  copied overlapping selector bodies and still reject ambiguous windows.
  Decorator-helper neighborhoods are the motivating generic shape: a
  helper declaration may be syntactically identical across many classes,
  but it becomes unambiguous when selected near the class or decorator
  calls whose property strings identify the target.
- **Constrained hole matching.** Multi-hole selectors need cheap local
  constraints so authors can say that a hole appears only as a specific
  argument, callback body, object property value, or statement-list slot. This
  should avoid scanning unrelated subtrees while preserving the current rule
  that ambiguous matches are hard errors.
- **Cross-module binding groups.** Current `binding_groups` export
  multiple bindings into one logical module. Add or design a form for
  one matched declaration context whose bindings should land in
  different modules, without repeating the whole source selector.
- **Selector debt reporting.** Shipped as `debundle spec selector-debt`:
  ranks name-only selectors by how minified the bound name looks, groups
  `source_match` bodies copied verbatim across members / anonymous
  statements / binding groups, and (via `--against`) diffs minified
  bindings across two spec versions. Still open is the **source-aware**
  column — flagging _near-ambiguous_ structural matches (a `source_match`
  that matches exactly one statement today but is one drifted subtree away
  from matching two). That needs the chunk AST / owner graph, not just the
  modules tree, so it is a separate report from the current spec-only one.
- **Selector linting.** The report now exists (`spec selector-debt`); the
  next step is an opt-in warning or gate that fails when a name-only
  selector over an autogenerated/minified source scores above a threshold,
  with an explicit escape hatch for cases where the name is genuinely
  stable or no better structural handle exists.
- **Matcher core cleanup.** Factor anonymous-statement and member-binding
  `source_match` resolution through a shared parse/canonicalize/window
  matcher before adding more selector variants.

## Logical materialization breadth

The current `materialize_logical_modules` covers top-level
function/class/variable declaration movement and explicit owner assignment.
Still to do:

- Full lowering matrix: binding placement reports, attached side-effects,
  staged-shell edge cases beyond the focused fixture.
- Owner-fragment modeling parity for nested declarations and re-exports.
- Keep new analysis tooling on the existing owner graph and embedded atomic
  DAG side outputs; do not add parallel selected-owner cache formats.

## Performance

Proposer-gate, `debundle run`, and materialize-stage performance work
all live in <perf/proposer.md>. Known lowering-side items not yet in
that log:

- `JsChunk::{get_file,get_file_mut,remove_file}` (`artifact.rs`) are
  linear scans over `files`, so passes that touch every file go O(n²)
  per chunk. A path-keyed index fixes it.
- `split_entry_body` (`lowering/lower.rs`) clones every retained
  statement out of the chunk AST even though the chunk body is
  replaced wholesale afterward; a draining/move-based split avoids
  the full-AST clone.

## Factorize / atomic-DAG docs drift

- **"Factorize" remains overloaded.** Broadly, factorization/assembly
  produces the authoritative owner partition (`factor_assembly.rs`),
  while `peel/factorize.rs` produces advisory planner proposals from
  the serialized atomic DAG (surfaced as `debundle modules propose`).
  Keep docs explicit about which one they mean.

## Analysis semantics breadth

The focused fixtures exercise the core access model. Validate or extend
behavior for:

- Class fields, static blocks, computed keys, nested function bodies.
- Replayable side-effect attachment.
- Top-level side-effect classification across uncommon initializer shapes.

The purity-classifier backlog (cross-chunk purity facts, block-bodied
enum IIFE forms, statement-level overrides, compositional proof) lives
in <x/purity_recursive.md>.

## Corpus breadth

Current passing surface is centered on small synthetic fixtures and the
mock browser bundle. Extend to:

- Large vendor-heavy graphs.
- Unusual dynamic import forms.
- HTML/runtime asset layouts outside the current corpus.

## Rename pipeline: collect → validate → execute _once_

Scope-aware down-payments have landed in `lowering/visitors.rs` /
`lowering/naturalize.rs` / `lowering/lower.rs`:

- **Source shadowing**: the in-place rename visitors
  (`IdentifierRenamer`, `RenameAndShorthandNaturalizer`) carry a
  `RenameScopeStack` and suppress a rename inside subtrees that re-bind
  the source name.
- **Target capture**: the same scope stack tracks rename TARGETS; a
  rename whose target is shadowed at the reference is withheld and
  recorded in the visitor's `captured` set, which every caller checks
  and rejects on (no silent capture, no partial application). Heuristic
  naturalizer renames pre-filter targets bound anywhere in the deriving
  node's subtree and are suppressed entirely instead.
- **Shorthand expansion**: renaming a binding read/declared through
  `{ a }` / `const { a } = o` shorthand expands to the key-value form
  (`{ a: b }`) so property keys survive the rename.
- **Labels**: label idents and their `break`/`continue` references are
  excluded from renaming (separate namespace).
- **Era-keyed consumers**: export locals and `binding_comments` keys
  remap through the module-scope (plan-driven) rename map only —
  `NaturalizedRenames` splits it from the merged heuristic map so
  scope-local heuristic renames can no longer remap a top-level export
  specifier or orphan a member comment.

**PR 1 landed (2026-06):** `lowering/rename_ledger.rs` holds the intent
buffer — `RenameLedger` accumulating `RenameIntent { scope, from: Id, to,
origin }` with scopes `Chunk | Module(ModuleId) | Function(span)` and
priority derived from origin (explicit > import-induced > heuristic).
`seal()` hard-errors when same-priority intents disagree on one binding's
target (naming both contributors) and projects the surviving intents into
the same maps the pre-ledger code built. The two explicit contributors
are converted (spec `chunk_renames`; plan-driven `export_name`s); the
remaining-contributor inventory lives in `rename_ledger.rs`'s module doc.

**PR 2 landed (2026-06):** every remaining contributor submits intents
and every application site consumes a sealed projection — heuristic
bound-source per-scope renames (`Function` scope; derived by
`ScopedHeuristicNaturalizer` over a scratch clone, replayed by
`SealedScopeRenameApplier`), heuristic free-source return-object aliases
(`Module` scope, per deriving function), entry- and module-side
import-local mints (`Chunk` / `Module` scope, `ImportInduced`), and
auto-grown residual public-export minting (new `EntryPublicExports`
scope). Because contributor derivation still depends on earlier
contributors' application, PR 2 seals at phase boundaries — one ledger
instance per former private map. Two previously silent last-write-wins
shapes are now seal-time hard errors: two deriving functions
free-aliasing one source to different targets, and two import-local
mints disagreeing on one binding.

**PR 3 landed (2026-06):** seal is the single validation point.
`RenameLedger::seal` takes per-scope occupancy facts (`SealValidation` /
`ScopeOccupancy`: root vs nested binding names from `scope_names.rs`,
the rename walk's observed capture pairs, deriving-subtree bound/mention
sets for `Function` scopes) and applies all target validation there:
explicit failures reproduce the pre-ledger hard errors (`invalid
chunk_renames spec`, `collides with an existing top-level local`,
`captured by a nested binding`, …), heuristic failures drop silently
(over-suppression OK, capture never), import-induced failures are
internal invariant violations. Cross-priority disagreement resolves
silently by priority; same-priority disagreement stays a hard error.
`$N` minting is a ledger service (`mint` / `seed_taken` / `claim` own
the per-scope taken sets; `import_emit.rs` and `exports.rs` request
mints). The per-module naturalize ledger absorbed the plan-driven
explicit renames, so one seal resolves explicit-vs-heuristic priority,
the module-global target-collision rule (`merge_module_renames`), and
occupancy. Application sites now only `debug_assert!` seal's no-capture
guarantee; what still validates at application (and the remaining seal
points PR 4 collapses) is inventoried in `rename_ledger.rs`'s module-doc
"Seal points" section.

**PR 4 landed (2026-06):** collect → seal → execute-once per ledger.
No pre-seal trial application remains: capture facts reach seal from
the **un-renamed** tree (the read-only `RenameCaptureProbe` over
`RenameLedger::pending_renames_by_name` for the entry body; the derive
clone's candidate walk for module bodies), the post-seal rename pass is
the only mutation of each scope unit's AST, and the hand-maintained
candidate-map mirrors are gone. The export-growth ledger merged into
`lower_chunk`'s chunk ledger (one seal validates `Chunk` +
`EntryPublicExports` together), collapsing five ledger instances to
four. `plan_module_reference_needs`' linear reverse `.find` over the
heuristic rename map is replaced by the sealed map's inverse projection
(`RuntimeImportLookup::original_by_renamed`; injective by seal's
target-collision rules). Documented blockers that stay (see
`rename_ledger.rs` "Seal points"): the naturalizer's derive clone
(enclosing scopes' subtree facts must reflect nested fired renames —
scope-sensitive, not expressible as set transformations of sealed
maps), the per-plan import ledger (collection needs post-naturalize
facts plus entry's grown exports, which need every module's facts —
a cross-module phase cycle), and `cross_module_chunk_renames`' separate
application (it composes _sequentially_ with import-local mints; a
single seal's priority rule would mis-resolve `x → y$1` vs `x → y`).

Decisions taken (formerly the open-questions subsection below):

1. **Same-priority conflicts are hard errors at seal**, naming both
   contributors — silent suppression is the trap the ledger closes.
2. **The `$N`-suffix minting scheme stays as-is** now that
   `disambiguate_import_locals`' minting is a ledger service (PR 3);
   readability is a separate, later naturalizer concern.
3. **Structural-mutation contract: no structural moves between seal and
   execute.** Most moves already happen pre-seal (the materializer seals
   after `ChunkPlan` finalization); the `&Module` type-level barrier for
   non-execute passes lands with the execute-once pass.

Remaining PRs:

- **PR 5 — cleanup + the deep cuts PR 4 documented**: delete the
  defensive era where seal's guarantee dominates (`captured` sets on
  the executors, `drop_subtree_captured_targets` where dominated by
  seal's subtree validation); attack the documented blockers — merge
  the per-plan import ledger by planning references off un-renamed
  body facts plus sealed-map reasoning at import emission (mind the
  mint-seeding suffix-mint corner cases), fold
  `cross_module_chunk_renames` into the sealed pipeline without
  breaking its sequential composition with import-local mints, and key
  the executor by hygiene `Id` end-to-end (deleting the seal-output
  `*_by_name` string projection — requires emitting import/export
  decls under real contexts instead of `new_no_ctxt`). The
  `merged`/`module_scope` split stays as long as free-source aliases
  apply function-locally while their intents live at `Module` scope.

The architecture sketch below remains the reference for PR 5.

The naturalizer / lowerer currently mutates identifiers in place across
several independently-discovered passes and lets every downstream consumer
(import planning, cross-module binding lookup, fact collection,
source-map fragment emission, export tables) read whatever name happens
to exist when _it_ runs. The problematic shape is a rename in one layer
followed by a downstream layer keying off the wrong-era name. Localized
defensive patches on individual consumers leave the same trap waiting
for the next consumer that does not know about the rename.

The proposed architectural fix is a single **collect → validate → execute
(once)** rename pipeline:

- **Collect**: every rename contributor submits _intents_ into a single
  buffer instead of mutating the AST. Contributors include explicit
  spec-specified renames, naturalizer heuristics (return-object alias
  inference, shorthand-collapse readback, future readable-name
  autonaming), import-induced renames (`{ sA as propKeyA }`),
  collision-resolution renames, and chunk-level renames produced by
  factorize. Each intent is `(scope, original_name, new_name, reason,
priority, invariants_it_assumes)`.

- **Validate**: resolve conflicts deterministically before any AST
  mutation. Priority order is explicit > import-induced > heuristic.
  Surface contradictions (`sA → propKeyA` _and_ `sA → propKeyB` in the
  same scope) as hard errors at validation time, not as silent
  last-write-wins behavior at AST mutation time. Output is a stable,
  read-only mapping `(scope, original_name) → final_name` and the
  inverse `(scope, final_name) → original_name`.

- **Execute once**: one pass applies the resolved mapping to the AST
  _and_ updates every fact table (`runtime_imports`, `referenced_idents`,
  export tables, source-map fragments, cross-module binding indexes) in
  lockstep, keyed by the original name. After execute, no later pass
  invents a rename — every consumer that needs to bridge between
  pre-rename and post-rename names consults the same finalized mapping.

Direct consequence: the family of "X-layer renamed it, Y-layer didn't
notice" bugs collapses to one architectural boundary. Existing
defensive reverse-lookups and path sanitizers can become ordinary
mapping/path-building code once the final mapping makes body ASTs,
runtime imports, and report tables agree before planning runs.

Remaining prerequisite work (the scope model landed with PR 1 and all
contributors collect through the ledger as of PR 2 — see
`lowering/rename_ledger.rs`):

1. Inventory every downstream consumer that today reads identifier
   names off the AST or off pre-rename fact maps. Same call sites that
   currently need defensive bridging.

2. Design must not block on landing small defensive fixes. Land them
   case by case as bugs are discovered. Removing redundant defensive
   patches is part of the pipeline-landing cleanup, not the architecture
   work itself.

## CLI scripting surface

Work items on the machine-consumable CLI contract (distinct from the
dogfood findings in <CLI_DOGFOOD.md>):

- **Structured rejection diagnostics for edit-gate failures.**
  `bindings {assign,unassign}` and `modules {merge,delete}` render
  gate rejections as a prose blame report on stderr
  (`cli/edit_gate.rs`) and bail; even with `--format json` there is
  no machine-readable rejection payload on stdout, so agents scrape
  stderr. Emit the blame report as JSON on stdout when a JSON format
  is selected.
- **`--format` parity for the five mutating verbs.**
  `bindings {assign,unassign,rename}` take `--format`;
  `modules {merge,delete}` print only a fixed summary line
  (`cli/module.rs`). Define one outcome schema shared by all five.
- **Edit-gate rejections leave no `cycles.json`.** The pipeline
  writes `cycles.json` on gate rejection, but edit-gate rejections
  (including `--dry-run` probes) write nothing, so
  `debundle gate list`/`gate describe` cannot be pointed at the
  failure that was just reported.
- **`DEBUNDLE_SOURCE_ROOT` double meaning.** The same env var feeds
  `run --tree-source-root` (spec-tree compile root, `pipeline.rs`)
  and the query commands' `--source-root` (upstream snapshot root
  for source-backed selectors). The two roots are different
  directories in real corpora; one of the flags should get its own
  env var.
- **`resolve_id_as_module_path` swallows I/O errors.** The
  `cli/mod.rs` helper applies `.ok()?` to the modules-tree walk, so
  a failing `collect_module_files` silently degrades a module-path
  id into a binding-name guess. Surface the error.
- **`selection_with_*` sentinel hack.** `selection_with_proposal`
  (`cli/mod.rs`) stuffs a sentinel empty-string `binding_id` that
  `run_describe` must remember to clear. Make `SelectionArgs`
  selection an enum (one variant per id kind) so the sentinel
  disappears.

## CLI usability

CLI usability and scripting-safety findings live in <CLI_DOGFOOD.md>.
