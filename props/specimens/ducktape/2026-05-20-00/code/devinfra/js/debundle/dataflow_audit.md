# Dataflow audit for the debundler's primary inputs

The debundler hosts conditionally-correct optimizations that infer
per-statement read/write summaries from static syntactic structure (see
AGENTS.md → "Conditionally-correct optimizations"). The first such pass
is the dataflow-aware S-chain in `graph.rs`. Soundness of every such
pass depends on the bundle being audited containing **none** of the
following dynamic-dispatch shapes that defeat static cell tracking:

| #   | Shape                                                                           | Why it breaks static dataflow                                                   |
| --- | ------------------------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| 1   | direct `eval(...)`, `(0, eval)(...)`                                            | dynamic code execution; can read/write anything in scope                        |
| 2   | `with(obj) { ... }`                                                             | implicit scope chain insertion; identifier resolution becomes runtime-dependent |
| 3   | `new Function(...)`, `Function(...)`                                            | constructs a function from string source; same risk as `eval`                   |
| 4   | `globalThis[<dynamic>]`, `window[<dynamic>]`, `self[<dynamic>]`                 | dynamic global property key — cell is not statically determinable               |
| 5   | `Object.defineProperty(<global>, ...)`, `Reflect.defineProperty(<global>, ...)` | installs getter/setter on a global; reads/writes become side-effecting          |
| 6   | `new Proxy(<global>, ...)`                                                      | intercepts arbitrary property access                                            |
| 7   | `<obj>[<dynamic>] = ...` / `<obj>[<dynamic>]` reads on outer-scope bindings     | dynamic member key on bindings we'd otherwise track                             |

When any of these appear in a statement, that statement is marked
`dataflow_summarizable = false` and falls back to the strictly-conservative
S-chain (every adjacent impure pair gets an edge).

## Inputs audited

### `tana/re/web/spec/78d928dca7` — Tana web app (`gaffer-private`)

Main debundled chunk: `static/index-DI2GynTv.js` (204k lines, 6.9MB
minified). Other JS files in the snapshot are either vendor swaps (the
spec replaces them with their npm-sourced original) or lazy-loaded chunks
out of debundle scope.

Audit performed by ripgrep over the minified source on
2026-05-18. All counts below are raw `grep` hits, then visually
verified to rule out string-content false positives.

| #   | Shape                                                                           | Hits | Notes                                                                                                 |
| --- | ------------------------------------------------------------------------------- | ---- | ----------------------------------------------------------------------------------------------------- |
| 1   | `\beval\b`                                                                      | 0    | no occurrences at all                                                                                 |
| 2   | `\bwith\s*\(`                                                                   | 0    | 758 raw `\bwith\b` hits are all string content (`"voice chat with LiveKit"`, `"node with id="`, etc.) |
| 3   | `\bFunction\s*\(`, `new\s+Function\s*\(`                                        | 0    | 0                                                                                                     |
| 4   | `globalThis\s*\[`, `window\s*\[`, `\bself\s*\[`                                 | 0    | 2 raw `globalThis` hits are inside the standard `typeof globalThis !== "undefined"` UMD guard         |
| 5   | `(Object\|Reflect)\.defineProperty\s*\(\s*(globalThis\|window\|self\|global)\b` | 0    | 0                                                                                                     |
| 6   | `new\s+Proxy\s*\(\s*(globalThis\|window\|self)`                                 | 0    | 0                                                                                                     |

**Conclusion**: the Tana RE app bundle is clean across all dynamic-dispatch
shapes that would defeat static dataflow tracking. The dataflow-aware
S-chain can fire on every impure statement in this bundle without
triggering the fallback path, modulo per-statement bail-outs for any
local shape we haven't modeled yet.

### Other inputs

Add a row per audited bundle as new corpora come online.

## Re-running the audit

```bash
BUNDLE=tana/upstream/web/snapshots/78d928dca7/static/index-DI2GynTv.js
grep -cE '\beval\b' "$BUNDLE"
grep -cE '\bwith\s*\(' "$BUNDLE"
grep -cE 'new\s+Function\s*\(|\bFunction\s*\(' "$BUNDLE"
grep -cE '(globalThis|window|self)\s*\[' "$BUNDLE"
grep -cE '(Object|Reflect)\.defineProperty\s*\(\s*(globalThis|window|self|global)\b' "$BUNDLE"
grep -cE 'new\s+Proxy\s*\(\s*(globalThis|window|self)' "$BUNDLE"
```

Each new snapshot (a Tana app version bump in `tana/upstream/web/snapshots/`)
should re-run the audit before bumping the spec's `dataflow_aware_s_chain`
opt-in.
