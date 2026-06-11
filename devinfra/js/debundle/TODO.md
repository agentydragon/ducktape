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
- **Bare anonymous list-hole aliases.** Anonymous throwaway holes currently
  use the reserved-prefix spelling with a trailing underscore, e.g.
  `STMT_LIST_;` for a statement-list hole. Accept the no-suffix base token
  `STMT_LIST;` as an alias too, because authors naturally read it as
  "anonymous statement list" rather than as a named binding. Keep the existing
  ambiguity behavior: repeated anonymous aliases must match independently, not
  bind a shared statement-list replacement.
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
all live in <perf/proposer.md>.

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

A scope-aware down-payment has landed: the in-place rename visitors
(`IdentifierRenamer`, `RenameAndShorthandNaturalizer` in
`lowering/visitors.rs`) now carry a `RenameScopeStack` and suppress a
rename inside subtrees that re-bind the name, so the "X-layer renamed it,
Y-layer didn't notice" shadowing class is mitigated. The
collect→validate→execute **ledger/intent-buffer** architecture below
remains the open work.

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

Prerequisite work before designing the pipeline:

1. Inventory every current rename contributor in `lowering/` and
   adjacent files. Examples already known:
   `collect_return_object_alias_renames` and
   `collect_naturalization_renames_from_function` in
   `lowering/naturalize.rs`; `RenameAndShorthandNaturalizer` and
   `naturalize_object_literal_shorthand` in `lowering/visitors.rs`;
   `disambiguate_import_locals` in `lowering/util.rs`; the chunk-level
   `chunk_renames` map that flows out of factorize; and any
   collision-resolution code path that mutates `module_import_renames`
   at the orchestration site. Capture each contributor's _kind_
   (explicit / heuristic / collision / chunk-level), _scope_ (function
   / module / chunk / cross-chunk), and _current side-effect surface_.

2. Inventory every downstream consumer that today reads identifier
   names off the AST or off pre-rename fact maps. Same call sites that
   currently need defensive bridging.

3. Decide on the scope model. Per-function naturalizer renames don't
   need to be visible at chunk scope; chunk_renames don't need to
   reach per-function naturalizer collection. The intent buffer should
   reject cross-scope writes by construction.

4. Design must not block on landing small defensive fixes. Land them
   case by case as bugs are discovered. Removing redundant defensive
   patches is part of the pipeline-landing cleanup, not the architecture
   work itself.

Likely multiple PRs.

### RenameLedger (PR2) open questions

Before implementing the pipeline above, pin these three design questions:

1. **Conflict policy for same-priority heuristic disagreements.** When
   two heuristic contributors propose conflicting renames at the same
   priority (e.g. `collect_return_object_alias_renames` says `sA →
propKeyA` and `collect_naturalization_renames_from_function` says
   `sA → propKeyB`), does the ledger panic at seal citing both
   submitters, or silently suppress the lower one? Default proposal:
   panic loudly, with both contributors named in the error — silent
   suppression is the trap PR2 is meant to close.
2. **Disambiguation name minter.** `disambiguate_import_locals` today
   appends `_1`, `_2`, ... suffixes until a free name is found. When
   that becomes a `RenameLedger` method (so the ledger owns "what
   names are taken in this chunk"), does the scheme stay as-is, or
   switch to something more readable (`name_from_module`, etc.)?
   Default proposal: keep `_N` scheme; readability is a separate
   concern best handled by a later naturalizer pass.
3. **Structural mutations during COLLECT.** Today
   `materialize_logical_modules` moves declarations between modules
   in-place during the collect phase. Tighten the contract to either:
   - "no structural moves between seal and execute" (pragmatic — most
     moves already happen pre-seal, and the type-level barrier
     `&Module` in non-execute passes lands cleanly), or
   - "all structural moves pre-COLLECT" (cleaner architecturally but
     requires reordering the materializer to compute a final body
     order before any rename intents are submitted).

## CLI usability

CLI usability and scripting-safety findings live in <CLI_DOGFOOD.md>.
