# Statement-class prototype: independent soundness audit

Audited Ducktape `56256b0cfab222d8f37bb6cee869379ddcfcfeff`. The
`x/summary_effects` tier-0 analysis is report-only: these findings do not
show that the current production gate accepts the same assignments.

Rechecked against `devel` at `40eadf4d5f1031788492096f78b2c3227b6e782d`:
the prototype and its JS parser source were unchanged from the audited
revision, and all 11 synthetic verdicts and the comparison-input cases
reproduced through `bb run -c opt`.

The existing `soundness_test` passed on RBE (invocation
`b4dc655e-84cc-4ee9-842c-77c957a8e66d`). Independent synthetic probes ran the
prototype through `bb run -c opt` and executed the original and reordered
programs under Node 24.21.0. In every negative case below, the report counted
one swapped pair and zero unproven pairs. No author premises or
`--what-if-pure` were supplied. The Node programs were manually reordered;
these are commutation checks, not pipeline-emitted-module tests.

## Findings

### Named default exports are missing from the binding census

`export default` function and class declarations are classified as hoisted or
pure, but their names are absent from the binding census in
<../x/summary_effects/tier0.rs>. The missing function has no reachable node
and its body's writes are never enabled.

```js
export default function bump() {
  state = 1;
}
let state = 0;
bump();
const seen = state;
console.log(seen);
```

Moving `const seen = state` before `bump()` changes stdout from `1` to `0`;
both runs succeed. Tier 0 reports no conflict, including with
`--exports-reachable`. Replacing the first line with an ordinary function
declaration makes the prototype refuse the same swap with a `write` conflict.
Similarly, moving a read of a named default class before its declaration
changes a successful run to a temporal-dead-zone error; an ordinary class
control is refused.

For the function example, save the original as `example.mjs` and run:

```sh
bb run -c opt //devinfra/js/debundle/x/summary_effects:summary_effects_bin -- \
  --chunk example.mjs --order 0,1,3,2,4 --exports-reachable --examples
```

The report gives `swapped_pairs: 1` and `unproven_pairs: 0`. Executing both
orders as ES modules under Node gives the differing output above.

### Default-export initialization has no cell to protect on a cycle

For a module importing `read` from a cyclic peer, `export default 1; read();`
prints `1` when the peer's `read` accesses that default. Moving `read()`
before the export throws a temporal-dead-zone error. With the complete
two-module program supplied, the prototype detects the import cycle and
enables export reach, yet still reports the swap as proven. The default
expression has no binding for the fallback to mark reachable.

```js
// a.js
import { read } from "./b.js";
export default 1;
read();
// b.js
import value from "./a.js";
export function read() {
  console.log(value);
}
```

With both files in a directory whose `package.json` contains
`{"type":"module"}`, analyse `a.js` using
`--program-chunks <directory> --order 0,2,1`.

### Potential exceptions are treated as effect-free

`const value = later; console.log('visible'); const later = 1;` throws before
printing in the original order. Moving the log before the first declaration
prints before the same error, but tier 0 reports no conflict. The original
program already throws; the observable prefix nevertheless changes. The
reordered ordinal sequence is `1,0,2`.

Likewise, `class Example { static ["prototype"]() {} }` throws during class
definition. Moving it across an independent log changes whether the log runs,
but the class is classified as pure even without optional upgrades. The
`static-fields` upgrade has the same issue with a computed static field.

### Computed Symbol-member keys can run coercion code

Under `--upgrade intrinsic-member-reads`, a class method keyed by
`[Symbol.for]` is classified as pure. `Symbol.for` is a function, not a symbol;
converting it to a property key can call `Function.prototype.toString`. In a
synthetic program that modifies that method and logs during conversion, the
prototype allows the class to cross another log, reversing their output. Both
programs complete successfully. The program's own intrinsic write is visible
to the census; the conversion dependency is not included in the class rule.
The reordered ordinal sequence is `0,2,1`.

```js
Function.prototype.toString = function () {
  console.log("coerce");
  return "key";
};
console.log("marker");
class Example {
  [Symbol.for]() {}
}
```

### Enum and derived export-helper calls are not always confined

With `--upgrade enum-definitions`, a TypeScript-style enum assignment to a
fresh `{}` can call an inherited setter or throw on an inherited non-writable
property. The prototype allows it to cross an independent log. The setter case
changes `marker, setter` to `setter, marker` while both runs succeed. Its
reordered ordinal sequence is `0,1,3,2`.

```js
Object.defineProperty(Object.prototype, "A", {
  set(value) {
    console.log("setter");
  },
});
var Enumeration;
console.log("marker");
(function (value) {
  value.A = 1;
})(Enumeration || (Enumeration = {}));
```

With `--derive`, an export helper that calls `Object.defineProperty` may throw
for a primitive target, or when a previous helper call installed a
non-configurable property with the same key. The prototype allows either
throwing call to cross an independent log. In the duplicate-key control the
two helper calls remain in their original mutual order; the reordered ordinal
sequence is `0,1,2,4,3`.

```js
var exporter = (target, all) => {
  for (var name in all) Object.defineProperty(target, name, { get: all[name], enumerable: true });
};
const target = {};
exporter(target, { value: () => 1 });
console.log("marker");
exporter(target, { value: () => 2 });
```

## Unchecked assumptions

The report can assume the absence of import cycles when no program is supplied,
and also records assumptions about mutations of intrinsics by unknown code and
escaped intrinsics. These are conditions for interpreting a verdict, not facts
established by the report. The counterexamples above do not require unknown
code to mutate an intrinsic; the cycle example supplies the complete program
and still demonstrates a missing default-export dependency.

## Comparison-input integrity

The prototype is a measurement CLI, not an admission gate. Its order and
conflict-report inputs must describe the same source and assignment. In
particular, `pairs::positions` checks that every ordinal appears but does not
reject extra duplicates; the tier-0 comparison path does not validate that
each supplied current-engine conflict is an inversion of that order. These
limits make input identity and permutation checks necessary before trusting a
cross-engine comparison.

Both behaviors were reproduced through `bb run`: the two-unit order `0,1,0`
is accepted, and an identity order supplied with a reversed conflict row
reports zero swaps but one conflict in common and `tier0_only` as
`18446744073709551615`. The points-to mode rejects that mismatched row. There
is no evidence that correctly matched, unique pipeline reports trigger either
case.

The conflicting row for the two-statement input
`console.log('first'); console.log('second');` is
`{"runs_first":{"unit":1},"runs_later":{"unit":0},"cells":[]}`.
It matches `--order 1,0`, but is accepted with the mismatched `--order 0,1`
when supplied via `--current-conflicts`.

## Follow-up

Before using tier 0 as a gate, model named and anonymous default-export cells,
preserve the ordering of observable abrupt completion, and repair or disable
the affected optional recognizers. Add the minimized examples as executable
regressions, with controls for ordinary declarations. Validate comparison
permutations and conflict rows before reporting overlap; a precision result is
not a soundness proof.

## Private corpus

A private-corpus comparison was also run. Its inputs and detailed results stay
with the private investigation. Precision measurements cannot establish
soundness in light of the synthetic counterexamples above.
