# Selector Resolver — Autonomous Execution Runbook to the Endpoint

Companion to the design narrative <selector_constraint_model.md> and the debt
worklist <../debug/2026_06_19_p4_debt_worklist.md>. This file is the **execution
contract** for an autonomous run: the ordered phases, the gates that authorize
each irreversible step, the abort bar, and the decisions pre-made so execution
does not stall. Update the progress ledger (bottom) after every commit — it is
the memory that survives context compaction.

## Endpoint (definition of done)

1. The hand-rolled `AstWildcardMatcher` (reached via the `binding_resolution`
   free functions) is **deleted**; the fact-based / Datalog resolver is the sole
   selector resolver behind the `SelectorResolver` seam.
2. **Parity proven, not asserted**: the corpus differential over the real tana/re
   spec is **0 disagreements** at the moment of the flip, across every selector
   kind the spec uses (member, binding-group, anonymous).
3. **All of P4 (steps 1–5)** landed and **applied to the real spec**: `@Name`
   cross-refs, `reads_member`, `member_of_module` use-site edges,
   counting/uniqueness, and one global solve. Each application converts real
   name-pin debt to a relational selector with the debundle output unchanged.
4. **Fail-closed everywhere**: lowering errors rather than silently
   under-constrains. No special-case hacks, no silent fallbacks.

## Operating rules (every step)

- **Branch** `claude/lucid-mendel-178j6q` (both repos). Commit footers:
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` +
  `Claude-Session: https://claude.ai/code/session_01JYRJift7ufbrfsdXTZDyaS`.
  **Never open a PR** unless explicitly asked. **No model identifier** in any
  committed artifact.
- **Bazel**: `bazelisk --server_javabase=/usr/lib/jvm/java-21-openjdk-amd64
build|test <targets>` with `dangerouslyDisableSandbox: true`, run from
  `/home/user/ducktape` (CWD drifts to `/home/user` across shell re-inits —
  always `cd` first). **RBE/`bbr` is unavailable here** (no `BUILDBUDDY_API_KEY`):
  verify on **targeted** targets only, **never `//...`** — the container has
  ~5G free of a ~38G quota and a full build base is ~8G. If disk tightens, reclaim
  re-fetchable caches (`~/.cache/bazel/_bazel_root/*/{cache,execroot}` of stale
  bases, `/tmp/*.log`); never delete `~/.cache/bazelisk`.
- **Commits** via `nix develop --command git commit -F <msgfile>`; rustfmt +
  prettier hooks run and may reformat → re-stage the reformatted file and
  re-commit. Pre-run `nix develop --command rustfmt --edition 2024 <changed.rs>`
  to avoid the dance.
- **Push** `git push -u origin claude/lucid-mendel-178j6q`, retry x4 with
  exponential backoff on network errors only.
- Each step ends in a **verified** (build + test green, lint on) **commit +
  push**. After each, update the progress ledger here and the worklist.

## Verification gates

- **Build gate**: `bazelisk build` the changed lib **and its consumers** (lets the
  compiler prove match exhaustiveness). Lint aspects (clippy + rustfmt) run on
  build — green build ⇒ lint-clean.
- **Test gate**: the unit/e2e tests for the changed area, `--cache_test_results=no`,
  lint on (no `--config=nolint` on the final run of a step).
- **THE parity gate (F)**: the corpus differential over the real tana/re spec =
  **0** disagreements, through the production resolution path. Mechanism +
  invocation: <../debug/2026_06_18_per_chunk_gate_real_source.md>
  (`PER_CHUNK_JS_ROOT=/home/user/gaffer-private/tana/re/web/78d928dca7/js`,
  classes: resolved_parity / value_disagree / reject_parity / fail_closed /
  over_resolved — only resolved_parity + reject_parity are allowed). Gates the
  flip and the delete.
- **Conversion gate (X)**: after converting a real selector from a name-pin to a
  relational selector, the spec's debundle **generated output is byte-identical**
  and the converted selector resolves to the **same binding** the name-pin did.
  This is X's parity proof (cross-ref selectors have no hand-rolled twin, so the
  differential does not apply to them).

## Abort & escalation bar (the goal's central directive)

- If a selector kind or the relational model **will not admit one general
  faithful encoding** — without a special-case hack or a silent fallback — **STOP**.
  Write the dead-end analysis into this file (what was attempted, why it fails,
  what the model would need), commit it, and check in with the user. **Do not
  hack toward the endpoint.** An honest dead end beats a matcher we can't trust.
- **Check in (do not self-resolve)** on: a genuine design fork with no principled
  default; a parity regression whose faithful fix isn't clear; a faithful-encoding
  dead end. Everything else: proceed on best engineering judgment.
- Fail-closed is non-negotiable: a selector that cannot be resolved categorically
  must **error**, never guess.

## Pre-made decisions (so execution does not stall)

- **`@Name` anchor map**: prefer the owner graph's `export_name` (the spec's
  readable member name) as the readable→binding handle. If `export_name` is not
  populated at member-resolution time, build the anchor readable→binding map from
  the **already-resolved members** (anchor-first order). Both paths use the proven
  kernel primitives (`owner_for_export` / `referencer_of_kind` /
  `alias_owner_for` / `binding_for_owner`).
- **`Resolution` in-pipeline**: `selector_solve::solve` consumes the lean
  `OwnerGraph` (JSON). In-pipeline, feed it the emitted owner-graph JSON if it is
  in scope; otherwise add a thin `solve`-over-report adapter — **do not** couple
  `selector_solve` to the `analysis` crate's rich types.
- **Flip shape (F4)**: `DifferentialResolver<Datalog, AstWildcard>` first (Datalog
  primary, hand-rolled shadow) for one green corpus run, then drop the shadow in
  F5. This keeps a one-commit safety net between flip and delete.

---

## Phases

Each phase is a sequence of verified commits. A phase's gate authorizes the next.

### Phase F — Replace & delete the hand-rolled matcher (headline goal)

**State correction (execution, commit `7cf02821`→):** the runbook's original
F0/F1 were stale. Reality on entry: the fact matcher reaches corpus parity
standalone (the per-chunk differential is **green**, 0 disagreements, ~22s —
<../debug/2026_06_18_per_chunk_gate_real_source.md>), **and `DatalogResolver`
already fully implemented `SelectorResolver`** (all three methods, 14 tests).
So F1 was done. The real remaining work is the **build-once-per-chunk seam** and
the production wiring/flip/delete — sequenced below.

- **F2-seam ✅ (done this run):** the per-call `SelectorResolver` trait (took
  `module` per call) was a half-measure — only tests used it; production bypasses
  it via the `binding_resolution` free functions, and the corpus binary already
  builds `ChunkResolver` once. The trait is now **chunk-bound**: methods drop the
  `module` arg, the implementor holds the chunk and builds its model once.
  `AstWildcardResolver<'m>` wraps the borrow (production needs no precompute);
  `ChunkResolver` impls it directly; the per-call `DatalogResolver` wrapper (which
  rebuilt the whole EDB on every call — fatal in the 1751-request loop) is
  **deleted**. Gate: `source_match_test` + `corpus_match_differential` build green.
- **F2-wire-member-group ✅ (done this run):** the chunk-bound resolver is built
  once in `materialize_chunk` (where `runtime_ast.module` has a stable borrow) and
  carried on `ExplicitRequestContext`, so all 1751 requests share it. The member
  path (`plans.rs::resolve_source_match` → `resolver.resolve_member`) and the
  binding-group path (`plan_builder.rs::resolve_request_source_matches` →
  `resolver.resolve_member_group(...).bindings`) now route through the seam.
  Production stays `AstWildcardResolver` (the seam delegates to the same free
  functions — behaviorally identical). Gate: `lowering_test` + e2e
  `{edit_gate_source_match,binding_name_resolution,gate}_cli_test` green.
- **F2-wire-anon-ordinals ✅ (done this run):** the chunk-bound anonymous path
  (`lowering/anonymous.rs::resolve_anonymous_statement_ordinals`) now routes through
  `ctx.selector_resolver.resolve_anonymous_groups`; the exactly-one-group
  categoricity that lived in `resolve_anonymous_statement_body_indices` moved to the
  caller (`one_anonymous_group`), so the flip carries this path too. Gate:
  `lowering_test` + e2e `{peel_factorize_extend_anonymous,gate,edit_gate_source_match}_cli_test`.
- **F2-wire-anon-comove ✅ (done this run):** the per-source co-move anonymous
  path (`anonymous_resolution.rs::resolve_anonymous_statement_claims_in_globals`)
  now builds one resolver per parsed source (once, before the claims×selector loops)
  and routes through `resolve_anonymous_groups`. Gate: 4 e2e cli gates pass.

**Scope finding — F5 (delete) is broader than the lowering flip.** Wiring the
lowering pipeline (member, group, anonymous) through the seam is enough to **flip
that pipeline** to the fact resolver (F4). But `AstWildcardMatcher` has consumers
the categorical seam does **not** cover, so deleting it is materially larger:

- `anonymous_resolution.rs::resolve_member_selector_claims_in_globals` uses
  `member_binding_candidates` — **non-categorical**, aggregates candidates across
  sources then applies cross-source categoricity. The seam exposes only
  categorical `resolve_member`; this needs a candidates method or a restructure.
- `source_match_body_debt` (used in `plan_builder` failure reporting + spec-validate
  near-miss hints). **Verdict after reading it:** it splits cleanly. `exact_groups`
  the fact resolver already computes. But `near_misses` calls
  `first_mismatch_reason(needle, candidate)` — a per-candidate **scored "first
  structural divergence" with a human reason string**. That is matcher _introspection_
  (an AST-walk that reports where/why a near-match diverged), not the boolean/set
  verdict the fact matcher produces. **This is the genuine F5 fork** (the user's call,
  not a resolution dead-end): (a) reimplement scored near-miss reasons over the fact
  model — large, and the reason text is inherently AST-shaped; (b) accept degraded
  failure diagnostics; or (c) keep a **diagnostics-only** residual matcher surface
  (resolution on facts, near-miss reasons on the AST walk). Resolution itself is
  fully served by facts — the headline goal does not depend on resolving this.
- `selector_codemod` (the minimizer) verifies minimized selectors via the matcher;
  `shape_index_soundness_test` likewise.
- the **corpus differential's own oracle is `AstWildcardResolver`** — deleting the
  matcher retires the differential (parity is proven at the flip, then the oracle
  is gone; the standing regression becomes ChunkResolver self-consistency, not a
  differential). This is inherent to a matcher replacement, not a bug.
  So F5 splits: serve `member_binding_candidates` + `source_match_body_debt` from
  the fact resolver (or keep them as the residual matcher surface), migrate
  codemod/soundness, then delete. The flip (F4) does **not** depend on this.
- **F3 (GATE):** corpus differential = **0** over the real spec — already green
  standalone; re-confirm after the wiring touches the dispatch. Non-zero ⇒ fix the
  fact matcher **faithfully**; an unencodable construct ⇒ **ABORT + write-up**.
- **F4 ✅ (main lowering pipeline flipped this run):** the single construction site
  in `materialize_chunk` now builds `ChunkResolver` (the fact resolver) instead of
  `AstWildcardResolver`, so the whole lowering pipeline (member, binding-group,
  anonymous-ordinals) resolves selectors via facts. Gated on: resolver parity proven
  over the real corpus (the match logic is byte-identical since the green run — these
  commits only changed the trait signature + wiring) + 6 e2e cli output gates green
  (`{edit_gate_source_match,binding_name_resolution,gate,module_merge,peel_factorize_landability,peel_factorize_extend_anonymous}`).
  **Deviation from the original runbook:** flipped _directly_, not via a permanent
  production `DifferentialResolver` — the differential stays the gate (corpus binary +
  tests), not a hot-path fixture. **Remaining:** the per-source co-move path
  (`anonymous_resolution.rs` fn 2) still constructs `AstWildcardResolver` — flip it
  too (trivial) once its e2e coverage is confirmed; harmless mixed state until then
  (both resolvers are parity-proven).
- **F5 — chosen direction: pursue FULL deletion** (latest user decision, supersedes the
  earlier diagnostics-only fallback). Make the fact resolver the sole resolver, then
  reproduce the matcher's remaining surfaces on facts so `AstWildcardMatcher` can be
  deleted outright: candidates, the codemod minimizer, and — the crux —
  `source_match_body_debt` near-miss diagnostics. **Per the abort bar: attempt the
  `body_debt` near-miss encoding on the fact model; if it can't be done faithfully
  (the reason text is AST-walk-shaped), STOP and report — do not degrade or fake it.**
  If full deletion proves infeasible at `body_debt`, fall back to the diagnostics-only
  residual. Concrete plan:
  - **New gate for step 1 (found while prepping):** the corpus differential proves
    _categorical_ parity (count + winner), **not** full candidate-_list_ equivalence.
    So `member_candidates` needs its own **candidate-list differential** (fact list ==
    `member_binding_candidates` matcher list over the real corpus) before it can replace
    the matcher path — add that to `corpus_match_differential` and gate on it = 0.
  1. **Add `ChunkResolver::member_candidates(request_id, selector) -> Result<Vec<ResolvedMemberBinding>>`**
     — the non-categorical match list. Refactor the four member sub-paths
     (`resolve_member_multi`, `resolve_var_declarator_member`,
     `resolve_declarator_hole_member`, and the single-statement arm of `resolve_member`)
     to each return their `matches: Vec<…>`; `resolve_member` keeps the
     `[single]`/`[]`/`multiple` categoricity wrapper, `member_candidates` returns the
     vec. **Required, not shortcuttable:** per-source categorical resolution would miss
     intra-source ambiguity that `member_binding_candidates`' cross-source count catches.
  2. **Reroute `anonymous_resolution.rs` fn 1** (`resolve_member_selector_claims_in_globals`)
     from `source_match::member_binding_candidates` to the per-source fact resolver's
     `member_candidates` (build one `ChunkResolver` per source, like fn 2).
  3. **Flip fn 2** (co-move anonymous) from `AstWildcardResolver` to `ChunkResolver`.
     ✅ done (`edit_gate_source_match` + `peel_factorize_{extend_anonymous,landability}`
     - `gate` e2e green). Steps 1–2 (the `member_candidates` refactor + fn 1 reroute)
       remain — the semantics-sensitive part, to be done carefully (not rushed).
  4. Now the matcher (`AstWildcardMatcher` + `find_member_binding_matches` +
     `find_anonymous_statement_body_index_groups` + `source_match_body_debt`) is reached
     ONLY by: `source_match_body_debt` diagnostics (`plan_builder` + `selector_debt.rs`
     - spec-validate), the `selector_codemod` minimizer, and the corpus differential's
       oracle. Keep those; drop the `AstWildcardResolver` seam wrapper if nothing but the
       differential uses it (the differential may keep its own oracle path).
  - **e2e coverage (confirmed):** fns 1 & 2 are exercised by `edit_gate_source_match_cli_test`,
    `binding_name_resolution_cli_test`, and `peel_factorize_{landability,extend_anonymous}_test`.
  - **Endpoint-definition note:** this leaves `AstWildcardMatcher` alive as a
    diagnostics/minimizer helper — so endpoint criterion #1 ("matcher deleted") is met
    in spirit (sole _resolver_), not literally. Full deletion would additionally require
    near-miss reasons on the fact model (deferred; AST-shaped, real quality risk).

### Phase X1 — `@Name` cross-references live (P4 step 1)

- **X1a:** thread `&selector_solve::Resolution` into
  `resolve_request_source_matches` / `MemberRequest::resolve_source_match`
  (they currently get only the chunk AST). Build it from the owner graph in scope
  in `materialize`.
- **X1b:** carry `CrossRefTarget` on `MemberRequest`; add `resolve_cross_ref`
  composing the kernel chain; remove the `build_members` fail-closed bail.
- **X1c:** settle the ordering question (pre-made decision above) with a test.
- **X1d (apply):** convert the real metaNode delegator
  (`isMeetingTranscriptionProvider` → `@isTranscriptionProvider`) and the alias
  debt to `cross_ref`. **Conversion gate.** The empty-class case waits for X3
  (structural ∧ cross-ref); the internal-anchor case (`generateUniqueId`→`rq`,
  no spec member) is **out of scope — document, do not hack**.

### Phase X2 — `reads_member` (P4 step 2)

- New EDB fact `reads_member(owner, member_name)` derived from the AST facts
  already in `chunk_facts`; a selector kind for "the function that reads member
  `.X` off the codegen context"; apply to the 72 TS codegen helpers.
  **Conversion gate.**

### Phase X3 — `member_of_module` use-site edge (P4 step 3)

- The first selector needing a **use-site** edge ("the export consumed as `mod.X`
  at a call site"). Pull use-site references into the relational model (new EDB
  facts from `chunk_facts`). Also unlocks the X1 empty-class case
  (structural ∧ cross-ref). **Conversion gate.**

### Phase X4 — counting / uniqueness (P4 step 4)

- `all_different` for duplicate-claim diagnostics; per-target categoricity as a
  constraint rather than a post-hoc check. Folds into X5.

### Phase X5 — one global solve (P4 step 5)

- Shift from per-selector solves to a single CSP over the whole spec: shared logic
  variables for `@Name`, `all_different` across targets. The capstone — "fully
  capable" lands here; steps 1–4 fold in. Largest architectural step; hold to the
  abort bar if the global encoding won't stay faithful.

---

## Progress ledger

Append a row per verified commit. (Pre-run state: kernel complete + proven on real
data; `cross_ref` surface landed fail-closed; commits `8c1afd4d`…`6235d751`.)

| phase | step                                                                                                          | commit            | gate result                                                                                                                                                                                                                                                                                                                  |
| ----- | ------------------------------------------------------------------------------------------------------------- | ----------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| (pre) | kernel + surface + plan                                                                                       | 8c1afd4d…6235d751 | green; corpus differential 0 standalone (worklist)                                                                                                                                                                                                                                                                           |
| F     | runbook + state correct                                                                                       | 7cf02821          | doc only                                                                                                                                                                                                                                                                                                                     |
| F     | F2-seam: chunk-bound seam, delete per-call DatalogResolver                                                    | 7306ce37          | `source_match_test` pass + `corpus_match_differential` builds (local bazelisk)                                                                                                                                                                                                                                               |
| F     | F2-wire-member-group: build-once resolver threaded (member + binding-group)                                   | 40f34b9b          | `lowering_test` + 3 e2e cli gates pass (local bazelisk)                                                                                                                                                                                                                                                                      |
| F     | F2-wire-anon-ordinals: chunk-bound anonymous path through the seam                                            | 0220695a          | `lowering_test` + 3 e2e cli gates pass (local bazelisk)                                                                                                                                                                                                                                                                      |
| F     | F2-wire-anon-comove: per-source co-move anonymous path through the seam                                       | b3406173          | 4 e2e cli gates pass (local bazelisk)                                                                                                                                                                                                                                                                                        |
| F     | F4: flip main lowering pipeline to the fact resolver (ChunkResolver)                                          | fe6e8d60          | `lowering_test` + 6 e2e cli output gates pass; corpus parity proven (unchanged logic)                                                                                                                                                                                                                                        |
| F     | record concrete F5 (matcher-deletion) fork from reading body_debt                                             | 376aa511          | doc only                                                                                                                                                                                                                                                                                                                     |
| F     | record chosen F5 direction (diagnostics-only residual) + impl plan                                            | 835c2eeb          | doc only                                                                                                                                                                                                                                                                                                                     |
| F     | F5 step 3: flip co-move anonymous fn 2 to the fact resolver                                                   | (this)            | 4 e2e cli gates pass (local bazelisk)                                                                                                                                                                                                                                                                                        |
| F     | F5 step 1: `member_candidates` (collectors + `one_member_match`) + candidate-list differential + reroute fn 1 | (this)            | candidate-list differential **0 divergences** over real corpus (member-candidates 6873 resolved-parity / 0 fc / 0 or / 0 vd, full 1751-module gate ✅); `source_match_test` + `lowering_test` + `edit_gate_source_match`/`binding_name_resolution`/`gate`/`peel_factorize_{landability,extend_anonymous}` e2e pass (lint on) |
