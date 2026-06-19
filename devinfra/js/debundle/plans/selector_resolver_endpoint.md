# Selector Resolver — Autonomous Execution Runbook to the Endpoint

Companion to the design narrative <selector_constraint_model.md> and the debt
worklist <../debug/2026_06_19_p4_debt_worklist.md>. This file is the **execution
contract** for an autonomous run: the ordered phases, the gates that authorize
each irreversible step, the abort bar, and the decisions pre-made so execution
does not stall. Update the progress ledger (bottom) after every commit — it is
the memory that survives context compaction.

## Endpoint (definition of done)

1. ✅ **DONE.** The hand-rolled `AstWildcardMatcher` and everything that existed only
   to serve or validate it (`matcher.rs`, `body_search.rs`, `prepared_needle.rs`,
   `target_matching.rs`, `hints.rs`, the matcher impls in `resolver.rs`, the matcher
   free functions in `binding_resolution.rs`, the matcher-only `source_match/tests.rs`,
   `alpha_canonicalize.rs`, `string_literal_predicate.rs`, `wildcard_idents.rs`, the
   `corpus_match_differential` oracle, the `selector_codemod` prefilter-soundness
   tests, and the matcher perf binary) are **deleted**. The fact-based `ChunkResolver`
   is the sole selector resolver behind the `SelectorResolver` seam, **corpus-proven**
   (the integrated differential passed 0 divergences across all 5 passes before the
   delete). Near-miss diagnostics (`fact_source_match_body_debt`) and `source_match`
   debt run entirely on the fact model; near-miss coverage is preserved as standalone
   golden tests in `fact_near_miss.rs`.
2. ✅ **DONE.** **Parity proven, not asserted**: the integrated corpus differential
   over the real tana/re spec passed **0 divergences** across all five passes
   (member / member-candidates / anonymous / binding-group / near-miss) immediately
   before the oracle was deleted.
3. ⬜ **REMAINING — the rest (Phases X1–X5).** All of P4 (steps 1–5) landed and
   **applied to the real spec**: `@Name` cross-refs, `reads_member`,
   `member_of_module` use-site edges, counting/uniqueness, and one global solve. Each
   application converts real name-pin debt to a relational selector with the debundle
   output unchanged. The X1 (`cross_ref`), X2 (`reads_member`), and X3
   (`member_of_module` use-site edge) primitives are **all landed and integrated** on
   the integration branch — they resolve through the full lowering pipeline,
   anchor-first / use-site, fail-closed, full suite green. The remaining work is the
   real-spec **conversions** (X1d onward), plus X4 (counting/uniqueness), X5 (one global
   solve), and push-to-zero.
4. ✅ **DONE (maintained).** **Fail-closed everywhere**: lowering errors rather than
   silently under-constrains. No special-case hacks, no silent fallbacks.

## P4 Execution — kickoff state (2026-06-19)

**Measured (Phase 0).** `selector-debt` on the live spec: **2193 `binding.name` pins**
(2192 minified-fragile; 1172 at score ≥80) vs. 5141 existing `source_match` selectors.
By family: features 1046 / domains 506 / app 452 / shared 106 / integrations 49 / infra 34;
162 depth-2 families (largest: `app/bootstrap` 200, `features/nodes` 161, `domains/graph`
159, `features/ai` 88, `features/tags` 77, `domains/ai` 76, `features/billing` 70…). These
are the stabilization lanes.

**Canonical recipe — use the GAFFER Bazel targets, NOT a hand-run `debundle` binary.** The
pipeline args (`tree_config`/`tree_modules`/`tree_vendor_marks`/`input_data`/`package_roots`)
are supplied by the `debundle_pipeline` rule at `gaffer-private//tana/re/web/78d928dca7:debundle`
(hand-running `debundle run` fails on missing `--tree-vendor-marks`/`--tree-source-root`/…).

- **Query/measure/convert** (selector-debt, synthesize-selectors, match-selector, validate):
  the `debundle_cli` wrapper sets `DEBUNDLE_MODULES`/`SOURCE_ROOT`/`GRAPH`. **This degraded
  session — verified flag forms (deviating breaks RBE):** `source
devinfra/secrets/web_env.sh`, then `bazelisk
--host_jvm_args=-Djavax.net.ssl.trustStore=/etc/ssl/certs/java/cacerts
--host_jvm_args=-Djavax.net.ssl.trustStorePassword=changeit run //tana/re:debundle_cli
--config=nolint --config=rbe --remote_header=x-buildbuddy-api-key=$BUILDBUDDY_API_KEY
--shell_executable=/bin/bash -- <subcommand>`. **Gotchas:** (1) **no `--platforms=`** — it
  strips the RBE container identity ⇒ `PERMISSION_DENIED: Container identity unknown` on every
  RBE-executed action (npm extract, regen); (2) **all bazel flags before `--`** (the `/tmp/bz`
  wrapper appends `--config=rbe` etc. _after_ args, so for `run … -- …` they leak into the CLI
  as `unexpected argument '--config'` — use raw `bazelisk` for `run`). First `run` compiles the
  debundler locally (~few min); then fast. `selector-debt --modules <spec/modules>` also works
  via a prebuilt raw binary (that's the count).
- **Byte-identical gate**: `/tmp/bz test //tana/re/web/78d928dca7:regen_js_test --config=nolint`
  (a `test`, so the appended flags are harmless). Verified PASSED on a fresh `--output_base`
  (each lane worktree has its own) — i.e. parallel-lane-safe. **Do NOT add `--platforms=` or a
  fresh `--output_base` via the AGENTS.md gate form here** — both reintroduce the container-identity
  failure in this session.
- **Test a NEW ducktape primitive against the real spec**: add `--config=source-debundler
--override_module=ducktape=/home/user/ducktape-<lane>` to the gaffer build (see
  <gaffer//tana/re/web/AGENTS.md>).
- All bazelisk runs: system-java truststore + RBE key (`/tmp/bz` wrapper, or `source
devinfra/secrets/web_env.sh`), `dangerouslyDisableSandbox: true`.

**Round status.** F (matcher deletion) and the X1/X2/X3 primitives (`cross_ref`
`acd80903`, `reads_member` `ce20a42b`, `member_of_module` use-site `ae6dec8b`) are all
**landed + integrated**; the branch was **squashed** to one commit (`395f2298b`), then
a **post-squash trim** (`aabaa2de8`, −660 lines): deleted the spent
`selector_query_examples` spike and the F5 study, and deduped the materialize
kind-label, claim, and anchor-fold helpers (`kind_labels.rs`,
`claim_post_stage_a_binding`, `resolved_anchor_bindings`). Full suite **129/129** green.
gaffer-private **PR #366** applied the first Phase A minimizer conversions (156).
**Next:** re-measure `selector-debt`, fan Phase A wide across the 162 families, then the
X-primitive real-spec conversions (delegators via `cross_ref`, codegen helpers via
`reads_member`, empty-class via `member_of_module`) ∥ push-to-zero escalation; then
X4/X5 global-solve capstone. Re-measure `selector-debt` each round; terminate at
name-only = 0 (or committed faithful-encoding dead-ends). Full plan:
`/root/.claude/plans/shimmying-tinkering-hedgehog.md`.

## Operating rules (every step)

- **Branch** `claude/lucid-mendel-178j6q` (both repos). Commit footers:
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>` +
  `Claude-Session: https://claude.ai/code/session_01JYRJift7ufbrfsdXTZDyaS`.
  **Never open a PR** unless explicitly asked. **No model identifier** in any
  committed artifact.
- **Bazel**: build/test with `dangerouslyDisableSandbox: true`, run from
  `/home/user/ducktape` (CWD drifts to `/home/user` across shell re-inits —
  always `cd` first). **RBE works** via `bazelisk` + the system-java truststore
  (`--host_jvm_args=-Djavax.net.ssl.trustStore=/etc/ssl/certs/java/cacerts`) plus
  `--config=rbe --remote_header=x-buildbuddy-api-key=$BUILDBUDDY_API_KEY`; when a
  degraded session hasn't populated the key, `source devinfra/secrets/web_env.sh`
  first (needs `SOPS_AGE_KEY`). Prefer `bazelisk` (`bbr` auth may be broken in a
  degraded session). `//devinfra/js/debundle/...` builds fine on RBE — no need to
  restrict to targeted targets. If the ext4 reserved blocks aren't reclaimed (a
  web_setup gap), `tune2fs -m 1 /dev/vda` frees ~210G.
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

### Phase F — Replace & delete the hand-rolled matcher (headline goal) ✅ COMPLETE

`AstWildcardMatcher` and its entire support cluster are **deleted** (`825e2887`); the
fact-based `ChunkResolver` is the sole resolver behind the `SelectorResolver` seam,
corpus-proven at 0 divergences before the oracle was retired. Near-miss diagnostics run
on the fact model (`source_match/fact_near_miss.rs`); the four post-deletion e2e
regressions (match equivalence, `target_binding` prebind, timing, diagnostics) were
repaired on the fact path (`08cc36ec`/`b6b3314a`/`74d669c8`) → full suite green. The
blow-by-blow F0–F5 walkthrough is condensed into the **progress ledger** rows below
(the durable record); live policy that still governs X4/X5 is in _Abort & escalation
bar_ and _Pre-made decisions_ above.

### Phase X1 — `@Name` cross-references live (P4 step 1)

- **X1a ✅:** the `selector_solve::Resolution` is built **once** per chunk in
  `materialize_logical_chunk` from the in-memory `precomputed.owner_graph` (Stage A),
  projected into the lean kernel graph by `lowering/materialize/cross_ref.rs`
  (`selector_solve` stays decoupled from `analysis`). Threaded into a dedicated
  builder pass rather than `resolve_source_match` because the owner graph (the
  reference/alias edges) only exists post-Stage-A — see X1c.
- **X1b ✅:** `MemberRequest` carries `cross_ref: Option<CrossRefTarget>`;
  `cross_ref::resolve_cross_ref` composes the kernel chain
  (`referencer_for`/`referencer_of_kind`/`alias_owner_for` → `binding_for_owner`),
  categorical / fail-closed; the `build_members` bail is removed; cross-ref members
  resolve + claim in `ChunkPlanBuilder::resolve_and_claim_cross_refs` (which also
  prunes the residual plan, like rebind folds).
- **X1c ✅:** ordering settled — **anchor-first**, because the owner graph's
  `export_name` is not populated at member-resolution time (it comes from the later
  `ChunkFactorization`). The anchor's binding is taken from already-resolved members.
  Pinned by `cross_ref_anchor_ordering_uses_resolved_member_binding` in
  `e2e/cross_ref_lowering_test.rs` (anchor selected by `source_match`, so no name
  shortcut could have resolved it). Full-pipeline `references` + `aliases` + fail-closed
  cases also covered there.
- **X1d (apply) — REMAINING:** convert the real metaNode delegator
  (`isMeetingTranscriptionProvider` → `@isTranscriptionProvider`) and the alias
  debt to `cross_ref`. **Conversion gate.** The empty-class case waits for X3
  (structural ∧ cross-ref); the internal-anchor case (`generateUniqueId`→`rq`,
  no spec member) is **out of scope — document, do not hack**.

### Phase X2 — `reads_member` (P4 step 2) ✅ primitive landed + integrated

- New EDB fact `reads_member(owner, member_name)` derived from the AST facts
  already in `chunk_facts`; a selector kind for "the function that reads member
  `.X` off the codegen context"; apply to the 72 TS codegen helpers.
  **Conversion gate.**
- **Landed + integrated (B2 lane `claude/p4-x2-reads-member`, commit `ce20a42b`;
  merged onto the integration branch `46b7e2313`):** the primitive is built end to
  end and proven through the full lowering pipeline — **but not yet applied to the
  real gaffer spec** (that is a separate later lane). The integration merge unions
  cleanly with X1 (shared `OwnerNode.member_reads` field, one shared
  `duplicate_claim_for`, the 6-tuple `build_members`); full suite **129/129** green
  with both primitives present.
  - **Kernel** (`selector_solve.rs`): `member_read` / `member_read_from` EDB
    relations and `reads_member` / `reads_member_from` derived relations (the
    `declares` conjunct mirrors `references` — only declaring owners are
    candidates), plus categorical resolvers `reads_member_owner` /
    `reads_member_owner_of_kind` / `reads_member_from_owner` /
    `reads_member_from_owner_of_kind`. 5 new unit tests.
  - **Fact derivation** (`chunk_facts.rs`): `member_reads_by_ordinal` projects
    each top-level statement's `Member`/`PropName` accesses to `(object?, member)`
    rows keyed by owner ordinal — **per-statement-tolerant** (a statement with an
    unmodeled construct yields no rows, fail-closed for the selector, unlike
    whole-chunk `extract_facts`). The object is the bare-identifier receiver
    (`ctx.X` ⟹ `Some("ctx")`); computed access contributes nothing.
  - **Surface** (`spec.rs`): a fourth `MemberSelectorSpec` variant `ReadsMember`
    beside `Binding`/`SourceMatch`/`CrossRef`. Shape: `reads_member: { member,
object?: @Anchor, kind? }`. Fail-closed (`deny_unknown_fields`, `member`
    required, exactly-one-of across the four selector kinds).
  - **Wiring** (`lowering/materialize/reads_member.rs` + `plan_builder.rs` +
    `mod.rs`, mirroring X1's `cross_ref` wiring): `build_resolution` projects the
    in-memory owner graph **plus** the chunk's AST member-reads (joined by
    statement ordinal) into the lean kernel EDB; `resolve_and_claim_reads_members`
    runs after Stage A (no-op + no solve for chunks without a reads-member
    member), resolving the optional `object: @Anchor` via the **already-resolved
    members** (anchor-first, the same ordering decision as `cross_ref`) and
    claiming the categorically-resolved binding.
  - **Gate:** `e2e/reads_member_lowering_test.rs` (5 tests) drives the real
    `debundle` binary: a bare-member helper and an object-constrained helper
    resolve to the right binding, land in the right module, run under Node; three
    fail-closed cases (ambiguous, zero-match, unknown object) error. `//devinfra/
js/debundle/...` builds green lint-on; the 4 pre-existing e2e failures
    (matcher-deletion fallout) confirmed unrelated via `git stash`.

### Phase X3 — `member_of_module` use-site edge (P4 step 3) ✅ primitive landed + integrated

- The first selector needing a **use-site** edge ("the export consumed as `mod.X`
  at a call site"). Pull use-site references into the relational model (new EDB
  facts from `chunk_facts`). Also unlocks the X1 empty-class case
  (structural ∧ cross-ref). **Conversion gate.**
- **Landed + integrated (B3 merge of lane `claude/p4-x3-member-of-module`, `ae6dec8b`,
  stacked on B2):** the primitive is built end to end and proven through the full
  lowering pipeline, now merged onto the integration branch alongside X1+X2 (union
  resolution: FIVE-kind `MemberSelectorSpec`, the 7-tuple `build_members`, three
  post-Stage-A passes sharing `claim_post_stage_a_binding`) — **but not yet applied to
  the real gaffer spec** (that is a separate later lane).
  - **Kernel** (`selector_solve.rs`): `module_member_use(owner, module, member)`
    EDB + derived `consumes_module_member` relation (the `declares` conjunct
    mirrors `reads_member`/`references` — only declaring owners are candidates),
    plus categorical resolvers `consumes_module_member_owner` /
    `consumes_module_member_owner_of_kind`. 5 new unit tests (incl. the empty
    two-subclass disambiguation and module-source disambiguation).
  - **Fact derivation** (`chunk_facts.rs`): `module_member_uses_by_ordinal`
    projects each statement's `Member` accesses (the same `Member`/`PropName`
    structure `member_reads_by_ordinal` uses) then **joins the bare-ident object to
    an import-source map** (local import binding → specifier). Per-statement
    tolerant (unmodeled construct ⇒ no row, fail-closed). The `extends mod.Base`
    superclass expression is captured as such a member access, which is what
    reaches the empty-subclass cluster.
  - **Surface** (`spec.rs`): a fifth `MemberSelectorSpec` variant `MemberOfModule`.
    Shape: `member_of_module: { module, member, kind? }`. Fail-closed
    (`deny_unknown_fields`, `module`+`member` required, exactly-one-of across the
    now-five selector kinds). Both labels are re-minify-invariant (import specifier
    - export name), unlike `reads_member`'s minified object.
  - **Wiring** (`lowering/materialize/member_of_module.rs` + `plan_builder.rs` +
    `mod.rs` + `plans.rs`, mirroring X2): `build_resolution` projects the in-memory
    owner graph **plus** the chunk's use-site facts (member accesses joined to the
    import table via `RuntimeImportFacts::iter_local_sources`) into the lean kernel
    EDB; `resolve_and_claim_member_of_modules` runs after Stage A (no-op + no solve
    for chunks without a member-of-module member). The post-Stage-A claim tail is
    now a shared `claim_post_stage_a_binding` helper used by both X2 and X3 (no
    object anchor for X3 — both its labels are invariant).
  - **Gate:** `e2e/member_of_module_lowering_test.rs` (5 tests) drives the real
    `debundle` binary: the empty-class/superclass disambiguation (two empty
    subclasses extending different members of an imported namespace, the
    `CardsViewAccessor` debt shape), a delegator, all resolving to the right
    binding, landing in the right module, running under Node; three fail-closed
    cases (ambiguous, zero-match, non-imported object).
  - **Scope (abort bar):** the primitive pins the declaring owner whose **own
    subtree** consumes `mod.X` (its `extends`/decorator/body). A target
    distinguished only by an _external_ `registry.register(Target)` is correctly
    out of reach (that owner declares nothing) — pinning by call argument needs a
    `resolves_to`-of-argument edge, a separate later primitive. Documented, not
    hacked.

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

| phase  | step                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | commit            | gate result                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| ------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| (pre)  | kernel + surface + plan                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | 8c1afd4d…6235d751 | green; corpus differential 0 standalone (worklist)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| F      | runbook + state correct                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | 7cf02821          | doc only                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| F      | F2-seam: chunk-bound seam, delete per-call DatalogResolver                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | 7306ce37          | `source_match_test` pass + `corpus_match_differential` builds (local bazelisk)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| F      | F2-wire-member-group: build-once resolver threaded (member + binding-group)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | 40f34b9b          | `lowering_test` + 3 e2e cli gates pass (local bazelisk)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| F      | F2-wire-anon-ordinals: chunk-bound anonymous path through the seam                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | 0220695a          | `lowering_test` + 3 e2e cli gates pass (local bazelisk)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       |
| F      | F2-wire-anon-comove: per-source co-move anonymous path through the seam                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | b3406173          | 4 e2e cli gates pass (local bazelisk)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| F      | F4: flip main lowering pipeline to the fact resolver (ChunkResolver)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | fe6e8d60          | `lowering_test` + 6 e2e cli output gates pass; corpus parity proven (unchanged logic)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| F      | record concrete F5 (matcher-deletion) fork from reading body_debt                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | 376aa511          | doc only                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| F      | record chosen F5 direction (diagnostics-only residual) + impl plan                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | 835c2eeb          | doc only                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| F      | F5 step 3: flip co-move anonymous fn 2 to the fact resolver                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | (this)            | 4 e2e cli gates pass (local bazelisk)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| F      | F5 step 1: `member_candidates` (collectors + `one_member_match`) + candidate-list differential + reroute fn 1                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | (this)            | candidate-list differential **0 divergences** over real corpus (member-candidates 6873 resolved-parity / 0 fc / 0 or / 0 vd, full 1751-module gate ✅); `source_match_test` + `lowering_test` + `edit_gate_source_match`/`binding_name_resolution`/`gate`/`peel_factorize_{landability,extend_anonymous}` e2e pass (lint on)                                                                                                                                                                                                                                                                  |
| F      | F5 codemod migration: reroute `selector_codemod`'s 3 matcher wrappers (`matched_binding_candidates`, `matched_body_indices` via it, `prove_synthesized_selector` both branches) + same-crate `match_selector` off `AstWildcardMatcher` onto `ChunkResolver::{member_candidates,resolve_member_group}`; `candidate_index`/`BodyIndexFilter` now `#[cfg(test)]` (matcher-surface `prefilter_soundness_tests` only). Matcher symbols NOT deleted (later join).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | (this)            | proptest `minimized_selector_uniquely_matches_target` green at `PROPTEST_CASES=2000` (fresh); candidate-list differential **still 0 divergences** over real corpus (member-candidates 6873 / 0 fc/or/vd, full 1751-module gate ✅); `source_match_test` pass; whole `//devinfra/js/debundle/...` (242 targets) builds with lint on                                                                                                                                                                                                                                                            |
| F      | **F5 FINAL — `AstWildcardMatcher` DELETED (endpoint reached).** Removed `matcher.rs`/`body_search.rs`/`prepared_needle.rs`/`target_matching.rs`/`hints.rs`/`tests.rs`/`alpha_canonicalize.rs`/`string_literal_predicate.rs`/`wildcard_idents.rs`, the matcher free fns + impls in `binding_resolution.rs`/`resolver.rs`, `corpus_match_differential` + perf binary + `prefilter_soundness_tests`. `fact_source_match_body_debt` exact-groups now via `match_top_level_sequence_indexed` (fact, corpus-proven equal). Near-miss tests **converted to standalone golden** (`fact_near_miss_golden_per_variant`, 20 variants + TS-only + declarator-hole). `selector_match_differential_test` + `shape_index_soundness_test` + `selector_candidate_index` tests rerouted to `ChunkResolver`/`selector_match`. Restored 4 shared selector-shape helpers into `declared_bindings.rs` (datalog-side, not matcher).                                                                     | (this)            | grep-clean of matcher symbols (only kept `fact_first_mismatch_reason` + doc hits); `//devinfra/js/debundle/...` builds green lint-on; 9 tests pass (`source_match`/`selector_codemod`/`lowering`/`selector_debt`/`cli`/`peel`/`selector_match_differential`/`selector_candidate_index`/`shape_index_soundness`); `corpus_match_differential` + perf targets gone (query fails)                                                                                                                                                                                                                |
| F5-fix | **Post-deletion e2e repair (matcher coverage).** F5 dropped real matcher behavior the fact resolver never reproduced, leaving 4 e2e targets RED. Restored on the fact path in `selector_match.rs`/`chunk_facts.rs`: shorthand⟷explicit same-name property equivalence (object `Shorthand`/`KeyValue` + pattern `PatAssign`/`PatKeyValue`, with the destructure-key **exact-spelling** gate + shorthand `Prop` token in `invariant_tokens`); `target_binding` prebind honored under **Exact** mode (`match_ref`/`match_binding` consult the bijection first); wildcard string literals (`str_wildcard` fact + `extract_facts_needle` + `bind_string`-style consistency).                                                                                                                                                                                                                                                                                                          | 08cc36ec          | 4 MATCH-regression e2e cases fixed; `selector_match_differential_test` + all 33 debundle library unit tests green (lint on)                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| F5-fix | **Post-deletion e2e repair (timing).** F5 trimmed `timing.rs` to identity-key helpers and dropped emission + call sites. Restored env config + `emit_source_match_timing`, wired into `ChunkResolver::resolve_member` (gated by the plan-builder `source_match_cache` → one line per unique selector per chunk).                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | b6b3314a          | `source_match_timing_env_reports_member_selector_resolution` / `_preview_can_be_disabled` / `repeated_source_match_selectors_reuse_chunk_cache_across_modules` green (lint on)                                                                                                                                                                                                                                                                                                                                                                                                                |
| F5-fix | **Post-deletion e2e repair (diagnostics).** F5 ported only leaf near-miss reasons; the candidate-list framing (`hints.rs`) was dropped and the no-match hint / capability gate never re-wired. Added `fact_source_match_no_match_hint` (Nearest class / variable-declaration candidate blocks, fact-based), `ChunkResolver::collapse_member_match` (top-level-declaration wording + `target_binding` hint + `Source:` echo + ambiguity body-indices/bindings + near-miss hint), and routed `parse_needles` through `parse_selector_module_with_capability_check` (ANYTHING-key / reserved-hole fail-closed before match).                                                                                                                                                                                                                                                                                                                                                        | 74d669c8          | **all 4 targets green; full `//devinfra/js/debundle/...` 127/127 green (lint on)**; `fact_near_miss_golden_per_variant` unchanged                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| X1     | **X1a–X1c — `@Name` cross-ref WIRED into production lowering (B1).** `MemberRequest.cross_ref`; `build_members` bail removed; `selector_solve::Resolution` built once per chunk from the in-memory `precomputed.owner_graph` via the lean-graph projection in `lowering/materialize/cross_ref.rs` (`build_resolution`/`resolve_cross_ref`; `selector_solve` stays decoupled from `analysis`); cross-ref members resolve + claim in `ChunkPlanBuilder::resolve_and_claim_cross_refs` (a post-Stage-A pass that also prunes the residual plan). Ordering settled **anchor-first** (owner-graph `export_name` not populated at member-resolution time) — anchor binding taken from already-resolved members.                                                                                                                                                                                                                                                                        | acd80903          | `//devinfra/js/debundle/...` (241 targets) builds green lint-on (clippy+rustfmt aspects); new `cross_ref_lowering_test` (4/4: references + aliases full-pipeline, anchor-ordering pin, fail-closed) + `selector_solve_cross_reference_test` + `lowering`/`source_match`/`edit_gate_source_match`/`binding_name_resolution`/`gate`/`module_merge`/`peel_factorize_{landability,extend_anonymous}` pass, unchanged for non-cross_ref. (Verified against the integration branch: the 4 matcher-deletion e2e regressions noted on the lane branch are fixed by `08cc36ec`/`b6b3314a`/`74d669c8`.) |
| X2     | **`reads_member` primitive WIRED + proven, then INTEGRATED (B2).** kernel EDB + 4 categorical resolvers (`selector_solve.rs`), AST fact derivation `member_reads_by_ordinal` (`chunk_facts.rs`), `ReadsMember` selector surface (`spec.rs`), production wiring (`lowering/materialize/reads_member.rs` + `plan_builder.rs` + `mod.rs`). NOT yet applied to the real spec (separate lane). Lane commit `ce20a42b`; integration merge resolved by union with X1 (shared `OwnerNode.member_reads` field, shared `duplicate_claim_for`, the 6-tuple `build_members`).                                                                                                                                                                                                                                                                                                                                                                                                                | ce20a42b (merge)  | `//devinfra/js/debundle/...` (242 targets) builds green lint-on; full suite **129/129** green including `e2e/reads_member_lowering_test` (5: bare + object-constrained resolve through full pipeline under Node, + ambiguous/zero/unknown-object fail-closed) alongside `cross_ref_lowering_test` + `selector_solve_cross_reference_test`.                                                                                                                                                                                                                                                    |
| X3     | **`member_of_module` use-site primitive WIRED + proven, then INTEGRATED (B3).** The first use-site edge — kernel `module_member_use` EDB + derived `consumes_module_member` + 2 categorical resolvers (`selector_solve.rs`), AST+import fact join `module_member_uses_by_ordinal` (`chunk_facts.rs`), `MemberOfModule` selector surface (`spec.rs`), production wiring (`lowering/materialize/member_of_module.rs` + `plan_builder.rs` + `mod.rs` + `plans.rs`; shared `claim_post_stage_a_binding` tail with X2). Unlocks the empty-class/superclass cluster (in-subtree `extends mod.Base`); external-registration disambiguation documented out of scope. NOT yet applied to the real spec (separate lane). Lane commit `ae6dec8b` (stacked on B2); integration merge resolved by union with X1+X2 (FIVE-kind `MemberSelectorSpec`, the 7-tuple `build_members`, three post-Stage-A passes sharing `claim_post_stage_a_binding`/`duplicate_claim_for`, union runbook ledger). | ae6dec8b (merge)  | `//devinfra/js/debundle/...` builds green lint-on; full suite green including `e2e/member_of_module_lowering_test` (5: empty-subclass disambiguation + delegator resolve through full pipeline under Node, + ambiguous/zero/non-imported fail-closed) alongside `reads_member_lowering_test` + `cross_ref_lowering_test`. The 4 matcher-deletion failures X3's pre-fix lane saw (`anonymous_statement`/`cross_module_emission`/`syntactic_holes`/`selector_minimizer_expectation`) now **PASS** here (fix `08cc36ec`/`b6b3314a`/`74d669c8` is in this base).                                  |
