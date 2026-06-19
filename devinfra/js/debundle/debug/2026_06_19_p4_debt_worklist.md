# P4 expressivity — debt measurement & prioritized worklist (2026-06-19)

Complement to <../plans/selector*constraint_model.md> (which carries the P4 design
narrative). This note is the **quantitative** side: what the current
name-pinned spec costs us, and the construct-by-construct worklist that pays it
down, ordered by impact-per-unit-effort. Numbers are corpus measurements over
the tana/re spec unless marked \_est.*

**Status (post-F, 2026-06-19):** Track F is **complete** — `AstWildcardMatcher` deleted,
`ChunkResolver` the sole resolver (`825e2887`). The F-then-X dependency is cleared:
**X1 (`cross_ref`), X2 (`reads_member`), and X3 (`member_of_module`) primitives are all
LANDED + INTEGRATED** (B1 `acd80903` + B2 `ce20a42b` + B3 `ae6dec8b`): the
`selector_solve` kernel is built once per chunk from the in-memory owner graph, and
`cross_ref` / `reads_member` / `member_of_module` members resolve + claim through the
full lowering pipeline, anchor-first / use-site, fail-closed, with non-relational output
unchanged; full suite green. The remaining P4 work is the real-spec **conversions**
(apply `cross_ref` to a real metaNode delegator — X1d —, `reads_member` to the 72
helpers, and `member_of_module` to the empty-subclass cluster), then X4/X5, plus
push-to-zero.

## The debt, measured

- **2,193 `binding.name` pins** across the spec. Every one rides a _minified
  identifier_ — the exact thing that re-minification churns. Each is a
  re-minify-fragile selector: stable only as long as the bundler hands the same
  short name to the same entity. This is the headline debt P4 retires.
- The fragility is not uniform. The pins cluster into a handful of shapes whose
  stable identity is **relational**, not nominal — they _have_ a faithful
  re-minify-proof encoding and are only pinned by name because the old language
  had no cross-reference primitive. Those clusters are the worklist below.

## Worklist (impact-ordered)

### Step 1 — `@Name` cross-reference (owner-graph only) ✅ kernel + surface + wiring landed

The single highest-impact primitive: pin a target by an **invariant edge to a
separately-identified entity** instead of by its own minified name.

- **Impact:** ~76+ direct pins resolve as cross-refs; 200–400 corpus-wide _est._
  once the derived predicates (`calls`/`alias`) are in. Turns metaNode's ~18
  stabilization-debt items into ~5 and removes the neighbor-borrow temptation.
- **Why first:** needs only the owner graph (already emitted) — no new EDB
  relation, no use-site AST facts. Pure win over data we already have.
- **Status:** the solver kernel landed in `selector_solve.rs` (commit
  `8c1afd4d`): the `references` relation (`resolves_to` projected to owner
  granularity) plus two categorical resolvers —
  - `referencer_for(@Name)` — the unique owner that references `@Name`; pins a
    shapeless delegator `function UBt(x){ return EBt(x) }` by what it references.
  - `alias_owner_for(@Name)` — the unique var-decl aliasing `@Name`
    (`const X = @Name`); pins a re-export by the class it aliases.
    Both return `None` on zero-or-several (per-target categoricity). 6/6 tests green.
- **Proven on real data + refined** (commit `1ed920f2`): running the kernel on an
  `owner_graph.json` the real pipeline emits caught that `export { X }` and
  side-effect statements are owners that reference _every_ binding they touch, so
  `references` now requires the referencer to be a _declaring_ owner (a `@Name`
  target has identity). `e2e/selector_solve_cross_reference_test` proves a
  delegator (`UBt→EBt`) and an alias (`const HI = UJ`) resolve on the emitted
  graph; consumer owners are correctly excluded.

**The `@Name` selector (architecture) — ✅ landed** (wiring `acd80903`, integrated
`46b7e2313`). Surface, anchor semantics, owner-graph resolution, and the post-Stage-A
resolve-and-claim pass are all in production; details in "Kernel + surface + WIRING
landed" below.

**Grounding (real metaNode cases).** A read-only sweep of the actual
`metaNode.yaml` (gaffer-private) found the debt is **not monolithic** — the three
explicitly-commented cases split into three resolution sub-patterns, only one of
which the owner-graph-only MVP handles:

| case (minified)                          | shape                              | anchor                                                         | MVP?                                                                                                                   |
| ---------------------------------------- | ---------------------------------- | -------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `isMeetingTranscriptionProvider` (`UBt`) | `function UBt(n){ return EBt(n) }` | `EBt` = spec member `isTranscriptionProvider` (another module) | ✅ delegator → spec member; `references @Name`, kind `function_declaration`                                            |
| `CardsViewAccessor` (`Uee`)              | `class Uee extends Ye {}`          | `Ye` = spec member `NodeAttributeAccessor`                     | ✅ Step 3 (`member_of_module`): superclass ref **and** empty body, disambiguated by the consumed `mod.X` use-site edge |
| `generateUniqueId` (`ls`)                | `function ls(){ return rq() }`     | `rq` = **internal** binding, no spec member                    | ❌ no `@Name` until `rq` is lifted into the spec                                                                       |

So the MVP cleanly retires the **delegator → spec member** sub-pattern (the
canonical `UBt`/`EBt` case the kernel already tests). `@Name` is confirmed to be a
cross-member reference by readable name; the anchor member can live in another
module (the owner graph is global, so cross-module is free). The empty-class case
needs cross-ref ∧ structural (the fact matcher); the internal-anchor case needs an
internal handle or lifting the anchor into the spec — both out of the MVP.

**Coupling with Track F (the flip).** Making `@Name` usable in production resolves
anchor members first, then cross-ref members — so it sits on top of the existing
per-member resolution and threads the owner graph through it. That is the same
member-resolution pipeline Track F (seam-routing) touches, so X-wiring and F are
**not independent**: F's `SelectorResolver` seam is the foundation a `CrossRef`
resolver plugs into. This suggests F-then-X, not X-then-F.

**Kernel + surface + WIRING landed; only the first conversion remains.** The
owner-graph resolution is done and proven _end to end on a real emitted graph_ —
including the readable-anchor chain (commit `5a25e06e`): `owner_for_export(@Name)`
→ `binding_for_owner` → `referencer_of_kind(_, kind)` / `alias_owner_for` →
`binding_for_owner`, returning the target's binding with no minified name written
anywhere. The `cross_ref` selector surface is landed and fail-closed (`f895c493`).
The production wiring (B1) is now landed too:

- **Thread point (done):** `MemberRequest` carries `cross_ref: Option<CrossRefTarget>`;
  `build_members` no longer fail-closes on it. A `selector_solve::Resolution` is built
  **once** in `materialize_logical_chunk` from the in-memory `precomputed.owner_graph`
  (Stage A), via the lean-graph projection in `lowering/materialize/cross_ref.rs`
  (`build_resolution` + `resolve_cross_ref`) — `selector_solve` stays decoupled from
  `analysis` (the projection lives in the lowering crate). Cross-ref members are
  resolved + claimed in `ChunkPlanBuilder::resolve_and_claim_cross_refs`, a pass that
  runs right after Stage A (the owner graph's reference/alias edges are only in scope
  there) and before the rebind-fold / mini-factor / finalize steps. Named/source-match
  members are byte-for-byte unaffected.
- **Ordering question, SETTLED (with a test):** the owner graph's `export_name`s are
  **not** populated at member-resolution time — `BindingReport::export_name` is filled
  from the `ChunkFactorization`, built _after_ the per-chunk plan. So the lean graph
  carries no `export_name` and the kernel is driven through the **anchor-first** handle:
  the anchor's _minified_ binding comes from the already-resolved members
  (`export_name → binding`), and the kernel rides the reference/alias edges from there.
  Pinned by `cross_ref_anchor_ordering_uses_resolved_member_binding` in
  `e2e/cross_ref_lowering_test.rs` (the anchor is itself selected by `source_match`, so
  no name shortcut could have found it).
- **Verification:** `cross_ref_lowering_test` proves a delegator (`references @Name`)
  and a re-export (`aliases @Name`) resolve through the full lowering pipeline to the
  correct binding and run under Node; `selector_solve_cross_reference_test` + the
  `lowering`/`source_match`/e2e cli gates stay green and unchanged for non-cross_ref
  selectors. Build green lint-on.
- **`Resolution`** is built by `selector_solve::solve` from the lean `OwnerGraph`; the
  in-pipeline path projects the in-memory `analysis::OwnerGraph` into the lean struct
  (no JSON round-trip, no `analysis` coupling in `selector_solve`).

### Step 2 — `reads_member` ✅ primitive wired + proven + integrated (not yet applied)

- **Impact:** 72 TS codegen helpers pinned by name today; their stable identity
  is "the function that reads member `.X` off the codegen context."
- **Done + integrated (B2 lane `claude/p4-x2-reads-member`, commit `ce20a42b`;
  merged onto the integration branch `46b7e2313`):** the primitive is built end to
  end and proven through the full lowering pipeline (the wire-and-prove half);
  **applying it to the real gaffer spec's 72 helpers is a separate later lane** (the
  conversion gate). What landed:
  - **Kernel** `selector_solve.rs`: `member_read`/`member_read_from` EDB +
    `reads_member`/`reads_member_from` derived relations (the `declares` conjunct
    keeps only declaring owners as candidates, mirroring `references`), categorical
    resolvers `reads_member_owner` / `reads_member_owner_of_kind` /
    `reads_member_from_owner` / `reads_member_from_owner_of_kind`.
  - **Fact** `chunk_facts::member_reads_by_ordinal`: the per-owner `(object?,
member)` rows from the existing `Member`/`PropName` projection,
    per-statement-tolerant (a statement with an unmodeled construct contributes no
    rows — fail-closed for the selector, never a wrong row).
  - **Surface** `spec.rs`: `reads_member: { member, object?: @Anchor, kind? }` —
    a fourth `MemberSelectorSpec` variant, fail-closed (`member` required,
    exactly-one-of, `deny_unknown_fields`). The `object:` anchor narrows "reads
    `.X`" to "reads `.X` off the codegen context."
  - **Wiring** `lowering/materialize/reads_member.rs` (+ `plan_builder.rs` /
    `mod.rs`), mirroring X1's `cross_ref`: the lean kernel EDB is fed the owner
    graph **plus** the chunk's AST member-reads (joined by ordinal); the
    object-anchor resolves via the already-resolved members (anchor-first, same
    ordering decision as X1).
  - **Gate** `e2e/reads_member_lowering_test.rs`: 5 tests through the real
    `debundle` binary (bare + object-constrained resolve and run under Node; three
    fail-closed cases). `//devinfra/js/debundle/...` green lint-on.

### Step 3 — `member_of_module` ✅ primitive wired + proven + integrated (not yet applied)

- **Impact:** the empty-class/superclass cluster (e.g. `class Uee extends Ye {}`,
  several byte-identical empty subclasses) whose only stable identity is _how each is
  consumed_ — "the export consumed as `mod.X` at a use site." The first selector kind
  that needs a **use-site** edge, not just the owner graph.
- **Done + integrated (B3 lane `claude/p4-x3-member-of-module`, commit `ae6dec8b`,
  stacked on B2; merged onto the integration branch):** the primitive is built end to
  end and proven through the full lowering pipeline; **applying it to the real spec's
  empty-subclass cluster is a separate later lane** (the conversion gate). What landed:
  - **Kernel** `selector_solve.rs`: `module_member_use(owner, module, member)` EDB +
    derived `consumes_module_member` relation (the `declares` conjunct keeps only
    declaring owners as candidates, mirroring `reads_member`/`references`), categorical
    resolvers `consumes_module_member_owner` / `consumes_module_member_owner_of_kind`.
  - **Fact** `chunk_facts::module_member_uses_by_ordinal`: the per-statement `Member`
    accesses (same projection `member_reads_by_ordinal` uses) **joined to the import
    table** (local import binding → source specifier), per-statement-tolerant
    (unmodeled construct ⇒ no row, fail-closed). `extends mod.Base` is captured here.
  - **Surface** `spec.rs`: `member_of_module: { module, member, kind? }` — the fifth
    `MemberSelectorSpec` variant, fail-closed (`module`+`member` required,
    exactly-one-of across the now-five kinds, `deny_unknown_fields`). Both labels are
    re-minify-invariant (import specifier + export name), unlike `reads_member`'s
    minified object.
  - **Wiring** `lowering/materialize/member_of_module.rs` (+ `plan_builder.rs` /
    `mod.rs` / `plans.rs`), mirroring X2: the lean kernel EDB is fed the owner graph
    **plus** the chunk's use-site facts (member accesses joined to the import table via
    `RuntimeImportFacts::iter_local_sources`); the post-Stage-A claim tail is a shared
    `claim_post_stage_a_binding` helper used by both X2 and X3 (no object anchor — both
    X3 labels are invariant).
  - **Gate** `e2e/member_of_module_lowering_test.rs`: 5 tests through the real
    `debundle` binary (empty-subclass disambiguation + delegator resolve and run under
    Node; three fail-closed cases: ambiguous, zero-match, non-imported). Full suite
    green lint-on.
  - **Scope (abort bar):** pins the declaring owner whose **own subtree** consumes
    `mod.X`; a target distinguished only by an _external_ `registry.register(Target)`
    is out of reach (a separate later `resolves_to`-of-argument primitive). Documented,
    not hacked.

### Step 4 — counting / uniqueness

- `all_different` for duplicate-claim diagnostics and per-target categoricity
  expressed as a constraint rather than a post-hoc check. Depends on the global
  solve (Step 5) to be maximally useful.

### Step 5 — one global solve

- Shift from per-selector solves to a single CSP over the whole spec: shared
  logic variables for `@Name`, `all_different` across targets. "Fully capable"
  lands here. This is the architectural step; Steps 1–4 each work standalone and
  fold into it.

## Sequencing note

Steps 1–2 are owner-graph / existing-fact only and ship independently. Step 3
introduces use-site edges. Steps 4–5 are the global-solve architecture. The flip
of the _existing_ language (plan P5) is independent of all of this and can land
whenever the mechanical wiring is done.
