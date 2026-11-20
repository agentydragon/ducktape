# Footprints by abstract interpretation

`Lower.lean` decides which units are effects from a short allow-list of inert shapes, so every call is an effect and any two calls keep their order. [`Effects.lean`](../../lean/Experiments/Effects.lean) reads the code instead: it runs the bundle's top level once over abstract values and reports, per unit, whether it touches the world (`effect`), which names it may change (`writes`) and which names it reads through functions it calls (`deps`). The verified model is unchanged apart from `Stmt.deps`; the checker's trust moves from "the allow-list is right" to "the interpreter's rules and premises are right".

`translate_effects` is `translate` with these footprints; `effects` prints them. `translate`, `lower` and `lowersplit` are untouched.

## Run

```bash
cd ../../lean && lake build effects translate_effects
cd ../experiments/translation && cp package.json.in package.json && npm ci --no-audit --no-fund
node ../effects/run.mjs [--show]                      # golden cases
node fuzz.mjs --effects --count 300 --seed 3          # tree fuzzer with translate_effects
node frontend.mjs bundle <bundle.js> --out bundle.json
../../lean/.lake/build/bin/effects bundle.json --premises premises.json
../../lean/.lake/build/bin/translate_effects bundle.json output.json cert.json --premises premises.json [--table t.json]
```

`./run.sh --effects` runs the golden cases and the fuzzer.

## Premises

Printed with every verdict (`premiseLines`), assumed and not checked:

| #   | Premise                                                                                                                                                                                                                                                                                                                                                                                                     |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| P1  | The intrinsics (`Object`, `Array`, `Map`, `Promise`, …) are not patched. A write to one is a world effect, so a bundle that patches one gets obligations; another script patching it before the bundle is not seen.                                                                                                                                                                                         |
| P3  | The world defines no hooks: it adds no accessor or conversion hook to an object the program made, and converting a value the world made runs no program code (accessors and hooks the program defined are run). `noWorldHooks` in the premises file; **off by default**: every access to an object the world holds, and every conversion of a value the world made or holds, is then a call into the world. |
| P4  | Evaluation completes: no uncaught exception, no hang (premise A6 of [`design.md`](../../../../docs/design.md)). A unit that merely might throw is not an effect for that reason; the equivalence claimed is for runs that complete.                                                                                                                                                                         |
| P5  | No other chunk runs during evaluation, except through callbacks this chunk gave the world.                                                                                                                                                                                                                                                                                                                  |
| V   | The vendor table: imports from outside the tree that only allocate (`"./v.js#make": "kit"`), and per family the members that only allocate. Any other import is the world.                                                                                                                                                                                                                                  |

The premises file is `{"vendor": {"from#imported": "family"}, "members": {"family": ["m"]}, "noWorldHooks": true}`. Every key is optional; an unknown key or a wrong shape is an error. A table written to make a measurement come out well is a guess: label it as one.

## What it computes

- Values: a set of codes (some primitive, the world, or an allocation site qualified by the call chain that reached it). Objects have constant-key properties, a bucket for other keys, prototypes and the keys with accessors. Variables and function scopes are objects too. Updates only add, so one pass in order summarizes the state before each unit under every valid order.
- Calls into code it can see are run (inlined; one level of call-site context, a depth cap, a step budget per unit, memoized while the heap is unchanged). Intrinsics come from the table in [`Intrinsics.lean`](../../lean/Experiments/Intrinsics.lean) (data, with the reason each row is safe). Any other call is the world.
- A call into the world makes the unit an effect; every object passed in **escapes**: the world may mutate it and call any function reachable from it. Variables those functions assign are _tainted_ (reads may return anything). A unit that runs code the analysis cannot see writes every name that reaches an escaped object; a unit that only talks to the environment (`console.log`, `addEventListener` with a callback it keeps) writes none.
- `writes` are names: every module-scope name from which a changed object can be reached. `deps` are names read by code the unit ran but did not mention (closures reached through objects other units filled).
- Names the bundle exports start out escaped: other modules may observe them during a cycle.
- Code that runs without a call expression is run or given up on the same way: a getter or setter (own or inherited), a conversion hook (`valueOf`, `toString`, `Symbol.toPrimitive`), the iterator of a spread or a `for…of`, a `then` of a thenable, a generator body (it runs when the iterator is advanced, in some later unit), and the conversions the standard functions do on their arguments. `await` makes the unit an effect: the continuation runs after the program, in the order the awaits were reached.

**Fail safe.** An unrecognized node, a direct `eval`, an exhausted budget or an over-wide set becomes a call into the world that also escapes everything the unit mentions. A call that is run but not looked into (deeper than the depth cap, through more candidates than the fan-out cap, a generator) escapes the function and its closure. More precision comes from understanding a construct. A wrong understanding gives a wrong verdict, and so does a give-up that forgets what the code it skipped can reach: the first version had eleven of those that the fuzzer did not find ([experiments § 13](../README.md#13-footprints-by-abstract-interpretation)).

## Compared with `Lower`

- No identity-call or factory recognizers (`inferredIdentity`, `inferredFactory`): the interpreter runs those calls. No `observedInit` or `nonReentrant`: initialization observed through an escaped callback is ordered through `writes` (a unit that may run escaped code writes every escaped name, and the declaring unit binds its own).
- A unit that touches only program objects is not an effect, so reordering such units no longer produces obligations; its `writes` order it against everything that shares an object with it.
- Costs more: about two minutes on the 9,707 units of the large bundle, against two seconds.

## Tests

- [`cases.mjs`](cases.mjs): 60 small bundles with the classification of every unit (`world`, `pure`, `writes a b`), each checked against what JavaScript does, and 47 reordered programs run in Node next to the original (`run.mjs`; `--permutations N` adds random permutations of every case's units). A `pure` where the code writes something is a bug; a `world` where the code is harmless is a loss of precision.
- [`fuzz.mjs --effects`](../translation/README.md#fuzzer): random bundles and splits, Node as the oracle. Templates that need this analysis: aliases through a variable, a destructuring or a registry object, writes through a helper, closures with state, instances, decorators and getters.
- [`fuzz_mutate.py`](../translation/fuzz_mutate.py): 54 mutants, each removing one rule of the analysis (or of the model); `run.mjs` runs first, then the fuzzer. A mutant that neither catches is either harmless or a missing case.

Eleven of the reordered programs were accepted by the analysis before the rule that rejects them was added, which is how the rules were found. For the others, the mutant that removes the rule is the evidence that the program needs it.

## Finding what blocks precision

`EFFECTS_WHY=1 ../../lean/.lake/build/bin/effects bundle.json --premises p.json` prints, on stderr, how many units touch the world for each first reason (`call of import ./vendor.js#o`, `getter or setter the world made`, `store into env:window`, …) with four unit numbers each. The reasons that name an import are the vendor table's work list.
