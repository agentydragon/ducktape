# Known limits of the general rules

[`cases.mjs`](cases.mjs) has a program for each place where the analysis lets a standard function do more than its row says, or runs a rule with less than the language does. Each is accepted in an order that Node runs differently. From `experiments/effects`:

```bash
CASES=debug/general_known_gaps/cases.mjs node run.mjs
```

Every `FAIL … WRONG ACCEPT` is a known limit. A line that passes means a limit is gone: move the case to [`cases.mjs`](../../cases.mjs).

| Program                                                                   | Why it is not seen                                                                                 | What would see it                                                                                    |
| ------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `arr.constructor = { [Symbol.species]: f }`, then `arr.map(…)`            | The methods that build their result through the species do not call it                             | A row flag for `map`, `filter`, `slice`, `splice`, `concat`, `flat`, `flatMap` that calls it         |
| `Reflect.get(src, "v", target)` with a getter that writes `this`          | The getter runs with the source as `this`, not the receiver                                        | Pass the receiver as `this` of the accessor call                                                     |
| `Reflect.set(o, "n", 1, target)`                                          | The store goes to the target of the call, not to the receiver                                      | Store into the receiver                                                                              |
| `String.raw({ raw: { get length() { … } } })`                             | A row reads the properties of its argument one level deep; the getter is on the object under `raw` | A row that reads two levels. `String.raw` is nearly always a tag, where the engine builds the object |
| `MyArr.from([1])` where `MyArr extends Array` constructs and runs `MyArr` | A static is called with its receiver, and a standard function does not construct the receiver      | A row flag that constructs the receiver when it is a class of the program                            |
| `console.log({ nested: obj })` where `obj` has `util.inspect.custom`      | `console.log` keeps nothing, and Node calls the method at any depth. Browsers do not call it       | Treat an argument that reaches an object with a hook like a call into the world                      |

The rules these are the limits of: a standard function calls what its row says (`calls`, `callsMethods`, `readsContents`, `iterates`, `convertsContents` in [`Intrinsics.lean`](../../../../lean/Experiments/Intrinsics.lean)), and nothing else.

Found by a fresh agent that tried to break the general rules with small programs (63 minimal programs in 15 causes; the 57 it found that are fixed are cases in [`cases.mjs`](../../cases.mjs)).
