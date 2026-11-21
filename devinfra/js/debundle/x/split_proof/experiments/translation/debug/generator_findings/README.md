# Wrong footprints found by the footprint oracle and the generators

[`cases.mjs`](cases.mjs): minimal programs in the format of [`effects/cases.mjs`](../../../effects/cases.mjs), each a reorder that `translate` accepts and Node runs differently. From `experiments/effects`:

```bash
CASES=../translation/debug/generator_findings/cases.mjs node run.mjs
```

Each `FAIL … WRONG ACCEPT` is a finding that stands. A line that passes means it is fixed: move the case to [`cases_general.mjs`](../../../effects/cases_general.mjs). Premise P3 is on (`noWorldHooks`), as in the fuzzers.

| Program                                                                                                                                                                                                                                                                                                                                                                                       | What the analysis gets wrong                                                                                                                                                                  | Cause                                                                                                                                                                                                                                                                                                                                                         | Found by                                                                                                          |
| --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| F7 `install = () => { Object.defineProperty(obj, "g", { get() { o.n++; … } }); }`, the world runs `install` (`[1].values().forEach(install)`), then `obj.g`. Also: `obj.toString = …` or `Object.setPrototypeOf(obj, { get g() { … } })` in the callback, the same in a generator body, and `Reflect.apply(Object.defineProperty, null, [obj, "g", { get() { … } }])` with no callback at all | The unit that reads `obj.g` (or converts `obj`) is pure and writes nothing, though it runs the getter and changes `o`. A pure reader of `o` crosses it. With P3 off every program is refused. | Premise P3 lets a property of an object the world holds be read like any other. It is stated for the world's code and assumed for all code the analysis does not run. A callback of the program that the world runs is such code, and it can add an accessor, a hook or a prototype to any object the world holds; a standard function without a row can too. | A review of the rules for F6, which had the same shape: a hook of the program reaches an object through the world |

F7 stands under premise P3, and it is answered by not assuming P3 for real inputs (decided 2026-11-21). Measured with `lower` of PR #18587, units that touch the world, P3 on against off:

| Production bundle                                        |  Units | P3 on | P3 off |
| -------------------------------------------------------- | -----: | ----: | -----: |
| the large private bundle                                 |  9,709 | 1,654 |  1,751 |
| a Claude Code web capture, the 62 chunks its spec covers | 47,762 | 3,347 |  3,452 |
| a Claude Code CLI release, the 30 chunks its spec names  | 28,280 | 1,484 |  1,542 |

On the large bundle P3 off changes 98 of 9,709 footprints. The conflict rule then still peels 886 modules together, and keeps 2,364 of 2,366 whole in bundle order. (The +30% measured before the fixes for F1 to F6 is gone: most units P3 covered became unknown code for other reasons.)

A cheaper rule was measured and dropped: give up P3 only once code that escapes names a construct that adds a hook (`defineProperty`, `defineProperties`, `setPrototypeOf`, `Object.assign`, `__defineGetter__`, a mutator of `Reflect`, a store under `toString`, `valueOf`, `then`, `__proto__`, `prototype` or a computed key). Counting computed keys, it fires before the first unit runs on every bundle: `t[r] = e.charCodeAt(r)` in an exported function is enough. Without them, the large bundle's escaped code holds one getter, on a fresh `Object.create(…)`; the Claude bundles' escaped code defines getters and `toString` on objects it is handed, with 260 and 238 hits. So the text cannot tell when P3 holds, and P3 off costs little.

## What did not turn up

Over these programs the oracle found no unit that changed a binding its footprint does not name, neither `unwritten` nor `unlisted`, and `lower` finished on all of them (premise P3 on for the corpora of `fuzz.mjs` and for the cases, off for Shift's programs; `--runs 2`):

| Programs                                                                          |                                                              Valid inputs | Units | Bindings changed (unit, binding) |
| --------------------------------------------------------------------------------- | ------------------------------------------------------------------------: | ----: | -------------------------------: |
| `fuzz.mjs` unit templates, seed 1 (61 of them patch a standard object on purpose) |                                                                150 of 150 | 2,381 |                              417 |
| `fuzz.mjs --callback-corpus`                                                      |                                                                  16 of 16 |   112 |                               16 |
| `fuzz.mjs --ast-corpus`                                                           |                                                                150 of 150 | 2,611 |                              303 |
| `fuzz.mjs --full-corpus` (`gen/`, every family), seed 1, 160 programs             |                                                                160 of 160 | 4,196 |                              569 |
| the same, seed 2, 200 programs                                                    |                                                                200 of 200 | 5,269 |                              733 |
| Shift module grammar, 150 seeds, those the test keeps                             |                                                                  22 of 29 |    78 |                                0 |
| Shift script grammar                                                              |                                                                  29 of 55 |   113 |                                0 |
| Shift script grammar with modern expressions                                      |                                                                  29 of 55 |   142 |                               29 |
| the 110 cases of `effects/cases.mjs`                                              | 100 of 110 (10 throw in plain Node: they need a browser or a vendor file) |   538 |                              118 |
| the 173 cases of `effects/cases_general.mjs`                                      |                         171 of 173 (two throw: they import a vendor file) |   877 |                              229 |

Shift's programs hardly change a binding after it exists: the oracle has little to see there. The 5 programs of `effects/debug/p1_known_gaps` are valid and give no finding: a patch of a standard object is not a change of a binding (the oracle lists them as patched intrinsics). The oracle re-finds the 5 programs of [`effects/debug/general_known_gaps`](../../../effects/debug/general_known_gaps/README.md) (four `unwritten`, and one `unlisted`: Node's `util.inspect.custom`) and the 5 of F7 (`unwritten`). The libraries (`lodash`, `acorn`, `semver`, `zod`) gave no finding before the fixes of F1 to F6 and were not run again after them.
