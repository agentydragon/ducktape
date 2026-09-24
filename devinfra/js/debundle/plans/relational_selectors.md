# Plan: relational selector language

A selector's job is to name a minified entity by something that survives
re-minification. Shape selectors (`source_match`) do that when the entity has a
distinctive body. When it does not — a bare delegator, one of twelve
byte-identical helper copies, a re-export alias — the only surviving identity is
a **relation** to something else in the program.

The engine that resolves relations already exists and is described in
<../docs/selector_resolution.md>: every selector kind compiles into one IR
program over every chunk, solved jointly (one CP-SAT request per group of
interacting entities), with `all_different` across
claimed targets. This plan is the remaining **language** work: which relations
the selector surface can express.

Notation: `@Name` means "the entity another spec member pins as `Name`".

## Shipped

These relational selector kinds lower natively into IR atoms over `chunk_facts`
and participate in the joint solve:

`cross_ref` (`references` / `aliases`), `reads_member`, `member_of_module`,
`passed_to_call`, `makes_decorate_call`, `intrinsic_alias`.

`all_different` over claimed targets is selector semantics, not a post-hoc
duplicate check: a broad selector can be forced unique because more specific
selectors consumed the other candidates.

## Remaining work

**R1 — Negation.** No selector can say "and lacks `dispose`" or "nothing
references it". Positive-only matching leaves the discriminating evidence
unusable for the entities that have no positive distinguishing feature.

**R2 — Counting and uniqueness.** "The _only_ exported class extending
`@BaseConfig`" is a count predicate over the graph. Today "exactly one" is
per-selector existence over the chunk body, which is a different statement.

**R3 — Transitive closure.** No reachability predicate, so "the registry plus
its whole eager-use cone" is inexpressible.

**R4 — Shape and relation in one anchor.** `MemberSelector` is a one-of: a
member is pinned by shape or by a relation, never by both. "Emits this literal
**and** is imported by `@settingsModule`" needs conjunction across the two
families.

**R5 — `@Name` inside a shape.** A `source_match` that mentions a name another
selector pinned treats it as an alpha wildcard: `const x = new Widget(ANYTHING);`
matches any `new C(…)`, so it is ambiguous or, when only another class is
constructed, silently wrong (<../SELECTOR_BUGS.md>). This is the
template-references step of <selector_engine.md>: an entity name in a template
becomes a table constraint in the joint solve.

## Landing a new relation

1. Add the fact to `chunk_facts` if it is not derivable from what is there.
   Extraction stays fail-closed.
2. Lower it to a table over candidate ids in the resolve engine
   (<selector_engine.md>), with a compiled encoding in
   `selector_constraint_model_builder`.
3. Prove it through `debundle run` on a fixture whose chunk also exports and
   uses the anchor, as <../e2e/cross_ref_lowering_test.rs> does: real graphs
   model `export { … }` and side-effect statements as owners that reference
   every binding they touch, which is the discriminating case bare fixtures miss.
4. Extend `docs/selectors.md` — a selector kind that is not documented there
   does not exist for authors.

## Execution contract

- **Build/test gate**: `bbr test //devinfra/js/debundle/...` green.
- **Faithful or unsupported**: each construct compiles to constraints provably
  faithful to `source_match` semantics, or reports `unsupported`. No silent
  fallback, no under-constrained lowering.
- **Real-spec conversion gate**: after converting a downstream selector from a
  name pin to a structural or relational selector, generated output stays
  byte-identical and the converted selector resolves to the same binding the
  name pin did.
- **Abort bar**: if a relation will not admit one general faithful encoding,
  stop and write the dead-end analysis. Do not add a special-case resolver.

## Downstream evidence

A 2026-06-23 census of the largest downstream spec found 5,583 `source_match`
blocks across 1,751 YAML files, all `identifiers: alpha_all`. It found no uses
of `target_statement`, `target_statements`, or authored
`wildcard_string_literals`.

Workers converting fragile name pins stop where the language needs: inverse
use-site selectors (target-as-call-argument, setter/callback assignment, owner
that reads a stable member); state slot / setter / getter families; identifying
one target inside a mixed `let`/`const` run by family evidence rather than
position; and membership in an object/array roster. These are R1–R5 in
authoring terms — treat them as the acceptance cases, not as separate features.
