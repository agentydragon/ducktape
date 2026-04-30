# Humanlike-output gaps in the Excalidraw reference

Observations from the current golden output of the debundler on the
Excalidraw bundle. Grouped by how mechanical the fix would be. Examples
cite line numbers in the golden files in this directory.

## Mechanical (no semantic inference needed)

### Promote pure init wrappers to top-level statements

`excalidraw_canvas_interactions.js:6-14` declares `let ree` and assigns
inside `__dt_generated_init__...()`:

```js
let ree;
export function __dt_generated_init__atomic_module_0001__ree() {
  ree = (e, t, value) => t in e ? iee(e, t, { ... }) : e[t] = value;
}
```

The initializer is a pure arrow function with no side effects, so the
`let`/init-wrapper indirection is unnecessary. Equivalent:

```js
export const ree = (e, t, value) => t in e ? iee(e, t, { ... }) : e[t] = value;
```

The const-promotion pass already handles literal/object/array initializers;
extending it to pure function expressions and `class` literals (with no
TDZ-sensitive references) would collapse most single-stage wrappers.

### Inline trivial property-access aliases

`excalidraw_app_state.js` is one line of substance:

```js
const iee = Object.defineProperty;
export { iee };
```

The alias exists to save bytes during minification. Post-debundle there's
no value in keeping it: the only consumer
(`excalidraw_canvas_interactions.js:8`) could call `Object.defineProperty`
directly, and the entire `excalidraw_app_state.js` module disappears.

A pass that detects `const X = <member-expression-on-builtin>;` and inlines
the references would eliminate dozens of similar single-line modules.

### Rewrite `!0` / `!1` to `true` / `false`

`excalidraw_canvas_interactions.js:10-11` (`!0`),
`excalidraw_element_mutations.js:36`, `:58`, `:118`, `:158`, etc. The
minifier's `!0`/`!1` substitution has no semantic effect; printing them
back as `true`/`false` is a one-pass AST rewrite.

### Rewrite `void 0` to `undefined`

`excalidraw_scene_restore.js:7,15`. Same rationale.

### Sequential statements instead of comma-cascades

`excalidraw_scene_restore.js:5-17`:

```js
function WO(type, t, props) {
  var key = null;
  if (props !== void 0 && (key = "" + props), t.key !== void 0 && (key = "" + t.key), "key" in t) {
    props = {};
    for (var r in t) r !== "key" && (props[r] = t[r]);
  } else props = t;
  ...
}
```

The chained `&&` and `,` operators in the `if` condition are minifier
output. Splitting them into sequential `if`/assignment statements would be
a deterministic rewrite for any `(a, b)` expression in statement position.

## Medium difficulty (needs local analysis)

### Collapse `O(Receiver, "name", value)` calls to property assignments

`excalidraw_element_mutations.js:25-27`:

```js
O = (e, t, n) => (ree(e, typeof t != "symbol" ? t + "" : t, n), n);
```

This is `__defProp`. Used 60+ times in the same file as
`O(Re, "DEBUG_LOG_TIMES", !0)`, `O(Ru, "set", ...)`, etc. Each call is
equivalent to `Re.DEBUG_LOG_TIMES = true` (modulo
descriptor.writable/enumerable/configurable, which are always `true` in
the helper). Detecting that a single bound name is `__defProp`-shaped and
rewriting calls to direct assignments would replace ~half the lines in
this file.

A further step: when the receiver is a class declared in the same module
and all `O(C, ...)` calls happen synchronously after the class body, lift
them into `static` class fields:

```js
class Re {
  static DEBUG_LOG_TIMES = true;
  static TIMES_AGGR = {};
  static scheduleAnimationFrame = () => { ... };
  ...
}
```

### Multi-stage init merging

`excalidraw_element_mutations.js` exports four init functions
(`__dt_generated_init__atomic_module_0002__Er_Mse_O_stage_0`..`_3`). Each
stage runs sequentially with no interleaved external code; the chunking
exists because the source had unrelated initialization passes back-to-back.
Merging stages that have no external observers (i.e. nothing imports
`stage_2` separately) into a single init function would halve the export
surface and the entry-side `init();` calls.

### Modernize `for (var n = 0; n < t.length; n++)` to `for (... of ...)`

`excalidraw_history_stack.js:6`. When the loop body uses only `t[n]` (and
never `n` itself), the rewrite is `for (const item of t)`. Detect: index
variable used only in `t[index]` accesses, no break/continue at the index
level.

### Inline single-use `module.exports` shims

`excalidraw_ui_actions.js`:

```js
import { jO } from "./atomic_module_0004__R5_WO_jO.js";
const Ww = jO.exports;
export { Ww };
```

`jO` is a CommonJS-style `{ exports: {} }` placeholder. The producer
(`excalidraw_scene_restore.js:30`) does `jO.exports = R5`. If the producer
is the only writer and the consumer is the only reader, the indirection
can be replaced with a direct re-export of `R5`'s named members.

## Hard / out of scope for deterministic transforms

### Semantic renaming of scrambled identifiers

`Re`, `Ru`, `O`, `T1`, `Mse`, `vP`, `_P`, `wP`, `v9e`, `TP`, `U8`, `Er`,
`jO`, `R5`, `WO`, `Ww`, `lee`, `oee`, `ree`, `iee` — none of these names
carry meaning. Inferring that `Re` is a `PerfTracker`, `WO` is `jsx`, `R5`
is the React JSX runtime export object, etc., requires looking at usage
patterns or matching against known npm packages. Out of scope for an
AST-only pass; a job for a separate vendor-detection / LLM-rename layer.

### Recovering original module boundaries and named exports

The current output groups bindings by atomic boundary, but the original
source likely had `class PerfTracker` in one file and the localStorage
helpers (`Ru`) in another, exported under semantic names. Reconstructing
those splits requires inferring authorial intent, not just dependency
analysis.

### Snapshot-lowered self-references

`excalidraw_element_mutations.js:29-54` uses
`__dt_selected_module_snapshot__owner_00734 = (() => { const T1 = ..., Re = class Re { ... }; return { T1, Mse, Re }; })()`
to handle the forward references inside `class Re`'s body that point to
sibling `T1` and `Mse`. The IIFE-then-destructure pattern is correct but
ugly; expressing the same intent in idiomatic JS would require either
hoisting all three into a function scope or using a class with deferred
property assignment. Both are larger structural rewrites than the current
deterministic lowering supports safely.
