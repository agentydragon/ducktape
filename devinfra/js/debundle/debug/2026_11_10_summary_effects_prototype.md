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

Both analyses refuse all 129. `points-to` first accepted 6 of the first 74;
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
