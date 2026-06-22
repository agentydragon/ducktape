# P4 expressivity — remaining worklist

Complement to <../plans/selector_constraint_model.md> (design narrative) and
<../plans/selector_resolver_endpoint.md> (endpoint runbook). The remaining
construct-by-construct work to drive the spec's `binding.name` debt toward zero,
ordered by impact-per-unit-effort.

## Where the debt is (measured 2026-06-21, gaffer `78d928dca7`, post round-2)

`spec selector-debt`: **989** fragile `binding.name` pins remain (down from ~2198
original / 1665 on 2026-06-20), against **6156** structural `source_match` selectors.
The drop came from round-1 (`domains/*`) + round-2's four lanes (`shared/ui`,
`features/{media,node_selection,tuples,workspace,billing,editor,tags,crm}`,
`app/{shell,state,panel}`). By layer: features 397, domains 275, app 203, shared 64,
integrations 46, infra 4.

The single dominant module is **`app/bootstrap` (99 pins, all score-100)** — the fused
at-init megamodule, an X5 concern (below), not a lane. The rest is a distributed tail
across ~150 families; the ~72 esbuild decorate-trio pins (next section) are scattered
through it. (Name-only census; the source-aware near-ambiguous second-half backlog
needs a separate `--source-file` run.)

## Remaining worklist (impact-ordered)

The three relational primitives this program needed — `passed_to_call`
(target-as-argument), `makes_decorate_call` (the esbuild `__decorate` keystone), and
`intrinsic_alias` (the `__defineProperty` / `__getOwnPropertyDescriptor` companions) —
have **all landed** in ducktape (EDB + kernel + spec surface + lowering + full e2e;
the `//devinfra/js/debundle/...` suite is green with lint). What remains is the gaffer
application of them plus the two global-solve items.

### Apply the decorate-trio conversion in gaffer (~72 pins)

`makes_decorate_call` + `intrinsic_alias` make all 72 esbuild TS decorate-trio pins (37
`__decorate`, 19 `__defineProperty`, 16 `__getOwnPropertyDescriptor`) retirable in one
gaffer application pass: the `__decorate` helper rides its decorated `@Class` (reached
through `resolves_to`, plus the optional member literal); the two companions ride
`referenced_by @<decorateHelper>` narrowed by the intrinsic property name. The
primitives are landed and fail-closed / re-minify-proof — only the spec conversion
remains. The `intrinsic_alias` `referenced_by` anchor now resolves **per logical
module** (esbuild co-locates each helper with its companions), so a generic helper
`name:` repeated across modules — e.g. `applyDecorators`, shared by ~11 modules'
`__decorate` helpers — no longer collapses to ambiguous; the 35 companion pins
(`__defineProperty` + `__getOwnPropertyDescriptor`) are convertible regardless of helper
naming (see <2026_06_21_intrinsic_alias_module_scoped_referenced_by.md>).

### Round-3 lane sweeps (recommended)

The convertible distributed tail, grouped into coherent ~60–90-pin lanes (excluding
`app/bootstrap` and the round-1/round-2 families' honest-debt residue). Convert via
self-emitted-literal / rich-signature anchors (the `debundle_stabilize` skill + the lane
recipe <2026_06_20_gaffer_phaseA_lane_recipe.md>); ~25–40% convertible, honest debt for the
rest.

| lane                | families                                                                                                                       | ~pins |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------ | ----- |
| R3-domains-graph    | `domains/graph` (50) + `domains/search` (17) + `domains/ai` (22)                                                               | 89    |
| R3-domains-tail     | `domains/{template,paste,local_api,focus,calendar}` (14 each)                                                                  | 70    |
| R3-features-nodes   | `features/nodes` (38) + `features/sidebar` (15) + `features/transcription` (14) + `features/publish` (14)                      | 81    |
| R3-features-surface | `features/public_pages` (17) + `features/calendar` (17) + `features/onboarding` (14) + `shared/dom` (15) + `app/commands` (13) | 76    |
| R3-integrations     | `integrations/google` (19) + `integrations/firebase` (12) + the integrations tail                                              | ~46   |

~362 pins targeted. The round-2 lanes' residual (e.g. `features/{media,node_selection,
tuples,workspace}` at 11–14 each) is mostly proven honest debt — not worth re-sweeping
until the decorate-trio application (above) and X4/X5 land.

### X4 — counting / uniqueness

`all_different` for duplicate-claim diagnostics and per-target categoricity as a constraint
rather than a post-hoc check. Most useful atop the global solve (X5).

### X5 — one global solve

Shift from per-selector solves to a single CSP over the whole spec (shared logic variables
for `@Name`, `all_different` across targets). The architectural capstone for the fused
`app/bootstrap` megamodule (99 pins, all score-100); the conversions and X4 fold
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
  of the 989 `binding.name` debt — the factory is one pin, not 226. The whole selector
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
