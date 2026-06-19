# P4 expressivity — debt measurement & prioritized worklist (2026-06-19)

Complement to <../plans/selector*constraint_model.md> (which carries the P4 design
narrative). This note is the **quantitative** side: what the current
name-pinned spec costs us, and the construct-by-construct worklist that pays it
down, ordered by impact-per-unit-effort. Numbers are corpus measurements over
the tana/re spec unless marked \_est.*

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

### Step 1 — `@Name` cross-reference (owner-graph only) ✅ kernel landed

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

**Remaining for Step 1 — the `@Name` selector (architecture).** The kernel
resolves at owner granularity; making `@Name` usable in real selectors is:

- **Surface** (`spec.rs`): a third `MemberSelectorSpec` variant beside `Binding`
  and `SourceMatch` — `CrossRef`. A member's `selector` gains a `cross_ref` field;
  `MemberSelector::selected()` enforces exactly-one-of {`binding`, `source_match`,
  `cross_ref`}. Shape: `cross_ref: { references | aliases: @Name, kind?:
function_declaration | class_declaration | variable_declarator }`. Fail-closed
  (`deny_unknown_fields`; exactly one relation). Reuses the existing
  `BindingSourceKind` enum for `kind`.
- **Anchor semantics**: `@Name` is a global logic variable (per "Scoping
  (resolved)" in the plan) — the binding the member `Name` resolves to. MVP:
  resolve anchor members first (topologically), then cross-ref members against
  their resolved bindings.
- **Resolution**: owner-graph-only via `referencer_for` / `alias_owner_for`, plus
  a `kind` filter (`stmt_kind`, already in the EDB) for the cases where several
  declaring owners reference one anchor. Categorical / fail-closed.
- **Plumbing — the architectural step**: cross-ref resolution needs a resolution
  _context_ (owner graph + already-resolved anchor bindings) that the per-member,
  per-chunk `SelectorResolver` trait does not carry. Step 1 therefore finishes
  with a small **global-solve pass** (resolve anchors, then cross-refs) — the seed
  of the one-solve P4/P5 want. This is the larger remaining chunk; the kernel and
  the real-data gate are done.
- Distinguishing `calls` (a call) from a bare `references` needs AST-level facts
  and is a later refinement; owner-graph `references` + `kind` covers the MVP debt
  shapes.

**Grounding (real metaNode cases).** A read-only sweep of the actual
`metaNode.yaml` (gaffer-private) found the debt is **not monolithic** — the three
explicitly-commented cases split into three resolution sub-patterns, only one of
which the owner-graph-only MVP handles:

| case (minified)                          | shape                              | anchor                                                         | MVP?                                                                                 |
| ---------------------------------------- | ---------------------------------- | -------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| `isMeetingTranscriptionProvider` (`UBt`) | `function UBt(n){ return EBt(n) }` | `EBt` = spec member `isTranscriptionProvider` (another module) | ✅ delegator → spec member; `references @Name`, kind `function_declaration`          |
| `CardsViewAccessor` (`Uee`)              | `class Uee extends Ye {}`          | `Ye` = spec member `NodeAttributeAccessor`                     | ⚠️ superclass ref **and** empty body — several children extend `Ye`, so needs Step 3 |
| `generateUniqueId` (`ls`)                | `function ls(){ return rq() }`     | `rq` = **internal** binding, no spec member                    | ❌ no `@Name` until `rq` is lifted into the spec                                     |

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

### Step 2 — `reads_member`

- **Impact:** 72 TS codegen helpers pinned by name today; their stable identity
  is "the function that reads member `.X` off the codegen context."
- **Needs:** a member-read fact (`reads_member(owner, member_name)`) in the EDB,
  derived from the AST facts already extracted in `chunk_facts`.

### Step 3 — `member_of_module` (use-site edge)

- The first selector kind that needs a **use-site** edge, not just the owner
  graph: "the export consumed as `mod.X` at a call site." Larger because it pulls
  use-site references into the relational model.

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
