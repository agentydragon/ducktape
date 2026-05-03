# Debundler Implementation Constraints

> The canonical design — what debundling means as a problem, what
> emit strategies are correct under which conditions, and the
> realizability theorem the validator enforces — lives in
> <DESIGN.md>. Read that first when working on the splitting
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
queue of still-scrambled symbols, etc.) that drive the next wave of
spec edits, and is itself improved as new shapes / bugs / heuristic
opportunities surface.

Three things that should follow from that:

- **Spec authoring is high-value, low-friction.** Build tools,
  side-output analyses, and heuristic generators that make spec
  authoring nice and convenient — fewer clicks, fewer round-trips,
  no peeking into git history. The pipeline emits machine-readable
  artifacts so consuming tools can suggest module decompositions /
  rename targets / chunk seams; the spec author reviews and commits
  decisions explicitly.
- **Heuristics surface signals, the spec makes decisions.** Heuristic
  outputs are recommendations only. They never make extraction or
  rename decisions on their own — the spec language extends to
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

## Working Rule

If a proposed change improves a test result without improving real
correctness, do not make that change. If the easiest fix is not the deepest
correct fix, do the deeper correct fix or stop and explain the blocker.

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
  — substring-match against the emitted source. Useful for shape checks
  ("`class A` appears", "no `__dt_generated_init__` wrapper").
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
- Constructing `JsPipelineArtifact`, `ChunkManifest`, or other intermediate
  pipeline types by hand to drive a stage in isolation, when feeding an
  input fixture through the real pipeline reaches the same code path.

## Native Rust Shapes

Internal pipeline types are pure Rust. Stringly-typed `node_type` fields,
`Vec<serde_json::Value>` payloads with known shape, and `Map<String,
Value>` blobs that mirror an external JSON shape are smells — replace
them with typed structs and enums.

- Use Rust enums (frequently a thin wrapper over an SWC AST variant) over
  stringly-typed kind fields.
- Drop `#[serde(rename_all = "camelCase")]` on types that aren't actually
  serialized to disk or sent over the wire — internal manifests that the
  pipeline orchestrator only reads `kind` from don't need to derive
  `Serialize` at all.
- Replace `Vec<Value>` / `Map<String, Value>` payloads with typed structs
  whenever the shape is known. `serde_json::Value` is appropriate only
  when the value is genuinely polymorphic (spec args, `#[serde(flatten)]`
  extension slots) or comes straight from an external JSON input.

The public spec format consumed by `--spec` and the on-disk artifact
manifests are external contracts — be deliberate about changing them.
Internal intermediate types have no such constraint.

## Path Resolution Contract

The CLI accepts relative or absolute paths for `--spec`, `--package-root
<pkg>=<dir>`, and `--packages-root`. When the binary runs inside a Bazel
runfiles context (`bazel run`, `bb run`, or otherwise with `RUNFILES_DIR` /
`RUNFILES_MANIFEST_FILE` set), each relative path is first resolved through
runfiles via the standard `runfiles` crate; if the resolution points at an
existing file the runfiles path is used, otherwise the path is left as-is for
the caller's filesystem semantics. Outside Bazel the binary behaves as a
plain CLI — runfiles resolution is opt-in by environment, not a build-time
mode.

This lets downstream Bazel targets compose absolute-equivalent paths with
just `$(rlocationpath <label>)` (no shell wrappers, no `$$RUNFILES_DIR`
substitutions) while keeping the binary usable as a standalone tool outside
the Bazel tree.

## Spec-level `inputs`

`load_js_chunks`, `compute_js_asts`, and `normalize_js_chunks` are always-on
startup steps run before the pipeline loop — they are not pipeline operations.
The spec configures them via a top-level `inputs: { inputRoot, jsListPath }`
object. Listing any of these three operations as a pipeline stage is a hard
error.

## Materialize logical-modules `targetDir`

`materialize_logical_modules` accepts an optional `targetDir`. Absent or
empty means "no subdirectory" — lowered files land directly under their
chunk root (`<out_dir>/<chunkId>/<target.path>.js`). A non-empty value adds
that prefix (`<out_dir>/<chunkId>/<targetDir>/<target.path>.js`). Tests that
want the legacy `modules/` layout pass `"targetDir": "modules"` explicitly.
