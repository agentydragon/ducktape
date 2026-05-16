# Oversize closure patterns in Tana web debundle

**Data source:** `/tmp/plan-work.json` from
`bazel-bin/tana/re/web/spec/debundle_78d928dca7.out/analysis/logical_modules/static/index-DI2GynTv/owner_graph.json`
(snapshot `78d928dca7`, after the ducktape `ff6fca0` dedup).

**Headline numbers**

| Metric                                                  | Value                                              |
| ------------------------------------------------------- | -------------------------------------------------- |
| `diagnostic_counts.exceeds_size_cap`                    | 2 751                                              |
| Distinct `(start_line, end_line)` ranges                | 1 048                                              |
| Diagnostics with cycle_blocker_count ≤ 3                | 889 (32 %)                                         |
| Residual owners                                         | 3 598                                              |
| Active (claimed) input bindings                         | 6 079                                              |
| Top diagnostic span                                     | lines 30 775 → 203 963 (15 186 lines, 777 members) |
| Distinct cycle-blocker owners across diagnostics        | ~120                                               |
| Number of "celebrity" blockers (≥ 400 diagnostics each) | ~30                                                |

Almost every top diagnostic is a _sliding window_ of the same enormous SCC
(start_line = 30 775, growing right edge). Once a small number of "celebrity"
blockers are reclassified or claimed, the giant component dissolves and the
diagnostic count collapses. Below: each of the seven distinct patterns that
actually causes those celebrity blockers, with fingerprints, code slices,
root-cause analysis, and a concrete ducktape improvement proposal per pattern.

The two highest-leverage interventions are (a) the **comma-chain `var_decl`
splitter** (P-1) — which on its own would shrink the residual horizon by an
estimated 800+ diagnostics — and (b) **statement-local purity for
`new ClassConstructor()` and `Object.freeze()` patterns** (P-2 + P-7) — these
unstick ~500 hub bindings each. Both are mechanical and don't require
reasoning across statements.

---

## P-1. `comma_chain_var_decl_eager_sibling_use`

**Fingerprint:** owners `5158`–`5161`, line 106 642–106 657, single
`const … = …,  … = …,  … = …,  … = …;` statement. The factorizer correctly
splits each `=`-binding into its own owner so they can be claimed
individually, but every sibling carries an `eager_use` edge back to the
preceding sibling because they share the same JS statement
(`constrains_init_order = true`).

Recurring celebrity blockers of this shape: 5158, 5159, 5160, 5161, 5121
(big array literal), 4943, 5090, 5227, 3826, 3987 (32 hits each across the
top 30 blockers). Each blocks 480+ residual peel attempts because every
diagnostic that reaches into this window sees the chain as a single SCC.

**Representative source (owner `5158`, line 106 642):**

```js
const n2 = h0("Next calendar node", 1, (n) => !Y($.globalPrevNext, n.nodeSpace.appUser) && n.nr_isCalendarDateNode),
  r2 = h0("Previous calendar node", -1, (n) => !Y($.globalPrevNext, n.nodeSpace.appUser) && n.nr_isCalendarDateNode),
  s2 = h0(
    "Next node",
    1,
    (n) =>
      Y($.globalPrevNext, n.nodeSpace.appUser) &&
      (n.nr_isCalendarDateNode || (n.nr_hasTemplate && n.nr_hasTitleExpression))
  ),
  o2 = h0(/* … */);
```

**Edges (debundle_agent_cli explain owner:5159):**

```text
out: lazy_use → owner:79 ($),  lazy_use → owner:100 (Y),  eager_use → owner:5157 (h0)
in:  eager_use ← owner:5162 (r2)   ← constrains_init_order: true
```

`5162 → 5159` is the false eager edge: it only exists because the AST emits a
sibling pointer for "previous decl in this `VariableDeclaration` AST node".
Each individual `=`-binding is a pure RHS expression with no observable order
dependency on its left sibling beyond the same statement boundary.

**Why ducktape can't break it apart today:** statement-level chunking treats
the comma-chain as one `VariableDeclaration` with multiple `VariableDeclarator`s.
The owner-graph builder splits owners per declarator (good), but the eager-use
edge for `constrains_init_order` is taken from the enclosing statement node, not
from the AST analysis of the individual declarator expression. So all
declarators in a single `const a = …, b = …, c = …;` end up in one strongly
connected component.

**Proposed ducktape improvement:** _Per-declarator statement splitting for
side-effect-free `VariableDeclaration` chains._ Concretely, in
`owner_graph::statement_split` (or equivalent — analogous to the recent C-3
decorator-block recognition):

1. When a `VariableDeclaration` node has multiple `declarator` children, evaluate
   each declarator's RHS for purity independently.
2. If every RHS is `purity=pure` and no declarator references a _preceding_
   declarator's binding by name, mark the declarators as _statement-coequal_
   instead of _statement-sequenced_ — i.e. emit owners with no inter-sibling
   `eager_use` edges.
3. Keep the existing behavior for chains where any sibling references
   a name introduced earlier in the same chain (e.g. `const a = f(); const b = a + 1;`
   if expressed as one chain).

Expected impact: collapses the contiguous-residual chains 5158–5161, 3826–3829,
5121-block, 4943-block, etc. — eliminates ~12 celebrity blockers, which
participate in 480+ diagnostics each.

---

## P-2. `singleton_instance_with_listener_blocking_pure_alias`

**Fingerprint:** owner `1080` (line 30 436, `const vt = uJ;`, _pure_,
incoming edges = 30, outgoing = 1 ⇒ `eager_use → owner:1078`). Owner
`1078`/`1079` are the impure singleton + side-effect listener; owner `1080`
is a 1-line _alias_ re-export. `1080` is a blocker in 536 diagnostics.

**Representative source (owner `1078`–`1080`, lines 30 434–30 436):**

```js
const uJ = new cJ();
typeof window < "u" && window.addEventListener("resize", () => uJ.resetData());
const vt = uJ;
```

**Edges:**

- `owner:1078` — `var_decl`, `purity=not_pure` (`unknown_new: cJ`) ⇒ residual
- `owner:1079` — `side_effect`, `purity=not_pure` (`unknown_call: window.addEventListener`) ⇒ residual
- `owner:1080` — `var_decl`, `purity=pure`, `eager_use → 1078`, 30 incoming
  lazy uses. ⇒ residual (blocked by 1078)

**Why ducktape can't break it apart today:** the `vt = uJ` alias is itself
pure, but its RHS reads the binding `uJ` which was declared by an impure
statement. The current owner-graph treats _any_ read of an impure-statement's
binding as `eager_use` because the impure statement's _side effects_ may not
yet have happened at module-init time.

That's correct for `cJ()` (the constructor may have side effects), but the
_aliasing_ `vt = uJ` doesn't _trigger_ `cJ()` — `uJ` is a value-typed binding
holding a pre-constructed instance. The alias is shape-equivalent to an
`export { uJ as vt };` re-export, which has no init-order semantics.

**Proposed ducktape improvement:** _Trivial-alias purity exception._ When a
`var_decl` owner's RHS is exactly an `Identifier` (no MemberExpression, no
Call, no `new`, no array/object literal), treat it as a re-export shim:

1. Demote `eager_use` to `lazy_use` for the binding read.
2. Allow the alias owner to peel independently into any logical module that
   claims either the source binding or the alias.

This is a 5-line check in `purity_analyzer.rs` (or wherever the edge classifier
sits). It eliminates ~50 unique alias-blocker owners and unsticks the
30+ diagnostics each one anchors.

A safer variant: also require the source identifier to resolve to a
`var_decl` statement (not a function expression or class). Then it's
demonstrably a value re-export, not a deferred-call alias.

---

## P-3. `class_extends_residual_base`

**Fingerprint:** owners `6030` (37 lines), `9168` (22), `9191` (226),
`9201` (67), `9206` (61), `9215` (18), `6518` (3) — all `class_decl`,
`purity=pure`, with exactly one `eager_use → owner:1855` (binding `Il =
PopoverBase_tentative`) or `eager_use → owner:2376` (binding `Jl =
NodeSpaceStore`). The base class is itself in residual.

Together these account for the seven highest-frequency class blockers
(each appears in 536 diagnostics).

**Representative source (owner `6030`, lines 125 018–125 054):**

```js
class xat extends Il {
  constructor(e) {
    (super(e.nodeSpace.focusService), (this.appState = e));
  }
  get rootFile() {
    /* … */
  }
  show(e, { operationTrigger: t } = {}) {
    /* … */
  }
  async hide() {
    /* … */
  }
  saveContentAndHide({ operationTrigger: e } = {}) {
    /* … */
  }
  /* … */
}
```

**Edges (owner:6030):** `out: eager_use → owner:1855 (Il)` +
`lazy_use → owner:79`.

`Il` itself (owner:1855, `class Il { … }`) is _pure_, but it's a
1400-line dependency target sitting in residual because _its_ dependencies
(decorate calls on its prototype, MobX `gt(this)`) chain back into the
giant SCC.

**Why ducktape can't break it apart today:** ES2015 `class Foo extends Bar
{ … }` is treated as a single statement with one `eager_use` edge for `Bar`
(must be available at class-definition time). That's strictly correct.
However the class _body_ (methods) does not actually evaluate at definition;
only the `extends` clause does. The factorizer therefore treats the entire
class declaration as eagerly dependent on the base class, and the base
class drags it into residual even though all the methods are lazy.

**Proposed ducktape improvement:** _Split-aware `extends` edge classification._
The class declaration `class A extends B { method1; method2; … }` should
expand to two owner nodes (or one owner with two edge sets):

1. A _binding owner_ that needs `B` at class-init time — `eager_use → B`.
2. A _body owner_ that defines the method bodies — `lazy_use → B`.

This is analogous to the existing C-3 decorator-block recognition but for
the `extends` clause. A simpler version: keep one owner per class, but flag
the `extends`-derived eager edge as _value-transit_ (Bar's _constructor_ is
referenced, not its identity). When the base class is itself pure and has no
constructor side effects, demote the eager edge to lazy.

Even better: when the destination spec already claims the base class to a
real module (e.g. via `binding-patch` for `Il`), automatically rewrite the
`extends Il` reference to an `import { Il } from "..."` at peel time — the
edge then becomes a cross-module import, not a same-statement dependency.

Expected impact: unsticks all seven class blockers (≥ 3 760 diagnostics) and
similar `Il`/`Jl`-extending classes throughout the bundle.

---

## P-4. `transitive_decorator_legacy_metadata_chain`

**Fingerprint:** owner `2382` (line 52 590, `class bg extends Jl { … }`, 9
lines) immediately follows a run of `qx([oe], Jl.prototype, "…", 1);`
calls (legacy `__decorate` emit for MobX `@observable`/`@action` decorators
applied to the base class). 493 diagnostics list owner `2382` as a blocker;
the immediately preceding decorate calls (owner `2377`–`2381`) carry
`side_effect` purity and chain into the giant SCC.

**Representative source (owners 2377–2382, lines ~52 580–52 600):**

```js
qx([Z], Jl.prototype, "_availableWorkspaces", 2);
qx([Pe], Jl.prototype, "recentEntities", 1);
qx([oe], Jl.prototype, "getNodeOrPlaceholder", 1);
class bg extends Jl {
  publicPreviewState;
  publicState;
  lazyLoadIfNeeded(e, t) {
    const r = typeof e == "string" ? e : e.id,
      s = this.nr_getNodeIfInMemory(r);
    s?.shouldBeLoading && s?.markAsFailedToLoad();
  }
}
```

**Edges (representative for `bg`):** `eager_use → owner:2376 (Jl)`, plus
implicit "must run after preceding statements" relation that puts `bg`
_after_ the entire decorate cluster.

**Why ducktape can't break it apart today:** the C-3 decorator-block
recognition (mentioned in the task brief) special-cases the _TypeScript
class-decorator emit_ (`__decorate([D], C)` immediately _before_ the class
that's being decorated). But the _prototype-method decorator_ emit pattern
is `__decorate([D], C.prototype, "method", N);` and runs _after_ the class
declaration. The current detector binds these to the _next_ class declaration
in source order, which is wrong — they belong to the _previous_ class.

So `class bg` (owner:2382) gets pulled into the same atomic unit as the
preceding `qx(... Jl.prototype ...)` calls, even though the decorators
target `Jl`, not `bg`.

**Proposed ducktape improvement:** _Method-decorator emit detection._ Extend
the C-3 recognizer with a _post-class_ variant:

1. Recognise call patterns of shape `<dec>([..._], <ClassName>.prototype, "<key>", <kind>);`
   immediately following a `class <ClassName> { … }` declaration.
2. Group those decorate calls with the _preceding_ class (not the next one).
3. Emit them as a single atomic owner with the class so that the class +
   its method-decorators peel into one module.

Concretely the spec annotation could be a new statement-kind tag
`statement_kind: class_with_prototype_decorators` that wraps the class plus
the contiguous run of `<member>.prototype` decorate calls. The class still
exposes one binding (the class identifier) but the decorate side effects
ride along.

Expected impact: removes the false separation that puts ~50 MobX-store-style
classes into residual.

---

## P-5. `zod_schema_constructor_chain`

**Fingerprint:** owners `3639`, `3640`, `3641` (lines 75 340–75 342). Three
sibling `const X = ee({ … })` Zod schema declarations with `unknown_call`
purity from the `ee` (likely `z.object`) constructor and from `ht(...)` /
`k()` / `ve()` helpers. All three are blockers in 536 diagnostics.

**Representative source:**

```js
Et(["createNewSubscription", "updateExistingSubscription", "AICreditCheckout"]);
const o5e = ee({ flow: ht("updateExistingSubscription"), tanaProductKey: xB, initiator: kO, priceId: k() }),
  i5e = ee({ flow: ht("createNewSubscription"), tanaProductKey: xB, initiator: kO }),
  a5e = ee({ flow: ht("AICreditCheckout"), quantity: ve(), tanaProductKey: xB, initiator: kO });
Vg("flow", [a5e, o5e, i5e]);
```

**Edges:** each schema has `eager_use → ee` (vendor binding `z.object`) +
`eager_use → ht/k/ve` + `sequenced → Vg(...)`. They form a single SCC with
the registration side effect at the end.

**Why ducktape can't break it apart today:** `ee(...)` and `ht(...)` are
treated as `unknown_call` (not_pure) because the analyzer can't prove they
have no side effects. The `Vg("flow", [...])` call at the end is a registry
side effect that consumes the three schemas — `sequenced` edges block
peeling because they enforce a global order.

**Proposed ducktape improvement:** _Spec-driven function-purity allowlist._
A spec-level annotation (in `vendor_marks.yaml` or a new
`pure_call_marks.yaml`) that names bindings whose calls are _value-typed
constructors_ and can be treated as `purity=pure` despite being
`unknown_call`. Concretely:

```yaml
# vendor_marks.yaml — extension
pure_constructors:
  - binding: ee # z.object
    module: "vendor/zod"
  - binding: ht # z.literal
    module: "vendor/zod"
  - binding: k # z.string
    module: "vendor/zod"
  - binding: ve # z.number
    module: "vendor/zod"
```

A pure call to a known-pure function does not produce an `eager_use` —
it produces a `lazy_use` to the constructor binding and no side-effect
horizon. This lets the three schema decls peel independently and the
`Vg(...)` registration can stay in residual as a wiring statement (which is
where it belongs anyway).

Expected impact: unsticks ~150 Zod-schema-style closures that follow this
shape (search for `unknown_call` on bindings used 20+ times — most are
`z.something()` or `i18n.translate()` patterns).

This generalises into a broader **"function-effect classifier" stage** —
analogous to a pure-function attribute in Rust — that runs after vendor
analysis. The current binary `pure | not_pure` distinction is the dominant
cause of false residual.

---

## P-6. `hub_factory_function_with_many_callers`

**Fingerprint:** owner `1115` (line 30 808, `function Se(n) { return new Fx(...); }`,
`purity=pure`, **184 incoming uses**, 3 outgoing lazy uses). Exported as
`defineNodeCommand`. The function itself is small (6 lines) and pure, but
nothing in the spec claims it, so it stays in residual; meanwhile 184
downstream call sites in residual depend on it, which gives it a cycle
participation count rivalling base classes.

**Representative source (owner `1115`, lines 30 808–30 813):**

```js
function Se(n) {
  return new Fx(n, new eL(), ({ node: e }) => ({
    [yt.NODE_SPACE_SIZE]: e.nodeSpace.spaceSize,
    [yt.NODE_SPACE_UNPARSED_SIZE]: e.nodeSpace.unparsedSpaceSize,
  }));
}
```

Note this is one of _several_ similarly-shaped factory functions at
30 808–30 873 (`Se`, `uf`, …) — see the surrounding lines. Each produces a
parameterised UI command and is intended to be called from a `commands/*.yaml`
module.

**Why ducktape can't break it apart today:** the spec currently has no module
that claims `defineNodeCommand`. Without a claim, the factorizer leaves it in
residual. Once it lands in residual, its 184 callers can't be peeled
_unless they don't reference it at all_, because each call site picks up an
`eager_use → owner:1115` edge that points into residual.

**Proposed ducktape improvement:** _Diagnostic: unclaimed pure hub function._
Add a new diagnostic kind `unclaimed_pure_hub` for any pure owner with
≥ N (say, 20) incoming uses and no spec claim. Surface these in the
`plan-work` output so spec writers can prioritise claiming them.

Concrete prioritised list output:

```text
unclaimed_pure_hub: owner:1115  binding=Se  export=defineNodeCommand  in_uses=184  lines=30808–30813
unclaimed_pure_hub: owner:391   binding=_A  export=findOrCreateDayNodeSync  in_uses=3 …
```

This is a _non-mechanical_ fix on the spec side, but the diagnostic itself
unblocks the spec author. Optionally: auto-propose a one-binding logical
module `app/commands/framework/define_node_command.yaml` for each
unclaimed pure hub (debundle*plan_work could emit it as a \_suggested
proposal*).

Expected impact: this is the single highest-value spec edit. Claiming
`defineNodeCommand` plus 5–10 similar factories (`hO` for calendar
factories at owner:2696, `h0` for prev/next at owner:5157, the `Fx`
constructor at owner:1114) would let the 200+ commands they produce all
peel into per-command modules.

---

## P-7. `frozen_object_table_literal`

**Fingerprint:** owner `5121` (line 105 925, `var_decl`, _pure_, length 26
lines) and similarly owner `5284`, owner `4940` (`unknown_call: Ci(...)` —
a React `memo` wrapper). Owner `5121`'s body is a _large array literal_ of
identifiers:

```js
const Rre = [
  MXe,
  RXe,
  LXe,
  jXe,
  UXe,
  BXe,
  WXe,
  VXe,
  $Xe,
  HXe,
  GXe,
  zXe,
  YXe,
  qXe,
  KXe,
  JXe,
  XXe,
  QXe,
  ZXe,
  eQe,
  tQe,
  nQe /* … */,
];
```

Each identifier (`MXe`, `RXe`, …) is the result of an earlier `var_decl`
chain that registered a UI command. Owner `5121` is _pure_ (just an array
construction), but ducktape gives it an `eager_use` edge per element ⇒ the
array brings ~25 owners into one SCC.

**Why ducktape can't break it apart today:** array-literal RHS is treated as
`eager_use` on every named element because the elements must be evaluated
to assemble the array. That's strictly correct for _evaluation order_, but
the resulting array is a passive table — no element triggers another.

**Proposed ducktape improvement:** _Demote eager → lazy for table-shape
array literals._ Specifically: when a `var_decl` RHS is a literal
`ArrayExpression` containing only `Identifier`/`Literal` elements (no
function calls, no spreads except of an already-eager binding) and the
resulting variable is read in `lazy_use` contexts only, demote the
per-element edges to `lazy_use`. The element values were already evaluated
_before_ this statement (by their own declarations); reading them into an
array is observationally identical to evaluating them by name.

Spec annotation alternative: a new field on the YAML module level:

```yaml
- name: AllCommands
  binding: Rre
  table_of: [MXe, RXe, LXe, …] # promises analyzer it's a pure table
```

This pattern occurs in command registries (`Rre`, top of `commands/all`),
key-binding tables, and i18n tables — easily 100+ occurrences.

---

## P-8 (uncertain). `array_spread_of_map_keys`

**Fingerprint:** owner `3987` (line 82 887, _one-liner_, `purity=not_pure
(array_spread)`, blocker in 537 diagnostics): `const qV = [Cf, ...Ax.keys()];`.

The analyzer tags `array_spread` impure because `[...Ax.keys()]` triggers
`Ax.keys()` which is `unknown_call`. The resulting value is a static
read-only snapshot of `Ax` at this point in time.

**Confidence:** medium. This is a real pattern but only ~5–10 owners across
the bundle look like this. I couldn't confidently demonstrate that fixing
it would unstick more than the immediate diagnostics, because the SCC it
participates in is dominated by P-2 / P-3 anyway. Listed for completeness.

**Possible improvement:** treat `[...X.keys()]` / `[...X.values()]` /
`[...X.entries()]` for known Map-like bindings as `purity=pure` if `X` is
itself pure. Requires the function-effect classifier from P-5.

---

## Patterns I couldn't confidently identify

- The `static_initializer_chain` pattern was hypothesised in the task brief
  but I didn't find a _clean_ example — the closest I have is the comma
  chain (P-1), which is functionally equivalent.
- The `template_string_table` pattern: I see large literal arrays (P-7) but
  not obviously template-string-driven ones.
- I never confirmed the `enum_with_dependency_methods` pattern — the
  closest is the Zod-schema chain (P-5).
- Of the 27 cycle blockers for the single largest diagnostic (777 members),
  20 fit one of P-1 through P-7 above. The remaining 7
  (owners 2624, 2626, 4940, 4943, 5133, 5158, 5161) are smaller-scale
  instances of the same patterns.

---

## Recommended PR sequence (impact-ordered)

1. **P-1 comma-chain splitter** — pure ducktape change, no spec edits,
   estimated 800+ diagnostics resolved. Pre-requisite for everything else
   because the comma-chain dominates the high-frequency blocker list.
2. **P-2 trivial-alias purity exception** — ~10-line ducktape change,
   no spec edits, unsticks ~50 alias owners.
3. **P-6 `unclaimed_pure_hub` diagnostic** — new diagnostic in
   `plan-work`. Doesn't fix anything mechanically; surfaces the work for
   spec authors. Unblocks the highest-leverage spec edits
   (`defineNodeCommand` ⇒ 184 callers).
4. **P-3 split-aware `extends` edge** — moderately invasive ducktape
   change, removes ~7 celebrity blockers worth 3 700+ diagnostics. Best
   approached after P-1 + P-2 land so the SCC has already shrunk and
   verification is cheaper.
5. **P-5 vendor `pure_constructors` allowlist** — small ducktape change +
   spec data entry. Generalises into the broader function-effect
   classifier and gives spec authors a knob to tune.
6. **P-7 table-shape array literal demotion** — ducktape change,
   ~100 closure unsticks.
7. **P-4 post-class decorator detection** — extends C-3 mechanically;
   ~50 MobX-style classes resolved.

The dominant pattern across all seven is the same: **the analyzer is
correctly conservative about side effects at statement granularity, but
the actual code unit that needs to peel is finer-grained than a statement.**
Three of the proposals (P-1, P-2, P-7) push purity analysis below the
statement; the other four (P-3, P-4, P-5, P-6) add either spec annotations
or new diagnostics to communicate "this thing is pure even though it looks
side-effecting."
