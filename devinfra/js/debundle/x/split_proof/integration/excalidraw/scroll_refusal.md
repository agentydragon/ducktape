# First refusal in the pinned scroll placement

Baseline: Ducktape commit `c0ef3be4f3bcf4725777e1f260f146ec54926bc8`,
`//devinfra/js/debundle/x/split_proof/lean:lowersplit`, and
`chunk-PN7KOLDJ.js` at SHA-256
`fa1cfc7930fe3c0e2b4d95e6cb14ebde21068014cd92db303622509d9a0d455c`.

The [unchanged replay](https://app.buildbuddy.io/invocation/b8da5e60-fbfd-4914-a4b3-e55365d94171)
runs the ordinary CLI with a deployment manifest and `--require normal-completion`
on the original file beside its pinned providers. The pair moves `#1337` and
`#1338` into `packages/excalidraw/scene/scroll.mjs`; the single move leaves `#1338`
in the entry. Both exit 1 with `no_constructed_rule`, null guarantee and scope,
and no output directory. The replay writes `scroll-spec.json` and `result.json`
as test artifacts. Separate `--diagnostic --plan` invocations report 518 and 259
`OrderOk` conflicts, respectively, starting with `[5,1337]`. Those artifacts
measure structural inversions; the ordinary CLI does not report an order error.

The ordinary singleton path classifies the original source before selecting
placement units or applying a proof. Its current verdict discards the
classification error. The manual diagnostic uses the same `module_io --parse`
and indexed source decoder as that path. Its first error on the unchanged file
is `statement 41: unsupported global Uint8Array`. Unit `#41` eagerly evaluates
`new Uint8Array([...])`. The current `constructor_acceptance_test` also reaches
that refusal through the authenticated classifier. An older version of that
probe reported `statement 5: unsupported global Object`
([run](https://app.buildbuddy.io/invocation/b522966b-068a-4e30-b8ff-5b8a7443610d)).
That probe omits the opaque capture annotations that let the ordinary frontend
retain the deferred React callback containing `Object.assign`. The first
`ImportExpression` refusal at `#354` is from a separate lowering census.
Neither explains the ordinary CLI's first refusal.

## Reproduce the reduced cases

```sh
bbr test //devinfra/js/debundle/x/split_proof/integration/excalidraw:scroll_slice --test_output=all
```

The test projects the original AST through module-scoped definitions, reads
(including deferred captures), and top-level binding writes, keeping source
order, the projected public export row, and the original owners from the
scroll request. It copies retained static providers transitively, without
editing their bytes. This is a lexical and static import closure for a refusal
reproduction, not a claim of semantic slicing of the editor.

| Projection              | Source ordinals / count      | Decoder stage and first error                                                     |
| ----------------------- | ---------------------------- | --------------------------------------------------------------------------------- |
| Single crossing and #41 | 4, 5, 41, 1337, 1786         | Lowering: slice #2 (original #41), unsupported global `Uint8Array`                |
| Pair crossing and #41   | 340, including 1337 and 1338 | Lowering: slice #6 (original #41), unsupported global `Uint8Array`                |
| Single crossing alone   | 4, 5, 1337, 1786             | Indexed source admission: `checked construction requires an import-free original` |

All three receive `no_constructed_rule` through the ordinary CLI and leave
its destination absent. The test artifacts are the generated source and
provider files, specs, ordinal maps and verdicts (`result.json`), the single
structural plan (`structural.json`), and the full source probe and isolated
empty-profile census (`full-lowering.json`). The census also records eager
`Array` at `#46` and `navigator.platform` at `#376`; [an earlier pair
projection](https://app.buildbuddy.io/invocation/df1cddb9-783f-430f-a762-9835d36b1d63)
without `#41` reproduces the latter. None of the reductions evaluates the editor
or exercises its lazy dependency graph.

## Candidate operations and counterexamples

1. **Link a named provider binding in the indexed constructor.** The four-unit
   crossing has an exact provider alias (`Ve` to export `b`), a wrapper
   initializer, the independent arrow, and an entry export row. First separate
   named import resolution and source-unit coverage from execution. For a
   candidate move rule, prove the provider cell and callback captures still
   resolve to the same cells, and prove wrapper creation defers the callback.
   The test's separate Node control uses the actual provider with a synthetic
   capturing callback. Both orders call it zero times on creation and once on
   load; repeated loads return the same object. Adding an eager load at the
   initializer makes the first load cache `undefined` before the crossed write,
   but cache the arrow itself after it. Both orders complete; the returned
   field's identity differs. Neither a syntactically bare imported call nor the
   shape of its result is sufficient to prove commutation. The existing
   [CoreJS wrapper theorem](../../EsmSplit/CoreJS/WrapperLinked.lean)
   covers a modeled wrapper, not this provider and placement.
2. **Specify typed-array allocation over a verified constructor value.** The
   first original lowering failure names the ambient `Uint8Array` lookup.
   Separately from how that binding is obtained, a scoped allocation rule
   should preserve fresh object identity, contents, and the initialized
   binding. Equal bytes cannot justify aliasing two arrays: writing one would
   then change reads through the other. Accepting a callee by spelling would
   also accept a replacement constructor that observes or changes the crossed
   binding. This diagnostic adds no host premise or proof rule.

## Exact wrapper boundary

`scroll_slice` writes `wrapper-boundary.json` from the same pinned editor
and provider bytes. The editor's `#5` resolves `Ve` through the named import
at `#4` to export `b` of `./chunk-BLPUKQJE.js`. That export resolves to
local `n` at provider ordinal `#7` (counting each declarator separately).
The scope analysis finds one definition and no subsequent source writes to
`n`. The callback at `#5` captures no enclosing lexical binding. The factory
captures none either, but the returned arrow closes over the factory's two
parameter cells:

```js
var n = (a, b) => () => (b || a((b = { exports: {} }).exports, b), b.exports);
```

The call at `#5` creates the callback and loader and initializes the loader's
private parameter cells; it does not evaluate the loader body. The runtime
control on the exact export supplies a synthetic callback that writes to
`exports`, reenters the loader and throws. Reentry sees the same partially
populated object, and the next call returns that object without rerunning the
callback. This rules out modeling a load as a fresh invocation, caching only
on success, or hiding the callback's effects.

The selected crossing is `[#5, #1337]`: `#5` stays in
`chunk-PN7KOLDJ.js`; `#1337` moves to
`packages/excalidraw/scene/scroll.mjs`; provider `#7` stays in
`chunk-BLPUKQJE.js`. The single and pair editor placements still have 259 and
518 structural conflicts. This audit discharges zero placement conflicts and
publishes no split, so there is no staged split to recheck.

The exact provider fails authenticated classification at `#0` (`Object.create`),
before it reaches `#7`. A proof for the four-unit crossing still needs a
linked prefix that preserves the provider's effects and abrupt outcomes in
both schedules, then establishes that `Ve` resolves to the initialized
factory cell when `#5` runs. Knowing the definition site alone does not
establish that state: even an `Object.create` getter could throw before
`#7`. With that prefix accounted for, the allocation swap must preserve the
original allocation identities and captured cells, prove equality at equal
fuel, and carry the same loader body and cache cells into the arbitrary
continuation. `WrapperLinked.normal_iff` supplies the successful-run
commutation for the modeled shape, but does not establish this provider
prefix, all outcomes, or correspondence to the actual staged files. The
provider cannot be dropped or assumed to evaluate under pristine globals.

### Loader invocation after a shared prefix

[WrapperLoader](../../EsmSplit/CoreJS/WrapperLoader.lean) proves how the
original loader runs from a state that already contains the checked closure.
The pinned source test uses the existing linked lowerer and compares the
entire lowered provider unit at `#7`, including the loader body, to the
statement used by the proof. This comparison of a single unit does not
execute the omitted provider prefix.

On a cache miss the evaluator allocates the module and exports objects in
their original order. The proof identifies two distinct fresh addresses
and shows the cache holds the module address before callback dispatch.
The first exports read is from an own data property and does not assume
anything about inherited properties. A call after that write skips the
callback and reads the current exports property. The proof keeps the actual
callback dispatch and the final property read at their original fuel
budgets, so a callback can reenter, replace exports, or throw. A callback
throw or model refusal propagates with its state; the final read only runs
after a normal callback result.

The suffix proof compares the full result at equal fuel when both schedules
arrive at the same checked state with the loader binding resolving to the
same cell. It does not show that the editor placements reach that state.
The nearest crossing is still editor `[#5, #1337]`, where the wrapper stays
in the editor and the arrow moves to the scroll owner. It is also crossed
by the full three-member scroll placement (`#1336`–`#1338`). That placement
needs additional ordering proofs, including the unchanged provider prefix
and its ambient reads. No split is certified by the invocation theorem.
