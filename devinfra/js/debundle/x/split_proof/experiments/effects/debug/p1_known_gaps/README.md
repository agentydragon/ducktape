# Known limits of premise P1

[`cases.mjs`](cases.mjs) has a program for each patch of an intrinsic that the analysis does not see (premise P1 in the [effects README](../../README.md#premises)). Each is accepted in an order that Node runs differently. From `experiments/effects`:

```bash
CASES=debug/p1_known_gaps/cases.mjs node run.mjs
```

Every `FAIL … WRONG ACCEPT` is a known limit. A line that passes means a limit is gone: move the case to [`cases.mjs`](../../cases.mjs).

| Program                                                                                  | Why it is not seen                                                                                     | What would see it                                                                                       |
| ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------- |
| `Object[k](Object.prototype, …)` with a computed name                                    | A member of an intrinsic under an unknown key is the world, and the world is assumed not to patch (P1) | A call through an unknown member of a standard namespace changes the intrinsics among its arguments     |
| `apply(Object.defineProperty, Object.prototype, …)` through `(f, ...a) => f(...a)`       | A spread joins the arguments into one value, so the target is mixed with the key and the descriptor    | Keep the positions of a spread of an array whose elements are known                                     |
| a patch through an alias (`const O = Object; O.prototype.x = …`) in code that is not run | The text of code that is not run is read for the names of intrinsics only                              | Run that code deeper, or resolve aliases in the text                                                    |
| `Object.defineProperty(globalThis, "Array", …)`                                          | The definition changes a global, and the key names which one only as a string                          | A `define` whose target is the global object and whose key names an intrinsic is a patch of that global |
| `RegExp.$1` read after `exec`                                                            | `exec`, `test`, `replace`, `match` and `split` with a pattern write the legacy statics of `RegExp`     | A row for each of them that writes `RegExp.$1` … `$9`, and a read of one that reads them                |

Found by a fresh agent that tried to break the patch rules with about 400 small bundles (24 minimal programs; the others are cases in [`cases.mjs`](../../cases.mjs)).
