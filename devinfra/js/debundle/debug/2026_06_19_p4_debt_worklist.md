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
- **Remaining for Step 1:** surface syntax for the `@Name` anchor + the resolver
  seam that feeds these into real selectors (the kernel is solver-internal so
  far); the AST-level `calls` refinement (reference that is _specifically a
  call_) over the parsed chunk.

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
