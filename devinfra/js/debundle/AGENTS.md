@README.md

# Debundler Implementation Constraints

> The canonical design — what debundling means as a problem, what
> emit strategies are correct under which conditions, and the
> realizability theorem the validator enforces — lives in
> <docs/design.md>. Read that first when working on the splitting
> pipeline. This file documents agent-facing operating principles
> on top of the design.

## Mission

The debundler is a peeling toolkit: it recovers a production-minified JavaScript
bundle into something that reads like a hand-written modular codebase. Each release
of an upstream app is re-peeled from a versioned spec; **the spec is the source of
truth** for which symbols belong where and what they are called. Consequences:

- **Spec authoring is high-value, low-friction** — build tools and side-output
  analyses that make authoring convenient; the pipeline emits machine-readable
  artifacts so tools can suggest owner/module splits and rename targets.
- **Heuristics surface signals, the spec makes decisions.** Heuristic outputs are
  diagnostics and candidate sets only; the spec author reviews and commits.
- **The debundler is itself a living target** — when a new bundle shape exposes a
  missing capability, extend it, reproducing the failure minimally first.

## Bug-fix discipline

When a debundler bug surfaces at the e2e / smoke / pipeline layer:

1. **Localize the failure** — file path, line numbers, exact shape.
2. **Reproduce with a minimal e2e fixture under `e2e/`** that fails for the same
   reason on synthetic inputs. The e2e flipping green is the contract for the fix.
3. **Then fix.** Land fixture and fix in the same PR.

### Fixture minimization

A bug-reproducing fixture is the **smallest** input that still triggers the bug:
strip every removable feature and use generic placeholder names (`a`, `mod_x`,
`readable`) over upstream-flavored ones. A fixture that passes against
`origin/devel` before the fix isn't testing the bug — drop it or move it to a
separate PR documenting the invariant. Keep each fixture focused on one pipeline
stage or bug class. Bugs first seen against a private corpus are minimized to
synthetic e2e inputs so the regression test lands in public CI; if one genuinely
can't be reproduced synthetically, say so in the PR body, add coverage at the
next-coarsest level, and smoke-test that corpus.

## Performance Profiling

Use the `debundle_pipeline` profile sibling targets
(<docs/bazel_integration.md> § Profiling) so the run shares the real Bazel action's
spec paths and inputs. In `perf` captures, read `perf_record_stderr.txt` for
progress markers, `perf_report_flat_symbols.txt` for self-cost, and the symbolized
children report for callgraphs. For timed repros, stop the
process on timeout and attach `gdb` to inspect live stacks; core-dump only when the
state must outlive the process. Keep production telemetry coarse.

## Worktree discipline for parallel agents

Dispatch parallel workers with the Agent tool's `isolation: "worktree"`. Symptoms
of missed isolation: changes spanning multiple agents' files, "my work
disappeared", `git status` showing another branch's files — bail out and
re-dispatch with isolation rather than recovering from contamination. The
orchestrator keeps the main worktree and never disturbs in-flight worker trees.

### Signing in worktrees

The session's signing tool (`/tmp/code-sign` via `gpg.ssh.program`) succeeds only
when cwd is the canonical repo path (`/home/user/ducktape`); from any worktree it
fails with `status 400: missing source`. Ratified exception to "never bypass
signing", narrowly scoped: ephemeral worker worktrees commit with `--no-gpg-sign`
and push unsigned — squash/merge signs the resulting commit on `devel`. The
orchestrator's commits in the canonical path keep the default signing.

## AST Requirement

JavaScript-source transformations use proper AST operations on the SWC-parsed
input — never raw text rewrites, regex rewriting, or ad hoc source patching.

Syntax-derived facts come from the one SWC parse in `prepare_js_chunks` and flow
through `ChunkManifest` / the `ArtifactIndexes` query API — not phase-local
rescans. Mutation passes still need an AST visitor to apply a rewrite, but the
resolution decision is fed from the manifest. If a new pipeline question can't be
answered by the existing manifest schema, extend the shallow analysis once instead
of re-deriving the fact in another stage.

## Selector resolution

Read <docs/selector_resolution.md> before touching `selector_resolve.rs`,
`source_match/`, `selector_ir_lowering` or `selector_constraint_model_builder`.
A command never decides exactly-one itself; it calls `selector_resolve`. Its
§ "Rejected: tree matching as solver constraints" is a measured dead end: do not
reopen it without a new measurement.

## Soundness over completeness

Any spec the validator accepts must emit a bundle that runs correctly.
Over-restriction is an acceptable failure mode; under-restriction is a soundness
violation. When an analysis is only conditionally correct, check the precondition
and fall back to the conservative path — never widen a rule because the common case
is fine. If the easiest fix is not the deepest correct fix, do the deeper fix or
stop and explain the blocker.

The contract and theorem: <docs/design.md> § "Soundness over completeness".
Admission rules for the purity whitelists and the spec's declared-purity trust
contracts: <docs/purity_soundness.md> — read it before touching
<purity/whitelists.rs> or adding a purity annotation to a spec.

## Testing Philosophy

**Default to end-to-end tests that drive the real pipeline** through the `debundle`
CLI (`e2e/` + `support.rs`) — a debundler bug almost always manifests in the
emitted output: exports, runtime behavior, file layout, source shape. This applies
even when the bug lives in one internal stage. Assertions state the external
contract ("module `foo/bar.js` exports `abc` and not `xyz`", "the emitted entry
prints `expected output` under Node") and survive internal refactors.

`e2e/support.rs` holds the assertion primitives (`assert_module_exports`,
`assert_module_source`, `assert_entry_output`,
`assert_generated_module_after_entry_script`, …) — add a helper when you repeat an
assertion shape; a test should be one or two helper calls, not a wall of
`fs::read` and substring matches.

Unit tests reaching into a module are the minority: combinatorial corner cases of
pure functions, or invariants awkward to expose via the CLI.

### Forbidden test shapes

- Mocking the pipeline or any of its stages.
- Hand-constructing intermediate artifact/manifest types to drive a stage in
  isolation when an input fixture through the real pipeline reaches the same code
  path.

## Native Rust Shapes

Internal pipeline types are pure Rust: typed structs and enums, not stringly-typed
discriminators or `Map<String, Value>` blobs mirroring external JSON. File roles
are `FileRole`; module extraction metadata is `ModuleExtractionState`.
Debundler-owned serialized enums use `#[serde(rename_all = "snake_case")]` unless
an external contract requires otherwise. `serde_json::Value` only for genuinely
polymorphic values (spec args, `#[serde(flatten)]` slots) or raw external input.
Debundler-owned JSON gets no compatibility envelopes (`kind`, `schema_version`,
stage lists) unless a real external consumer needs them; flat transform specs carry
inputs and author decisions only — analysis provenance (`evidence`, `confidence`,
`export_shape`) goes in docs or diagnostic side outputs, not `VendorMark`.

## Spec `note:` field — STYLE.md exemption

**Deviation** from STYLE.md ("every field needs a reader"; provenance belongs in
`#` comments): the spec's `note:` fields are a ratified exemption, because the
rewriters drop `#` comments (<README.md> → "Comments").

## Spec structure

The flat transform spec carries declarative top-level data maps (`inputs`,
`vendor`, `logical_modules`, `unassigned_mode`, `chunk_renames`), read via typed
serde structs in <spec.rs>; transforms run in a fixed canonical order. Field
semantics and authoring workflow: <docs/spec_editing.md>. The CLI accepts relative
or absolute paths for `--spec` / package roots; for module-specifier path math use
the path crates and helpers (`relative-path`, artifact path helpers), never
hand-rolled segment splitting.
