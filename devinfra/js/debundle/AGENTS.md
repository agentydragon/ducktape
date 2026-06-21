@README.md

# Debundler Implementation Constraints

> The canonical design — what debundling means as a problem, what
> emit strategies are correct under which conditions, and the
> realizability theorem the validator enforces — lives in
> <docs/design.md>. Read that first when working on the splitting
> pipeline. This file documents agent-facing operating principles
> on top of the design.

## Mission

The debundler is a peeling toolkit. Its purpose is to recover a
production-minified JavaScript bundle into something that reads like a
hand-written modular codebase — stable names, real module seams,
narrow public surfaces, residual / generated noise driven down over
time. Each release of an upstream app (props/frontend, private
downstream corpora, etc.) is re-peeled from a versioned spec; the
spec is the source of truth for
which symbols belong where and what they should be called. The
debundler executes the spec, emits side-output analyses (priority
queue of still-unrenamed symbols, etc.) that drive the next wave of
spec edits, and is itself improved as new shapes / bugs / heuristic
opportunities surface.

Three things that should follow from that:

- **Spec authoring is high-value, low-friction.** Build tools,
  side-output analyses, and heuristic generators that make spec
  authoring nice and convenient — fewer clicks, fewer round-trips,
  no peeking into git history. The pipeline emits machine-readable
  artifacts so consuming tools can suggest owner/module splits /
  rename targets / chunk seams; the spec author reviews and commits
  decisions explicitly.
- **Heuristics surface signals, the spec makes decisions.** Heuristic
  outputs are diagnostics and candidate sets only. They never make
  extraction or rename decisions on their own — the spec language extends to
  consume them (e.g. "this React component scope, except symbols
  X and Y") so reviewers can see and adjust what gets applied.
- **The debundler is itself a living target.** When a new bundle
  shape exposes a missing capability, extend the debundler — but
  reproduce the failure with a minimal e2e first (see "Bug-fix
  discipline" below).

## Bug-fix discipline

When a debundler bug surfaces at the e2e / smoke / pipeline layer,
the order is:

1. **Localize the failure** — file path, line numbers, exact shape.
2. **Reproduce with a minimal e2e fixture under `e2e/`** that fails
   for the same reason on synthetic inputs. The e2e flipping green is
   the contract for the fix; without it, the bug is one upstream
   bump away from regressing silently.
3. **Then fix.** Land both the fixture and the fix in the same PR.

### Fixture minimization

A bug-reproducing fixture should be the **smallest** input that still
triggers the bug. After the failure shape is reproduced, strip every
feature that can be removed while the test still fails — extra
bindings, extra modules, extra plan members, longer source bodies,
realistic-flavored names. Use generic placeholder names (`a`, `b`,
`mod_x`, `readable`) over names borrowed from the original
upstream-corpus shape. A reader of a future regression should be
able to point at the fixture and see what specifically triggers the
bug, not have to mentally peel off layers of irrelevant detail.

Don't add fixtures for invariants the materializer already upholds.
If a candidate fixture passes against `origin/devel` before the fix
is applied, it isn't testing the bug — drop it or move it into a
separate PR documenting the invariant.

If a bug genuinely cannot be reproduced synthetically (e.g. it depends
on a specific upstream-bundle quirk that's hard to mimic), say so
explicitly in the PR body and add coverage at the next-coarsest level
(props/frontend smoke, dedicated corpus fixture, etc.).

Pipeline-level bugs that surface only against a private downstream
corpus and have no obvious synthetic reproducer should also be
reproduced against **props/frontend's debundle pipeline** before
fixing — that smoke is the open-source proxy for a real
React/Svelte chunk-split vendored production bundle and lets a
regression test land in public CI.

## Verification corpora

Two debundle corpora live in this repo:

- **Synthetic e2e fixtures** (`e2e/`) — focused, fast, exercise one
  pipeline stage / bug class. Most tests live here.
- **`props/frontend/debundle/`** — realistic-shape corpus (Svelte 5,
  esbuild splitting, real prod npm vendor packages). Use as the
  verification step _before_ claiming a fix applies to a full
  private downstream bundle. Failures here surface chunk-graph /
  vendor-shape / rename-pipeline issues without needing access to
  a private corpus.

When a fix lands, the order is: synthetic e2e green → props/frontend
debundle green → private-corpus smoke green. Skipping the
props/frontend layer means a regression that survives synthetic
tests will only show up against the private corpus, where iteration
is slower and the repro can't be shared publicly.

## Performance Profiling

Use external profiling before adding fine-grained timing boilerplate
to production code. The default downstream workflow is documented in
<README.md>: use the profile sibling targets from
`debundle_pipeline_with_profiles` so the profiling run shares the real
Bazel action's spec paths, package roots, working directory, declared
inputs, and debundler binary.

Profile-mode samples answer "where is the runtime going relative to
total time?" They are the right tool for discovering hot stack
patterns, accidental repeated full-graph walks, map-heavy inner loops,
avoidable clones, missing indexes, bad data structures, and algorithmic
shape problems. Many fixes should be structural: change maps to dense
typed-id vectors, cache instead of recomputing, use a better graph
algorithm, fuse passes, or adopt a proven crate/data structure rather
than sprinkling more counters through the code.

Use production builds for absolute elapsed-time numbers. Symbol-heavy
profiling flags can trade exact wall-clock comparability for better
stack samples. Keep production telemetry coarse and useful for users of
the pipeline; don't add detailed stage fields solely because an
optimization investigation needs temporary visibility.

## Test shape preferences

Strongly prefer **executable e2e tests with high-level assertions**
over implementation-specific unit tests:

- "module `foo/bar.js` exports `abc` and not `xyz`" (parse the emitted
  module; check the export set)
- "running the emitted entry under Node prints `expected output`"
- "module body parses and binds `readable` at most once per scope"

These assertions test the debundler's external contract — what
downstream consumers see — and survive internal refactors. They also
build up a reusable library of fixture cases with very similar shape
(the `e2e/support.rs` helpers exist for exactly this), so adding the
N+1th fixture is cheap.

Reach for unit-tests-on-internals only for combinatorial corner cases
of pure functions (path resolution, name-disambiguation logic, etc.)
where exposing the input/output via the CLI would be awkward. The
"Testing Philosophy" and "Forbidden test shapes" sections below are
the long-form rules.

## Worktree discipline for parallel agents

When multiple worker agents may simultaneously edit code in this
repo (or any other Bazel-managed repo here), **each agent must work
in its own git worktree.** The Agent tool's `isolation: "worktree"`
parameter does this automatically; without it, agents share the
single working tree at `/home/user/ducktape` and stomp on each
other's uncommitted changes when one agent's `git checkout` happens
between another agent's edits and commit.

Symptoms of missed isolation: working-tree changes that span
multiple agents' files at once, agents reporting "my work
disappeared", `git status` showing files belonging to a branch that
isn't checked out. If you see these, bail out — re-dispatch the
affected agents with `isolation: "worktree"` rather than trying to
recover from contamination.

The orchestrator agent (the one dispatching workers) keeps the main
worktree for itself and never disturbs in-flight worker working
trees.

### Signing in worktrees

The session's signing tool (`/tmp/code-sign`, invoked as
`gpg.ssh.program` via `~/.gitconfig`) succeeds only when its current
working directory is the canonical repo path (`/home/user/ducktape`).
From any other path — `/tmp/foo`, `/home/user/wt-foo`, or any other
worktree — the signing server returns
`status 400: missing source` and the commit fails. The "source" is
inferred by the server from the cwd context, not from the file path
or git config.

Workaround for parallel worker agents: each worker uses its own
worktree to avoid working-tree contention, but commits with
`--no-gpg-sign` (and pushes unsigned) since the worktree path can't
satisfy the signing tool. Authorized exception to the repo-wide
"never bypass signing" rule, narrowly scoped to ephemeral worker
worktrees that exist only to push a feature branch. Squash/merge on
the GitHub side signs the resulting commit on `devel`, so the
commit history on `devel` stays signed.

The orchestrator agent's commits (in `/home/user/ducktape`) keep
the default signing path.

## AST Requirement

JavaScript-source transformations must use proper AST operations on the
SWC-parsed input. Do not use raw text rewrites, string scanning, regex
rewriting, ad hoc source patching, or other text-based mutation as a
substitute for AST transformations.

Syntax-derived facts likewise come from the SWC-backed parse/analysis
path and flow through artifact manifests / indexes — not from
phase-local rescans. The pipeline parses each chunk once in
`prepare_js_chunks` and records imports, exports, dynamic-import
shape, etc. on `ChunkManifest`; downstream consumers (vendor planning,
emission-time specifier rewriting, owner-graph construction) consume
those manifest facts through the centralized `ArtifactIndexes` query API
(`resolve_runtime_import_reference`, `manifest_imports_targeting_chunk`,
etc.) rather than re-walking the AST to answer "which chunk does this
import resolve to" or "which chunks import this binding". Mutation
passes still need an AST visitor to apply the rewrite — that's
unavoidable — but the resolution decision is fed in from the
manifest, not rebuilt per stage. If a new pipeline question can't
be answered by the existing manifest schema, extend the shallow
analysis once instead of discovering the same fact again in another
stage.

## Working Rule

If a proposed change improves a test result without improving real
correctness, do not make that change. If the easiest fix is not the deepest
correct fix, do the deeper correct fix or stop and explain the blocker.

## Soundness over completeness

The debundler's contract: **any spec the validator accepts must
emit a debundled bundle that runs correctly.** Over-restriction
(rejecting a spec that would have produced a working output) is
an acceptable failure mode — the user can rework the spec or we
can loosen the validator. Under-restriction (accepting a spec
that produces a broken bundle) is a soundness violation and is
not acceptable.

This rule applies to every static analysis the validator runs —
purity classification, side-effect graph construction, top-level-
await detection, dependency-graph cycles, etc.

### Conditionally-correct optimizations

Soundness does **not** require every inference to hold for arbitrary JS.
An inference may be **conditionally correct** — sound only when the
input satisfies a checkable precondition — so long as the implementation:

1. Checks the precondition on the specific statements / chunks it would
   fire on, and
2. Falls back to the strictly-conservative path (the one already known
   sound) when the precondition fails.

Example: dataflow-aware S-chain emission (`graph.rs`). Per-statement
write/read summaries assume the statement contains no `with`, no direct
`eval`, no computed-key `globalThis` access, no `Function`-constructor,
etc. — constructs that would invalidate static reasoning about which
cells the statement touches. Each impure statement carries a
`dataflow_summarizable` bit; statements that fail the check fall back
to the unconditional adjacent-impure S-edge.

This is deliberate: the real input (the real-corpus bundle in
`gaffer-private`, props/frontend, etc.) is well-behaved and admits
precise reasoning even though generic JS does not. Document each such
optimization with (a) the precondition it requires, (b) where the
check lives, and (c) the fallback path.

### Pure-call whitelist soundness

The purity classifier's `PURE_STATIC_PROPS`, `PURE_STATIC_CALLS`,
and `PURE_GLOBAL_CALLS` whitelists in
<purity/whitelists.rs> directly drive S-edge construction. An
over-approximating entry — a built-in flagged Pure that can in
fact fire user code on some argument — drops S edges the
realizability theorem needs, and can let a cyclic spec slip past
the validator and emit a bundle whose runtime ordering breaks.

**Admission rule for new entries:**

- The operation must fire **no user-defined code on any argument
  type**. That means: no `ToNumber` / `ToString` / `ToPrimitive`
  / `ToPropertyKey` coercion of the args (these can fire
  `valueOf` / `toString` / `[Symbol.toPrimitive]`); no iterator
  protocol (`[Symbol.iterator]`); no proxy traps; no own-property
  `[[Get]]` / `[[Set]]` (which fire user accessors); no mutation
  of any reachable object; no `toJSON` callback path.
- Cite the ECMA-262 clause that establishes this. "Common in
  production bundles" is not a soundness argument.
- "Often called with a fresh-literal target / primitive args in
  practice" is also not a soundness argument — the validator does
  not infer types and cannot rely on argument shape unless it is
  enforced syntactically at the call site (e.g., a future
  primitive-only purity bit applied to arguments).

**If the whitelist is too restrictive in practice:** that is the
expected failure mode. Loosen it only in safe ways — by adding
operations that are universally pure (no user-callback path on
any arg), or by introducing stronger argument analysis (e.g., a
"statically-primitive" classification) that lets a wider set of
operations be admitted _under syntactic gates_ on the arguments.
Never add an entry "because the common case is fine."

The analysis engine carries an inline TODO listing patterns
that would become admissible once a primitive-arg gate exists —
treat that as the queue for safe whitelist growth.

All whitelist admission arguments additionally assume **intrinsic
integrity** (docs/design.md A11): the chunk runs with unmodified
built-in prototypes. Prototype pollution (e.g. replacing
`Set.prototype.add` or `Array.prototype[Symbol.iterator]`) defeats
every whitelist entry whose ECMA-262 citation describes the
standard built-in; the analyzer cannot detect it because the
pollution may live outside the analyzed chunk.

### Declared purity

The spec format admits optional per-member `purity:` annotations.
When `purity: "pure"` is present, the validator treats calls to
the bound Ident as `Pure` regardless of the function body's
contents. When `purity: "pure_new"` is present, the validator
treats `new BoundIdent(...)` as `Pure` if every constructor
argument is pure. These are **author trust contracts**: the
validator does not re-verify the body or constructor. An incorrect
annotation can produce a buggy debundle the same way an incorrect
spec selector can — soundness shifts to the spec author.

Use the annotation when:

- The body is too dynamic for static inference (dynamic dispatch
  through a registry, dynamic property access).
- The body calls into vendor code outside the chunk's analyzable
  scope.
- The function is opaque by construction (a host-provided callback,
  an FFI shim) but is known by the author to have no observable
  side effects.

The `pure` annotation overrides A8's shadowing fallback: a chunk that
imports `Boolean` from userland AND declares the local `Boolean`
binding pure in the spec uses the declared-pure path (the author
asserts THIS bound value is pure, regardless of where it came
from). Args to the call are still evaluated normally — declared
purity covers the function value, not its arguments.

`pure_new` is separate from `pure`: it covers only `new X(...)`,
does not make `X(...)` pure, and still evaluates constructor
arguments normally.

`pure_members: [<prop>, …]` is a third trust contract on the same
member, extending the contract from "calls of the bound Ident are
pure" to "calls of `<binding>.<prop>(args)` for each listed
property are pure when args evaluate pure". The intended shape is
a vendor namespace binding (a star-import or renamed binding
standing in for a vendor module — React, etc.) whose member
function values are author-known to be side-effect-free even
though the analyzer cannot inspect them.

```yaml
- name: React
  selector:
    binding: { name: b, kind: import_specifier }
  pure_members: [forwardRef, lazy, memo, createContext]
```

Admission rule: only static identifier-property access fires the
rule (`<binding>.<prop>(args)` / `<binding>?.<prop>(args)`).
Computed access (`<binding>[expr](args)`), chained access
(`<binding>.x.y(args)`), private fields, and non-Ident receivers
fall back to the regular classifier path. As with `purity: pure`,
the rule wins over the global-shadowing fallback — the spec
author asserts THIS bound value is pure regardless of where it
came from. Args are still classified independently.

The annotation is **per-member**: a sibling member without the
annotation stays subject to inferred classification. There is no
"declare a whole module pure" shorthand.

### Function-ref reads vs calls

The `PURE_STATIC_FUNCTION_REFS` table whitelists static-property
reads of function-valued built-ins (e.g.
`const define = Object.defineProperty;`). Reading such a property
fires no getter per ECMA-262, so the read itself is pure. The
**call** of the resolved value is still subject to the
`PURE_STATIC_CALLS` admission contract above — call shapes that
mutate, fire iterators, fire user getters, or invoke proxy traps
must NOT be added to `PURE_STATIC_CALLS` regardless of whether
the read appears in `PURE_STATIC_FUNCTION_REFS`.

Every entry added to `PURE_STATIC_FUNCTION_REFS` MUST land with
both:

- a positive `static_function_ref_*_alias_is_pure` test
  (`Receiver.method` classifies as `Pure`), and
- a negative `static_function_ref_*_calls_remain_unknown` test
  (`Receiver.method(args)` classifies as `Unknown`).

The two-direction pinning prevents a future maintainer from
misreading the read-pure entry as call-pure and incorrectly
promoting the entry into `PURE_STATIC_CALLS`.

### Argument-shape-gated whitelists

`PURE_OBJECT_CALLS_ON_PLAIN_DATA` is a separate table that admits
`Object.{keys, values, entries, freeze, fromEntries}` as Pure
**only when the argument is syntactically a fresh plain-data
literal** (an `Expr::Object` with `is_plain_data_prop`-passing
props, an `Expr::Array`, or — for the non-`fromEntries` members
— a chunk-top binding registered as `ChunkBinding::PlainData`).
The general-arg form of these calls stays in
`PURE_STATIC_FUNCTION_REFS` (read-pure, call-unknown), matching
the soundness rule that we cannot admit `[[Get]]` /
descriptor-mutation on an arbitrary receiver.

New entries to `PURE_OBJECT_CALLS_ON_PLAIN_DATA` need the same
per-entry ECMA-262 citation as `PURE_STATIC_CALLS`, plus paired
positive and negative tests in `purity/classifier_tests.rs`:

- a positive `object_<prop>_on_plain_<shape>_classifies_pure`
  test (`Object.<prop>({lit})` is Pure), and
- a negative `object_<prop>_on_<non-plain>_stays_unknown` test
  (`Object.<prop>(somefn())` is Unknown, opaque-binding form is
  Unknown).

Cycle-breaking behaviour is pinned in
`devinfra/js/debundle/e2e/object_plain_data_calls_test.rs`.

## Testing Philosophy

**Default to end-to-end tests that drive the real pipeline.** A debundler
bug almost always manifests as something an outside observer can see — the
emitted JS is not executable, an expected exported symbol is missing, the
extracted module shape is wrong, runtime behavior differs from the input
bundle. Reach for the e2e harness in `e2e/` (the `debundle` CLI invoked
through `support.rs`) before reaching into internals.

This applies even when you're chasing a bug or feature that lives in one
internal stage. If the symptom is observable in the unbundled output —
exported symbol set, runtime behavior, file layout, source shape — write
the regression as an e2e test driving the real CLI. Do not mock the
pipeline and do not construct internal types directly when an input
fixture reaches the same code path.

### Assertion helpers

`e2e/support.rs` provides primitives like:

- `assert_module_exports(out_root, "foo/bar.js", &["abc"], &[])` — assert
  module exports include `abc` and exclude listed names.
- `assert_module_source(out_root, "foo/bar.js", &["needle"], &["antineedle"])`
  — substring-match against the emitted source. Useful for emit-shape
  checks (e.g., `&["const A = f()"]` to pin the inline-init shape, or
  `&["import { foo }"]` to pin a cross-module re-import).
- `assert_entry_output(fixture, "expected stdout\n")` — runs the entire
  emitted tree under node and asserts the bundle's runtime behavior is
  preserved.
- `assert_generated_module_after_entry_script(...)` — runs a probe script
  after the emitted entry has executed; useful for asserting on lazy-loaded
  modules' runtime shape.

Add new helpers when you find yourself repeating an assertion shape across
tests. A test that says "this file exports `abc` as a function and that
file does not, and the whole tree still runs and prints `X`" should be one
or two helper calls, not a wall of `fs::read` and substring matches.

### When unit tests are appropriate

A small number of focused unit tests reaching into a specific module are
fine — combinatorial corner cases for a pure function (e.g. path
resolution), or invariants that are awkward to expose via the CLI.
**They should be the minority.** If a behavior or bug can be demonstrated
by checking the shape of debundled output, the test belongs in `e2e/`.

### Forbidden test shapes

- Mocking the pipeline or any of its stages.
- Constructing intermediate pipeline artifact or manifest types by hand to
  drive a stage in isolation, when feeding an input fixture through the real
  pipeline reaches the same code path.

## Native Rust Shapes

Internal pipeline types are pure Rust. Stringly-typed `node_type` fields,
`Vec<serde_json::Value>` payloads with known shape, and `Map<String,
Value>` blobs that mirror an external JSON shape are smells — replace
them with typed structs and enums.

- Use Rust enums (frequently a thin wrapper over an SWC AST variant) over
  stringly-typed discriminator fields. Debundler-owned serialized enums use
  `#[serde(rename_all = "snake_case")]` unless an external contract already
  requires a different spelling.
- Keep known artifact state typed. File roles are `FileRole`; module
  extraction metadata is `ModuleExtractionState`; do not reintroduce role
  strings or ad hoc metadata maps for known shapes.
- Replace `Vec<Value>` / `Map<String, Value>` payloads with typed structs
  whenever the shape is known. `serde_json::Value` is appropriate only
  when the value is genuinely polymorphic (spec args, `#[serde(flatten)]`
  extension slots) or comes straight from an external JSON input.
- Debundler-owned JSON does not get compatibility envelopes by default. Do
  not add top-level `kind`, `schema_version`, `operations`, or stage-list
  fields to flat transform specs or other owned JSON unless a real external
  consumer needs them.
- Flat transform specs contain inputs and author decisions only. Keep analysis
  provenance and notes (`evidence`, `notes`, `confidence`, `export_shape`)
  out of `VendorMark`; put that context in docs or diagnostic side outputs.

The public spec format consumed by `--spec` is a reviewed contract, but it is
still debundler-owned. Prefer clean typed shape over compatibility fields kept
only for older in-repo fixtures.

## Spec `note:` field — STYLE.md exemption

**Deviation** from STYLE.md ("every field needs a reader"; authoring provenance
belongs in inert `#` comments, not `note:` schema fields): the spec's optional
`note:` field on `Member` / `BindingGroup` / `AnonymousStatementSelector`
(<spec.rs>) is a ratified exemption. The spec rewriters (`bindings assign`,
`synthesize --apply`) re-emit the YAML and **drop `#` comments**, so a
round-tripped `note:` is the only debt-rationale annotation that survives a
rewrite. Unlike `comment:` (which emits a `//` line into the debundled JS and is
gated by byte-identity), `note:` is non-emitting; its reader is the human spec
author.

## Module-specifier path math

The CLI accepts relative or absolute paths for `--spec`, `--package-root
<pkg>=<dir>`, and `--packages-root`; callers pass already-resolved paths
(e.g. `$(rlocationpath <label>)` from a Bazel target). For module-specifier
path math, use existing path crates and local helpers (`relative-path`,
artifact path helpers, etc.) rather than hand-rolled POSIX segment splitting
or string replacement. If a helper is missing, add one in the path module and
keep call sites typed.

## Spec-level `inputs`

`load_js_chunks` and `prepare_js_chunks` are always-on preparation steps.
The spec configures the load step via `inputs: { input_root, js_list_path }`;
chunk preparation parses selected chunks, computes shallow program facts, and
canonicalizes chunk entries in one parallel per-chunk pass. Import-specifier
canonicalization is applied at emission time by the unified pass-through
directive rewriter (`vendor/passthrough.rs`), not as a separate stage.

## Spec-level declarative sections

The flat transform spec carries declarative top-level data maps, not an operation
or pipeline list. Transform code reads these maps directly via typed serde
structs in <spec.rs>.

- `vendor` — keyed by chunk path (`"static/lib.js"` → `VendorMark`). The
  `level` discriminator selects between `suppress` / `boundary_rename` /
  `swap`; only `swap` requires `package`/`version`/`subpath` (parse-time
  guarantee via the `VendorLevel::Swap` enum variant). It is executable
  configuration only, not a place for notes, evidence, confidence scores, or
  export-shape analysis.
- `logical_modules` — keyed by chunk id, then target path (`"static/app"` →
  `"foo/bar/baz.js"` → `LogicalModule`). Two-level nesting makes
  `(chunk_id, target_path)` uniqueness a parser property.
- `unassigned_mode` — keyed by chunk id (`"static/app"` → `UnassignedMode`).
  Per-chunk policy for top-level statements no `logical_modules` entry
  explicitly claims: `inline_in_entry` (default — keep in entry),
  `catchall_file { target }` (emit to a separate logical module at `target`,
  defaulting to `residual/unhandled`), or `mini_factors` (one synthetic
  logical module per atomic factor unit).
- `chunk_renames` — keyed by chunk id, then binding name. These are in-place
  readability renames for bindings that remain in the chunk entry.

Transforms run in a fixed canonical order. Vendor planning and module
materialization are gated by their data maps; tree and harness emission are
gated by their output config fields. The emission rewrite pass (specifier
canonicalization plus vendor consumer surgery) is always-on.

## Materialize logical-modules `target_dir`

`materialize_logical_modules` accepts an optional `target_dir`. Absent or
empty means "no subdirectory" — lowered files land directly under their
chunk root (`<out_dir>/<chunk_id>/<target.path>.js`). A non-empty value adds
that prefix (`<out_dir>/<chunk_id>/<target_dir>/<target.path>.js`). Tests that
want the legacy `modules/` layout pass `"target_dir": "modules"` explicitly.
