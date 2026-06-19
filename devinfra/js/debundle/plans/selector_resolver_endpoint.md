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

- **F0 (state):** the fact matcher (`selector_match` via `ChunkResolver`) already
  reaches corpus parity standalone (differential = 0, per the worklist).
  `DatalogResolver` is a stub; `ChunkResolver` does the work but may not cover all
  three `SelectorResolver` methods.
- **F1:** complete `DatalogResolver` as a full `SelectorResolver` — all three
  methods (`resolve_member`, `resolve_member_group`, `resolve_anonymous_groups`)
  delegate to `ChunkResolver`. Unit tests through the trait.
- **F2:** route the three production call sites through
  `DifferentialResolver<AstWildcard, Datalog>` in **shadow** (primary =
  AstWildcard, behavior unchanged), with a divergence sink that fails the corpus
  gate. Sites: `lowering/plans.rs::resolve_source_match`,
  `lowering/materialize/plan_builder.rs` (binding-group),
  `anonymous_resolution.rs::find_anonymous_statement_body_index_groups`.
- **F3 (GATE):** corpus differential through the production-shadow path = **0**
  over the real spec. Non-zero ⇒ diagnose each divergence and fix the fact matcher
  **faithfully**; a divergence exposing an unencodable construct ⇒ **ABORT + write-up**.
- **F4 (irreversible; gated on F3 = 0):** flip primary to Datalog
  (`DifferentialResolver<Datalog, AstWildcard>`); one green corpus run.
- **F5 (irreversible; gated):** delete `AstWildcardMatcher`, the `binding_resolution`
  free functions, and all now-dead code — atomic, all references updated. Keep a
  regression test that the fact resolver resolves the corpus.

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

| phase | step                    | commit            | gate result                                        |
| ----- | ----------------------- | ----------------- | -------------------------------------------------- |
| (pre) | kernel + surface + plan | 8c1afd4d…6235d751 | green; corpus differential 0 standalone (worklist) |
