# Summary-based effect analysis: prototype measurements

Interim. Question: can a summary-based, flow-insensitive analysis replace the
execution-based effect producer in `facts/sync_effects/`? The prototype is
report-only (`x/summary_effects/`); it changes no verdict.

Two analyses share one binary, the parse (`js_ast`), source-unit ordinals and
the swapped-pair sweep:

- **`points-to`**: inclusion-based points-to over the chunk, per-function effect
  summaries over call-graph SCCs, per-cell pair check.
- **`tier0`**: every top-level statement is a pure definition or impure, from
  syntax; impure statements keep their mutual order and read a reference-reach
  set stamped in source order. The rules and the soundness argument are the
  module comment of `x/summary_effects/tier0.rs`.

## Result so far

Public Excalidraw corpus, split shape (`devel`), 4-core VM, single thread.
Swapped pairs left unproven:

| Assignment, chunk        | Swapped | Legacy gate | Current effect verdict | `points-to` | `tier0` |
| ------------------------ | ------: | ----------: | ---------------------: | ----------: | ------: |
| Committed spec, `editor` |   3,589 |           0 |                    710 |         571 |       0 |
| Committed spec, `main`   |   3,029 |           0 |                  1,556 |   no result |       0 |
| Stress, `editor`         | 423,683 |           0 |                140,477 |     not run |   2,383 |
| Stress, `main`           | 169,613 |           0 |                 71,392 |     not run |     784 |

The stress assignment is one module per atomic group (`per_atomic_group` for
both chunks), built by the measurement in
`2026_11_10_excalidraw_module_blockers.md`; its emitted tree passes the browser
smoke, so every pair an analysis refuses there is a precision loss.

Cost on the `editor` chunk (517 KB, 1,787 source units):

| Analysis    | Time                                          | Peak RSS |
| ----------- | --------------------------------------------- | -------: |
| `tier0`     | 28 ms parse, 6 ms analysis, 26 ms pair check  |    24 MB |
| `points-to` | 120 s solve, 21 s summaries, 151 s pair check |   4.9 GB |

`points-to` on `main` (803 KB) had not converged after 15 minutes. The current
engine analyses the `editor` chunk in about 14–16 s on this shape.

**`points-to` is not a replacement as specified**: slower than the engine it
would replace, and less precise than the legacy gate on the committed spec.
**`tier0` is the candidate**: milliseconds, and it keeps the committed spec's
modules.

### `tier0` on the stress assignment

| Measure                                                     |        `main` |        `editor` |
| ----------------------------------------------------------- | ------------: | --------------: |
| Statements: impure / pure / hoisted / inert                 | 714/720/186/8 | 337/1,327/117/6 |
| Unproven pairs                                              |           784 |           2,383 |
| … both statements impure                                    |           276 |           1,672 |
| … definition reachable from the impure statement's own code |            55 |              16 |
| … reachable only through earlier leaks                      |           453 |             436 |
| … assignment to, or eager read of, a written binding        |             0 |             259 |
| Statements in an unproven pair                              |           392 |             420 |
| Modules in an unproven pair, of all                         |   170 / 1,301 |     111 / 1,271 |
| Groups of modules to co-locate (largest)                    |       2 (167) |         1 (111) |
| Modules no unproven pair touches                            |         1,131 |           1,160 |

The current effect verdict puts 994 (`main`) and 1,065 (`editor`) modules in
one component.

- "Reachable only through earlier leaks" is the price of closing the legacy
  gate's gap (it follows a statement's own closure, not callbacks leaked by
  earlier statements).
- "Both impure" pairs are swaps the legacy gate allows because it treats one
  side as pure. `tier0` calls a statement impure for arithmetic on a binding
  (`a = 2 * b + 1`, 20 in `editor`), `new Uint8Array([…])`, member reads and
  calls of local functions. Candidates for a bounded upgrade: constant-number
  bindings, native constructors with literal arguments, local callees proven
  to only allocate.
- All but 9 `tier0`-unproven `editor` pairs are also unproven by the current
  effect verdict; all `main` ones are.
- `--exports-reachable` (another chunk in an import cycle may call this
  chunk's exports during its initialization) leaves `main` unchanged and
  raises `editor` to 127,669 unproven pairs in one 1,046-module component.
  Whether a chunk is in such a cycle is a whole-program fact the real pipeline
  has; the prototype takes it as a flag.

### `tier0` upgrades

Each widens "pure definition" by one bounded rule (`--upgrade`), with tests
for the near-misses that must stay impure. Stress assignment, cumulative:

| Upgrade                                               | `main` unproven | `main` modules (largest) | `editor` unproven | `editor` modules (largest) |
| ----------------------------------------------------- | --------------: | -----------------------: | ----------------: | -------------------------: |
| none                                                  |             784 |                170 (167) |             2,383 |                  111 (111) |
| operators over primitive-only bindings                |             521 |                170 (167) |             2,107 |                    50 (44) |
| + intrinsic allocators (`new Map()`, typed arrays, …) |             521 |                170 (167) |             2,076 |                    50 (44) |
| + `class X extends <intrinsic>`                       |             521 |                170 (167) |             1,153 |                    46 (40) |
| + intrinsic member reads                              |             521 |                170 (167) |             1,153 |                    46 (40) |

The committed spec stays at 0 unproven pairs throughout.

The intrinsic rules consult a census of what the chunk itself writes on
globals (`x/summary_effects/intrinsics.rs`): a use is pure only if the global
binding and the properties the use depends on are untouched, and a global used
as a value anywhere counts as touched. That census costs `main` its eleven
`new Map()`-style statements (`typeof WeakMap == "function" ? WeakMap : Map`
aliases the constructors; without the census the allocator rule gives 515
unproven pairs and 167 modules) and both chunks every `Object.*` read. `main`
writes `Array.prototype.at` and `Error.prepareStackTrace`; neither disables a
rule, because each rule names the properties it depends on. The census sees one
chunk; writes by other chunks are a per-program fact.

A callee summary for local functions that only allocate was not built. The
statements it was meant for are not allocator calls: in `editor`, 160 of 172
local-callee statements are `X = wrap(importedNamespace.jsxs(…))`, and the
rest store into a parameter (`zero(table)`, the class-field helper).

What is left is cross-chunk. Estimates only (`--what-if-pure`, unsound: it
counts statements impure for a cause as pure), with the first two upgrades:

| Counted as pure                    | `main` unproven / modules | `editor` unproven / modules |
| ---------------------------------- | ------------------------: | --------------------------: |
| nothing                            |                 515 / 167 |                  2,076 / 50 |
| calls of imported functions        |                  268 / 94 |                  2,036 / 47 |
| calls of members                   |                 420 / 119 |                  1,704 / 47 |
| calls of local functions           |                 497 / 167 |                    650 / 50 |
| all three                          |                  123 / 35 |                    383 / 44 |
| all three and `X.p = …` statements |                   18 / 10 |                    371 / 44 |

### Preconditions

The report's `preconditions` block says, for the input at hand, whether each
assumption holds and what the analysis fell back to.

| Precondition                          | Check                                                                                                               | Fallback                                   |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------- | ------------------------------------------ |
| No `eval` or `with`                   | syntax                                                                                                              | every statement is impure                  |
| No top-level `await`                  | syntax                                                                                                              | the statement is impure; exports reachable |
| Not on an import cycle                | `--program-chunks <dir>`: static cycle, or a dynamic `import()` in the chunk's static dependencies that may load it | exports reachable (`--exports-reachable`)  |
| Intrinsics unmodified by program code | the census, over the chunk or over `--program-chunks`                                                               | the intrinsic use is impure                |
| Intrinsics unmodified by unknown code | none: a stated premise for `intrinsic_families_used`                                                                |                                            |

On the Excalidraw corpus (79 chunks, read in about 0.3 s): no `eval`, `with`
or top-level `await`; neither `main` nor `editor` is on an import cycle, so
`editor`'s exports need not count as reachable. The program-wide census,
though, finds every intrinsic the upgrades rely on used as a value in some
chunk, so with `--program-chunks` all intrinsic upgrades are refused (15 uses
in `main`, 25 in `editor`) and `editor` falls back to the primitive-bindings
row: 2,107 unproven pairs, 50 modules. The census's "used as a value means
touched" rule is the conservative part; narrowing it means following aliases
of intrinsic constructors.

The fixture `tier0_export_cycle_needs_the_precondition` shows the pair the
cycle precondition decides.

### Shapes from a large private consumer corpus

`tier0_synthetic_shape_families` fixes the verdicts for three generic shapes.

- **Decorator helper** `decorate([d], Class.prototype, "m", null)`: decorator
  statements keep their mutual order; each stays after its own class; a later
  one also stays after every class an earlier one named; unrelated
  definitions move freely. Whether the decorators are local or imported makes
  no difference to `tier0`.
- **Factory with an object literal** `var x = make({…})`: the calls keep their
  mutual order and stay after what `make` references; unrelated definitions
  move freely. `tier0` does not tell a pure factory from a registering one.
- **Wrapper with a function** `var C = wrap(function () { … })`: `C` stays
  after every binding the function references, since the wrapper may call it
  at once; wrappers keep their mutual order.

### Composing with the precise engine

`tier0` is complete: it gives every statement a verdict. The execution-based
engine sometimes proves more for one statement. A statement's impure verdict
can be replaced by a precise effect set under these conditions.

- **The set is complete and the statement makes no opaque call.** `tier0`
  charges an impure statement with everything unknown code could run. A precise
  set that omits that is sound only when no unknown code runs.
- **The statement leaks nothing, or stays ordered with every impure
  statement.** Reach stamps assume impure statements never pass each other. A
  precise statement that stores a function where unknown code can find it, then
  moves ahead of an impure statement `H`, lets `H` run that function, which
  `reach(H)` did not count. So the interface needs a `leaks` bit next to
  `complete`; a leaking statement keeps its place among impure statements even
  when its own effects are precise.
- **Against an impure statement, compare with its reach, not with cells.**
  `tier0` has no heap cells for `H`. A precise statement may cross `H` only if
  it is leak-free, its bindings are outside `reach(H)` and `H`'s writes, and
  every object it reads or writes is one it allocated and never leaked.
- **Against a pure definition, compare bindings.** A pure definition writes
  its binding and reads its eager reads; a complete precise set names its
  binding reads and writes.
- **Two precise, complete, leak-free statements** commute when their cell sets
  are disjoint.

The precise statement still counts as leaking everything it mentions when
stamps are computed for later impure statements; the stamps come from syntax
and do not change.

## Soundness fixtures

`x/summary_effects/soundness_test` holds 129 small programs. For each, Node's
output changes when two statements are swapped; an analysis must report a
conflict for that pair.

- 74 statements that reach a binding-writing function indirectly (closure
  write to an outer local, destructuring, iteration, generators, `arguments`,
  throw/catch, builtin callbacks, accessors, coercions, tagged templates,
  class members, `call`/`apply`/`bind`, …).
- 39 definition-shaped statements that run code (`const d = { [f()]: 1 }`,
  `class K extends (f(), Object) {}`, `const d = -{ valueOf: f }`, …).
- 16 whole programs for reference reach: a callback leaked by an earlier
  statement, a definition read through a leaked closure, a function-valued
  `var` observed by `typeof`, a class in its temporal dead zone.

Both analyses refuse all 129, `tier0` with and without its upgrades. `points-to` first accepted 6 of the first 74;
both causes were prototype bugs (a constraint on a bare global identifier was
never applied, so arguments of `new Map(x)` did not escape; `instanceof` did
not call `Symbol.hasInstance`), not gaps in the formulation.

Not done: the Node-divergence tests under `e2e/` have not been adapted.

## `points-to`: what convergence took

A solver with per-object field nodes did not finish on one chunk. Getting a
result needed:

1. property value flow merged by property name; elements, unknown-key stores,
   `prototype` and collapsed contents stay per object; effect cells stay per
   object;
2. one coercion, iteration or native-receiver constraint per site rather than
   per object;
3. bit-vector points-to sets;
4. one abstract object per kind for the children of a literal with 16 or more
   literal children, and for the literals of each function body;
5. per-call specialization of small helpers that use a parameter as a property
   key, and `Object.defineProperty(o, k, { value | get | set })` as a store:
   esbuild's class-field helper otherwise merges every object and value;
6. three assumptions, each of which merges the whole heap when dropped:
   unknown code does not write intrinsic prototypes; a computed-key store can
   reach an intrinsic prototype only when written against a global identifier
   or a top-level alias of one; an unknown key is a well-known symbol only when
   it derives from `Symbol`, `Object.getOwnPropertySymbols`, `Reflect.ownKeys`
   or `Object.getPrototypeOf`.

Intrinsic-prototype premises are per family in the engine and were refuted on
this corpus for `Error` and `Array`; the prototype's blanket form is not
acceptable as is.

Not modeled, and what each maps to:

| Construct                                                   | Treatment                                       |
| ----------------------------------------------------------- | ----------------------------------------------- |
| `eval`, `with`, `new.target`, `__proto__`, `using`, JSX, TS | the enclosing unit conflicts with every unit    |
| `await`, `yield`, `import()`                                | call into Escaped; top-level `await` as above   |
| imports, unresolved globals, results of unknown calls       | the unknown object; reads and calls are Escaped |
| builtin methods without a summary                           | operands escape; Escaped unless all primitive   |
| a throw from a top-level statement                          | **not an effect** — a gap in this mode          |

## Running it

```sh
bbr build -c opt //devinfra/js/debundle/x/summary_effects:summary_effects_bin
summary_effects_bin --mode tier0 \
  --chunk <chunk.js> \
  --effect-report <reports/tree/<chunk>/effect_translation_validation.json> \
  --modules <reports/tree/<chunk>/modules.json> \
  --current-conflicts <effect_conflicts.jsonl> \
  --unproven-pairs <pairs.tsv> --out <report.json>
```

Only `--chunk` is required; `--effect-report` supplies the emitted order.
Without `--examples` the report holds counts, ordinals, classes and timings
only: no source text, identifiers or property names. `--mode points-to` takes
`--function-heap {site,function,global}`.
