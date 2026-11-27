# Global member reads in pinned library bundles

The empty-profile constructor cannot yet decode the first global reads in two
public bundles. This note records the boundary and two possible designs; it
does not propose an acceptance rule or a new host premise.

At `d10843cd8d0fd27e7c84dd4a89204663bbbdf31c`, the existing pinned library
preparation produces these original units. Ordinals count the original
top-level units, including export metadata; the scope annotator marks each
`Object` reference as global (`G`), rather than a local declaration or import.

| Bundle                                                                                   | Original unit                                  | Operation                                                              |
| ---------------------------------------------------------------------------------------- | ---------------------------------------------- | ---------------------------------------------------------------------- |
| semver 7.8.6, SHA-256 `9a1ab71f40d915f55f3985c314a844d7686f936b1a097dd78ded2c92cf84ba44` | `#0`: `var __create = Object.create;`          | Eager binding read, then eager property Get; save the resulting value. |
| semver, same bundle                                                                      | `#1`: `var __defProp = Object.defineProperty;` | Another eager binding and property read.                               |
| Zod 4.6.7, SHA-256 `5d67ee00c72ddfbaa8d82f88913330687b364e6767faf5065538238d21495538`    | `#0`: `var __defProp = Object.defineProperty;` | Eager binding and property read. A later helper call is separate.      |

Neither row calls the value it saves. The whole-chunk indexed probes stop at
`unsupported global Object` before placement checking; this is a source
decoder refusal, not a semantic counterexample or a failed certified split.
The [cohort probe](https://app.buildbuddy.io/invocation/a7d2a9ee-85c0-4497-bc62-d3306cbc5f73)
records the exact pinned requests and refusals. The [row inspection](https://app.buildbuddy.io/invocation/43373266-19b8-4a68-8add-d6afebef8009)
records the original text and binding annotations. No before/after improvement
has been measured. Acorn's separate first refusal is an eager `new RegExp(...)`
at `#8`, which additionally evaluates an argument and constructs a value.

## Why a fixed `Object` is not enough

A generic Node experiment can replace the global binding with an accessor and
return an object whose `create` property is also an accessor:

```js
import assert from "node:assert/strict";
import vm from "node:vm";

const trace = [];
const replacement = {
  get create() {
    trace.push(this === replacement ? "property" : "wrong receiver");
    return 7;
  },
};
const global = {};
Object.defineProperty(global, "Object", {
  get() {
    trace.push("binding");
    return replacement;
  },
});
assert.equal(vm.runInNewContext("var saved = Object.create; saved;", global), 7);
assert.deepEqual(trace, ["binding", "property"]);
```

If the binding getter throws, the property getter is not reached; if it
returns `null`, the property read throws a `TypeError`. A parameter named
`Object` shadows that global getter. A property getter may mutate state, throw,
or return an arbitrary value, and its receiver is the actual binding value.
These are ordinary JavaScript observations; an initial intrinsic identity
alone does not establish them away.

## Existing model and the boundary

[`FromESTree.reference`](../EsmSplit/CoreJS/FromESTree.lean) refuses an
unrecognized global under the empty profile. `World.hostRef` in
[`Value.lean`](../EsmSplit/CoreJS/Value.lean) can change host state, but its
successful result is only a named host identity or number; absence becomes
`unknown`. It cannot directly return an arbitrary modeled object, `undefined`
or a JavaScript throw, or express a getter callback into program cells.
`opaqueAmbientRef` also ends a reached read as `unknown`. Equality of two such
unknown results alone does not establish the real program's subsequent
effects. The ordinary constructor currently refuses structural/host options.

[`resolveGlobal` and `readGlobal`](../EsmSplit/CoreJS/Eval.lean) already model
lexical shadowing, object-backed bindings and getters for a supplied **complete
ordinary** global environment. They are not wired into the current expression
evaluator and `runProgram`; a partial or exotic host object cannot be silently
treated as that complete snapshot. The [existing realm proposal](../PLAN.md#host-contract)
describes broader environment work.

## Bounded alternatives

1. **Preserve the operation parametrically.** Start with an unchanged unit in
   an order-preserving split. Quantify over every modeled world and fuel and
   require the source and output to execute the same original operation with
   the same lexical/import cells and host context. Existing
   [`OpaqueInput`](../EsmSplit/CoreJS/OpaqueInput.lean) and
   [`OrderedRegion`](../EsmSplit/CoreJS/OrderedRegion.lean) are ingredients,
   not this completed constructor. Its receipt must identify the original
   unit, owner, order, captures and checked output; a changed cell or changed
   operation must refuse. Prove correspondence for normal, thrown and unknown
   outcomes and callbacks, rather than deriving JavaScript equivalence from
   two matching `unknown` results. Any movement across effects additionally
   needs a proved frame condition. Deployment must preserve the relevant global
   environment and resolution context; the proof cannot assume those facts
   merely because the text is unchanged.
2. **Model a richer environment.** Evaluate global resolution and property Get
   against a supplied initial global environment and the existing state
   transitions. Quantify over all admitted initial heaps and worlds, then
   prove source/output behavior for every fuel, including getter calls,
   mutation and throws. The receipt must state the actual environment and
   host/frame premises used; an incomplete or exotic environment remains
   unknown or refused. A concrete test heap or observed pristine builtin is
   not a premise for every deployment.

Both paths need independent controls for binding and property getters, getter
receiver, thrown lookup, null binding, lexical shadowing, changed live cells,
and output readback. Record full-chunk decode progress separately from actual
certified emission and Node observations. Do not infer host purity from a
successful probe, or use the desired source/output equivalence itself to
justify the opaque operation's correspondence.

The policy decision is which deployment correspondence or environment
premises the product is willing to expose and trust. Until that is decided and
proved, retain the current refusal.
