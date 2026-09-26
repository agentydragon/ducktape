# Plan: selector language

A selector's job is to name a minified entity by something that survives
re-minification. Shape selectors (`source_match`) do that when the entity has a
distinctive body. When it does not — a bare delegator, one of twelve
byte-identical helper copies, a re-export alias — the only surviving identity is
a **relation** to something else in the program.

Relational selectors resolve in the one joint solve described in
<../docs/selector_resolution.md>; this plan is the remaining **language** work:
which relations and template forms the selector surface can express. Build a
form only when it unlocks synthesis, stabilization, repair, or porting, and test
it on generic synthetic fixtures.

Notation: `@Name` means "the entity another spec member pins as `Name`".

## Remaining work

**R1 — Negation.** No selector can say "and lacks `dispose`" or "nothing
references it". Positive-only matching leaves the discriminating evidence
unusable for the entities that have no positive distinguishing feature.

**R2 — Counting and uniqueness.** "The _only_ exported class extending
`@BaseConfig`" is a count predicate over the graph. Today "exactly one" is
per-selector existence over the chunk body, which is a different statement.

**R3 — Transitive closure.** No reachability predicate, so "the registry plus
its whole eager-use cone" is inexpressible.

**R4 — Shape and relation in one member.** A template can name a pinned entity
(<../docs/selectors.md> § Naming other entities) and claim an entity through a
statement that uses it (§ Pinning by use site), so "has this shape and
references `@Anchor`" is expressible. `MemberSelector` is still a one-of between
a template and a relational kind (`cross_ref`, `reads_member`, …): "emits this
literal **and** is imported by `@settingsModule`" needs conjunction across the
two.

**Template-language gaps.**

- **Contextual sugar.** A template already spans adjacent statements, and
  synthesis reads off a stable immediate neighbor into a 2-statement
  `source_matches[]` window. Missing: readable `before` / `after` / `near` sugar
  for hand-authored windows, and non-adjacent or enclosing-call-site context.
- **Constrained holes.** A way to say a hole appears only as a specific
  argument, callback body, object property value, or statement-list slot,
  without scanning unrelated subtrees; ambiguous matches stay hard errors.

## Execution contract

- **Build/test gate**: `bbr test //devinfra/js/debundle/...` green.
- **Faithful or unsupported**: each construct compiles to constraints provably
  faithful to the relation's documented meaning, or reports `unsupported`. No
  silent fallback, no under-constrained lowering.
- **Real-spec conversion gate**: after converting a downstream selector from a
  name pin to a structural or relational selector, generated output stays
  byte-identical and the converted selector resolves to the same binding the
  name pin did.
- **Abort bar**: if a relation will not admit one general faithful encoding,
  stop and write the dead-end analysis. Do not add a special-case resolver.

## Acceptance cases

Name pins workers still cannot convert: one target inside a mixed `let`/`const`
run, told apart by family evidence rather than position; and state slot /
setter / getter families.
