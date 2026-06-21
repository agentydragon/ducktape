# P4 expressivity — remaining worklist

Complement to <../plans/selector_constraint_model.md> (design narrative) and
<../plans/selector_resolver_endpoint.md> (endpoint runbook). The remaining
construct-by-construct work to drive the spec's `binding.name` debt toward zero,
ordered by impact-per-unit-effort.

## Where the debt is (measured 2026-06-20, gaffer `78d928dca7`)

`spec selector-debt`: **1665** fragile `binding.name` pins remain (down from ~2198),
against ~5599 structural `source_match` selectors. By layer: features 790, domains 413,
app 309, shared 106, integrations 49, infra 5. The dominant remaining debt is **two
concentrated clusters that X1–X3 cannot reach**, plus a distributed long tail.

## Remaining worklist (impact-ordered)

### `passed_to_call` (`resolves_to`-of-argument primitive) — LANDED for the target-as-argument direction

A `resolves_to`-of-argument selector has two opposite shapes, and investigating the
gaffer `78d928dca7` bundle showed they need **different machinery**:

- **Target is the argument** — `registry.register(Target)`, where `Target` is a
  separately-declared top-level binding distinguished only by being passed to the call.
  This is owner-granular (the target _is_ an owner) and is the documented gap in
  `member_of_module`'s scope. **LANDED** as the `passed_to_call` selector: EDB
  (`chunk_facts::call_argument_uses`, keyed by the argument binding), solve
  (`passed_to_call`/`passed_to_call_from` Ascent rules joining the argument to its
  declaring owner via `name_owner`; `passed_to_call_owner` resolver narrowing by
  `object`/`arg_index`/`kind`), spec (`PassedToCallSelector`), lowering
  (`materialize/passed_to_call.rs` wired through `plan_builder`/`mod.rs`/`plans.rs`),
  and a full-pipeline e2e (`e2e/passed_to_call_lowering_test.rs`) proving the
  registry-distinguished empty class resolves + runs under Node, plus
  object/arg-index disambiguation and fail-closed zero/ambiguous/unknown-object/
  non-identifier cases. The whole `//devinfra/js/debundle/...` suite is green with lint.

  Reach against this bundle: the concrete registry call sites here
  (`ServiceRegistry.register(t)` with `t` a locally-constructed Langium service,
  `componentHandler.register({constructor: t, ...})` in vendor code) pass _locals_, not
  separately-declared top-level bindings, so they are **not** current spec debt. The
  primitive is the mechanism for the documented abort-bar — it retires a
  registry-distinguished _top-level_ target whenever one appears (this bundle, or
  another) — verified by the synthetic e2e mirroring the real `register(Target)` shape.
  No spec pins are flipped here (a separate application pass).

- **Target makes the call** — the `sOe` system-nodes factory (~226–233 getters that all
  read the same `.getNodeOrPlaceholder`, discriminated by the `systemIds.<id>` call
  argument). **ABORT-BAR at owner granularity** — see below. Not retired by
  `passed_to_call`, and not retirable by any X1–X3-style owner-level primitive.

### Debundler helper-recognition — esbuild TS decorate trios (~72 trio pins) — `__decorate` keystone LANDED via `makes_decorate_call`

`__decorate` / `__defineProperty` / `__getOwnPropertyDescriptor` alias members read those
methods off the **global `Object`** → N-way ambiguous, anchor is not a spec member; no
`source_match` can pin them. The bundler emits an identical copy per module, so only the
minified name distinguishes them — they were stuck as fragile `binding.name` pins (measured:
**72** trio members in the gaffer `78d928dca7` spec — 37 `__decorate`, 19 `__defineProperty`,
16 `__getOwnPropertyDescriptor`; the worklist's "~108" predated the measurement). The
`effect: typescript_decorate_helper` annotation (21 sites, all on the `__decorate` member) is
**orthogonal**: it models the decorator-application _call statement_'s local effect and does
**not** retire the helper definition's name-pin (the annotated `__decorate` member still
carries `selector.binding.name`). So this is a ducktape capability gap, not a gaffer
annotation pass.

**LANDED — the keystone `makes_decorate_call` primitive** (the inverse-direction sibling of
`passed_to_call`: the target _makes_ the decorator-application call rather than being _passed
to_ one), the design in <../plans/selector_constraint_model.md> §"the 12 `__decorate` copies".
The `__decorate` helper is pinned by the decorator application it makes —
`H([d], @Class.prototype, "m")` — riding the decorated class `@Class` (a separately-pinned
entity, reached through `resolves_to`) plus the optional source-level member literal, neither
of which a re-minification rewrites. Layers (mirroring `passed_to_call` exactly): EDB fact
(`chunk_facts::decorate_call_uses`), kernel (`selector_solve` `decorate_call` relation +
`makes_decorate_call` rule + `decorate_call_owner` resolver), spec surface
(`MakesDecorateCallSelector` with `class:` required, optional `member:` / `kind:`), lowering
bridge (`materialize/makes_decorate_call.rs` wired through `plan_builder`/`mod.rs`/`plans.rs`),
unit tests at every layer, and a full-pipeline e2e (`e2e/makes_decorate_call_lowering_test.rs`)
proving the byte-identical helper copies resolve by their decorated class, disambiguate two
copies by class + member literal, narrow by kind, and run under Node with decoration semantics
intact. Fail-closed (zero/ambiguous → no-match) and re-minify-proof (so `selector-debt` counts
it as the debt solution, not name-pin debt). The whole `//devinfra/js/debundle/...` suite is
green with lint. **This retires the 37 `__decorate` member pins** once the gaffer spec converts
them (a separate application pass — sequence after Track B to avoid contending on the gaffer
branch).

**LANDED — the two intrinsic-alias companions (~35 pins: 19 `__defineProperty` + 16
`__getOwnPropertyDescriptor`) via `intrinsic_alias`.** Each is
`var X = Object.<defineProperty|getOwnPropertyDescriptor>`, read **only inside** its trio's
`__decorate` helper body (verified: exactly one referencer each). No `source_match` can pin
them — N byte-identical copies, the anchor is the global `Object`, not a spec member. The
invariant anchor is "the `Object.<method>` intrinsic alias **referenced by**
`@<decorateHelper>`", and the `__decorate` helper is now a stable `@Name` because
`makes_decorate_call` pins it. **LANDED** as the `intrinsic_alias` selector, pairing (a)
structural recognition of `var X = Object.<property>` off the **unshadowed** global `Object`
(a fail-closed identity guard in `chunk_facts` — a shadowed/reassigned/imported `Object`
yields no rows) with (b) an inverse-`references` edge ("the alias referenced by owner `@H`",
narrowed by the intrinsic property name, riding the owner graph's own `references` edge that
`cross_ref` uses). Layers (mirroring `makes_decorate_call` exactly): EDB fact
(`chunk_facts::intrinsic_alias_uses`, keyed by the alias binding + the unshadowed-`Object`
guard), kernel (`selector_solve` `intrinsic_alias` relation + `references_intrinsic_alias`
rule joining the alias to its referencer + `intrinsic_alias_owner` resolver narrowing by
property + referencer owner), spec surface (`IntrinsicAliasSelector` with `property:` +
`referenced_by:` both required, `deny_unknown_fields`), lowering bridge
(`materialize/intrinsic_alias.rs` wired through `owner_graph_projection`/`plan_builder`/`mod.rs`/
`plans.rs`; the `referenced_by` anchor reads the `makes_decorate_call`-claimed helper binding
back out of `module_plans` via `claimed_member_bindings`, since the helper is pinned by a prior
post-Stage-A pass, not `add_explicit_request`), unit tests at every layer, and a full-pipeline
e2e (`e2e/intrinsic_alias_lowering_test.rs`) on generic synthetic fixtures that mirror the real
trio shape — two `var X = Object.defineProperty` copies each read by a distinct `@helper`,
disambiguated by `referenced_by` + `property` — proving the companions resolve, two copies
disambiguate by their referencing helper, the property narrows two companions of one helper,
and the emitted tree runs under Node with decoration semantics intact; fail-closed
zero/ambiguous/shadowed-`Object` cases are exercised as rejections. The whole
`//devinfra/js/debundle/...` suite is green with lint. **This retires the remaining ~35
companion pins** once the gaffer spec converts them (a separate application pass). \*\*With this

- `makes_decorate_call`, all 72 decorate-trio pins are now retirable in one gaffer application
  pass.\*\*

### Lane sweeps on the distributed tail (~1330 across families)

Self-anchorable name-pins spread across families (top: domains/graph 159, features/nodes
141, features/transcription 64, features/search 58, shared/ui 50, features/navigation 47).
Convert via self-emitted-literal / rich-signature anchors (the `debundle_stabilize` skill +
the lane recipe <2026_06_20_gaffer_phaseA_lane_recipe.md>); ~25–40% convertible, honest debt
for the rest.

### X4 — counting / uniqueness

`all_different` for duplicate-claim diagnostics and per-target categoricity as a constraint
rather than a post-hoc check. Most useful atop the global solve (X5).

### X5 — one global solve

Shift from per-selector solves to a single CSP over the whole spec (shared logic variables
for `@Name`, `all_different` across targets). The architectural capstone for the fused
`app/bootstrap/initBundle` megamodule (103 pins, all score-100); the conversions and X4 fold
into it.

## Primitive reach (measured against this bundle)

- **X1 `cross_ref`** (`references` / `aliases @Name`): in use (17 sites); delegator/alias
  conversions partially harvested, more remain in the distributed tail. The
  one-`cross_ref`-per-module ceiling is **lifted** — N same-module `cross_ref` members
  (each anchored on a same-module `source_match` anchor) now resolve to distinct
  bindings (see <2026_06_20_cross_ref_multi_member_per_module.md>), so the same-module
  alias clusters held back as `note:` debt in gaffer `metaNode.yaml` are now convertible
  in a follow-up gaffer pass.
- **X2 `reads_member`**: **largely exhausted here — ~7 genuine conversions landed**, not the
  ~72 an earlier estimate assumed. This bundle has no distinctive-per-helper codegen-context
  cluster; the `.X`-reading helpers that exist are mostly the global-`Object` decorate trios
  above (un-pinnable by selectors).
- **X3 `member_of_module`**: **no candidates in this bundle.** The chunk has 6 empty
  subclasses — 5 extend builtin `Error`, 1 extends a bare local identifier — and **zero** of
  the `class X extends mod.Y {}` member-access shape X3 requires (a flat single-chunk Vite
  bundle has no cross-module `mod.Y` superclass). Keep the primitive for other bundles.
- **`passed_to_call`** (target-as-argument): **no current debt in this bundle** — the
  `register(...)` sites here pass locals, not separately-declared top-level bindings. The
  primitive is the landed mechanism for the registry abort-bar; it retires a
  registry-distinguished _top-level_ target whenever one appears. Keep for other bundles.

## Abort bars (document, do not hack)

- **The `sOe` system-nodes factory (~226 getters) — sub-owner granularity, no
  owner-level primitive can reach it.** The getters are object _properties_ inside one
  returned object literal of a single top-level binding
  (`const SystemNodesAccessor = (n) => ({ get coreTemplate() { return
n.getNodeOrPlaceholder(systemIds.coreTemplateId); }, … })`). The spec pins the _whole
  factory_ as one member (`SystemNodesAccessor` → `binding: { name: sOe }`); the getters
  are **not** separate spec members and **not** separate owners, so they are **not** part
  of the 1665 `binding.name` debt — the factory is one pin, not 226. The whole selector
  resolution layer (X1–X3 and `passed_to_call` alike) resolves at _owner_ (top-level
  statement) granularity via `binding_for_owner`, so "pin getter `coreTemplate` by its
  `systemIds.coreTemplateId` argument" has no representable target: there is no owner to
  resolve to, and no spec member to pin. A `passed_to_call`-style "target makes a call
  with a discriminating argument" edge does not help either — every getter's call lives
  in the _same_ owner (`sOe`), already pinned. Retiring per-getter identity needs
  **sub-owner targets** (addressing a member of an object literal as a distinct entity),
  a granularity the model does not have; it is an X5 / finer-granularity concern, not a
  new selector. Documented here as an explicit dead end rather than hacked through.
- A target distinguished only by an _external_ `registry.register(Target)` where `Target`
  is a top-level binding → **retired** by the landed `passed_to_call` selector (above).
- An internal-anchor delegator (`function ls(){ return rq() }` where `rq` is an internal
  binding, no spec member) → stays a name-pin until the anchor is lifted into the spec.
