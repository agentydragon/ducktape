# Retained object spread from Zod #52

Zod 4.6.7's full readable bundle has SHA-256
`5d67ee00c72ddfbaa8d82f88913330687b364e6767faf5065538238d21495538`.
Its unit #52 is `normalizeParams` at UTF-16 range `[36686,37168)`
(SHA-256 `2de528f7cdecd630a739b31d8348453d917dc04837dae33bb8e470909465057c`).
In the string-error branch it returns `{ ...params, error: () => params.error }`.

The [full blocker census](FULL_BLOCKER_CENSUS.md) already lowers this unit in
isolation. The first refusal for a retained copy-and-override body was in
`indexed-exported-function`: its concrete syntax check rejected object
expressions even though the evaluator implements them. The check now admits
static string-key initializers, zero-argument arrow values with a concrete
return expression, and spread operands in the same bounded fragment. It
continues to refuse literal getters, methods, prototype setters, computed
initializer keys, and rest exclusions. Copying uses the existing
`CopyDataProperties` evaluator. The checked constructor keeps the same unit
order, linked captures, public cells, and all-outcomes proof.

| Replayed source                                                   | Before                             | After                                                                                                                   |
| ----------------------------------------------------------------- | ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| Three-row control with Zod's exact copy-and-override expression   | Retained entry unit refused        | Certifies at `all-outcomes` and passes disk readback                                                                    |
| Whole #52 body copied verbatim into the same three-row projection | Retained entry unit refused        | Still refused; no artifact. The body includes other forms, including `params?.message`, `delete`, and the error branch. |
| Unchanged 1,072-unit Zod with halves ownership                    | Refused in the full blocker census | Refused; no artifact. The separate empty-profile source probe stops at eager `Object` in #0.                            |

Replaying complete prefixes of the original #52 body after the spread change
admits its first three statements. The first refused prefix adds
`if (params?.message !== void 0) { … }` at UTF-16 range `[36850,37031)`
([prefix replay](https://app.buildbuddy.io/invocation/74d8bfc1-4511-4fc7-88fd-357d03eb4a79)).
This branch also contains the optional `error` read, the throw, and the
assignment; the statement-level refusal by itself does not isolate which
subexpressions must be admitted.

The control uses an ordinary caller-supplied object. An index getter observes
its original receiver, deletes a later own key, and adds a key after
enumeration starts. The copy skips the deleted, late, inherited, and
non-enumerable properties. It copies the enumerable symbol and own
`__proto__` as writable, enumerable, configurable data properties. The
override closure still reads the original parameter's current error field.
A throwing getter preserves its thrown object and earlier effects and prevents
the next getter from running. The same observations run against the complete
#52 function in Node and through the certified control's emitted entry.
Public `z.string` also observes the expected error message on the full
original bundle. Swapping copy and override in an emitted file changes the
result and is refused by disk readback.

[Failing baseline](https://app.buildbuddy.io/invocation/133aca82-4cd3-443b-b8b5-585e923193f7)
and [passing replay, CoreJS lowering, and constructor regressions](https://app.buildbuddy.io/invocation/530fe70c-1c18-4fdd-ae23-7f0e3bdcee6d).

```sh
p=//devinfra/js/debundle/x/split_proof/integration/library_splits
bbr test "$p:zod_object_spread_test" --test_output=all
```
