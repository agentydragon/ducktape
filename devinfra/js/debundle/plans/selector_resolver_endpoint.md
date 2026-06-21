# Selector Resolver — Execution Runbook to the Endpoint

Companion to the design narrative <selector_constraint_model.md> and the debt
worklist <../debug/2026_06_19_p4_debt_worklist.md>. This file is the **execution
contract**: the ordered phases, the gates that authorize each irreversible step,
the abort bar, and the decisions pre-made so execution does not stall.

## Endpoint (definition of done)

Drive the spec's name-pin debt to **zero**: every selector pinned by genuine stable
identity (a relational edge or a structural signature), the debundle output
byte-identical throughout. The fact-based `ChunkResolver` and the X1–X3 relational
primitives are in place; the remaining endpoint work is the real-spec **conversions**
(delegators → `cross_ref`, codegen helpers → `reads_member`, empty-classes →
`member_of_module`) ∥ push-to-zero, then X4 (counting/uniqueness) and X5 (one global
solve). **Invariant (non-negotiable): fail-closed** — a selector that cannot resolve
categorically errors, never guesses; no special-case hacks, no silent fallbacks.

## Next

Re-measure `selector-debt`; fan the Phase A stabilization lanes wide across the ~162
depth-2 families (name-pins → structural `source_match` / relational selectors, output
byte-identical); apply the X-primitives to their clusters (per
<../debug/2026_06_19_p4_debt_worklist.md>) ∥ push-to-zero escalation; then the X4/X5
global-solve capstone. Terminate at name-only = 0 (or committed faithful-encoding
dead-ends). Real-spec conversions land in gaffer-private (PR #366 + per-family lanes).

## Recipe (degraded web session — verified flag forms)

- **Build/test on RBE** with `bazelisk` + the system-java truststore and the RBE key.
  `source devinfra/secrets/web_env.sh` (sets `BUILDBUDDY_API_KEY`), then
  `bazelisk … --config=rbe --remote_header=x-buildbuddy-api-key=$BUILDBUDDY_API_KEY
--shell_executable=/bin/bash`. **No `--platforms=`** (strips RBE container identity ⇒
  `PERMISSION_DENIED: Container identity unknown`). `dangerouslyDisableSandbox: true`.
- **Query/convert** (selector-debt, synthesize-selectors, match-selector) via
  `gaffer//tana/re:debundle_cli`; **all bazel flags before `--`** (for `run … -- …`).
- **Byte-identical gate:** `bazelisk test
//tana/re/web/78d928dca7:regen_js_test --config=rbe …` (from gaffer) → `PASSED`.
- **Test a NEW ducktape primitive against the real spec** without a repin:
  `--config=source-debundler --override_module=ducktape=/home/user/ducktape` on the
  gaffer build (see <gaffer//tana/re/web/AGENTS.md>). The full lane recipe lives in
  <../debug/2026_06_20_gaffer_phaseA_lane_recipe.md>.

## Operating rules (every step)

- **Branch** `claude/lucid-mendel-178j6q` (both repos). Commit footers: the
  `Co-Authored-By` + `Claude-Session` trailers. **Never open a PR** unless asked.
  **No model identifier** in any committed artifact.
- **Commits** via `nix develop --command git commit -F <msgfile>`; rustfmt + prettier
  hooks may reformat → re-stage and re-commit (or pre-run `rustfmt --edition 2024`).
- Each step ends in a **verified** (build + test green, lint on) commit + push.

## Verification gates

- **Build/test gate**: build the changed lib **and its consumers** (proves match
  exhaustiveness); run the changed area's tests `--cache_test_results=no`, lint on (no
  `--config=nolint` on a step's final run).
- **Conversion gate (X)**: after converting a real selector from a name-pin to a
  relational selector, the spec's debundle **generated output is byte-identical** and
  the converted selector resolves to the **same binding** the name-pin did. (Cross-ref
  selectors have no hand-rolled twin, so a differential does not apply to them.)

## Abort & escalation bar (the goal's central directive)

- If a selector kind or the relational model **will not admit one general faithful
  encoding** — without a special-case hack or a silent fallback — **STOP**. Write the
  dead-end analysis into a `debug/` note (what was attempted, why it fails, what the
  model would need), commit it, and check in with the user. **Do not hack toward the
  endpoint.** An honest dead end beats a resolver we can't trust.
- **Check in (do not self-resolve)** on: a genuine design fork with no principled
  default; a parity regression whose faithful fix isn't clear; a faithful-encoding
  dead end. Everything else: proceed on best engineering judgment.
- Fail-closed is non-negotiable: a selector that cannot be resolved categorically must
  **error**, never guess.

## Pre-made decisions (so execution does not stall)

- **`@Name` anchor map**: prefer the owner graph's `export_name` as the
  readable→binding handle. It is **not** populated at member-resolution time, so build
  the anchor map from the **already-resolved members** (anchor-first order). Both paths
  use the proven kernel primitives (`owner_for_export` / `referencer_of_kind` /
  `alias_owner_for` / `binding_for_owner`).
- **`Resolution` in-pipeline**: `selector_solve::solve` consumes the lean `OwnerGraph`;
  in-pipeline, project the in-memory `analysis::OwnerGraph` into the lean struct — **do
  not** couple `selector_solve` to the `analysis` crate's rich types.

## Remaining phases

The X1 `cross_ref` / X2 `reads_member` / X3 `member_of_module` primitives are in place
(kernel `selector_solve.rs`, facts `chunk_facts.rs`, surface `spec.rs`, wiring
`lowering/materialize/*`); applying them to the real clusters is the conversion work in
<../debug/2026_06_19_p4_debt_worklist.md>. The phases below are not yet started.

### Phase X4 — counting / uniqueness (P4 step 4)

`all_different` for duplicate-claim diagnostics; per-target categoricity as a
constraint rather than a post-hoc check. Folds into X5.

### Phase X5 — one global solve (P4 step 5)

Shift from per-selector solves to a single CSP over the whole spec: shared logic
variables for `@Name`, `all_different` across targets. The capstone — X1–X4 fold in.
Largest architectural step; hold to the abort bar if the global encoding won't stay
faithful.

## Progress ledger

Append a row per verified commit for the remaining work (X-conversions, X4, X5). The
F + X1–X3 build-out is complete and squashed into the debundle PR; its blow-by-blow
walkthrough and pre-squash commit hashes are retired (the durable record is the code,
the PR, and the debt worklist).

| phase | step | commit | gate result |
| ----- | ---- | ------ | ----------- |
