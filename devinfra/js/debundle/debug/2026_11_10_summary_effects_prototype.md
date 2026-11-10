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
| Stress, `editor`         | 423,683 |           0 |                140,477 |     not run |   2,124 |
| Stress, `main`           | 169,613 |           0 |                 71,392 |     not run |     784 |

The stress assignment is one module per atomic group (`per_atomic_group` for
both chunks), built by the measurement in
`2026_11_10_excalidraw_module_blockers.md`; its emitted tree passes the browser
smoke, so every pair an analysis refuses there is a precision loss.

Cost on the `editor` chunk (517 KB, 1,787 source units):

| Analysis    | Time                                          | Peak RSS |
| ----------- | --------------------------------------------- | -------: |
| `tier0`     | 28 ms parse, 30 ms analysis, 32 ms pair check |    26 MB |
| `points-to` | 120 s solve, 21 s summaries, 151 s pair check |   4.9 GB |

Most of `tier0`'s analysis time is the intrinsic census (6 ms without it). The
pair check visits every pair of statements, so it is the part that grows: on
the large private consumer corpus it was 1.8 s of 2.8 s for a chunk with 18
million swapped pairs. A sweep over impure statements and reach stamps would
avoid the quadratic visit; it was not needed at these sizes.

On the inlined shape (commit `6114b352`: 75 chunks, 4.94 MB, 5,298 source
units, every chunk inlined in its entry) `tier0` with all eight upgrades and
derivation takes 0.13 s of parsing, 1.8 s of analysis and 0.09 s of pair
checks summed over the 75 chunks, at 89 MiB peak; the current engine's
analysis of that corpus took 304 s. Each chunk was run as its own process,
which reads the whole program again every time (22 s of wall time in all), so
the 1.8 s includes 75 rounds of indexing every chunk for derivation. That
shape has no swapped pairs, so the run gives time only.

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
| Unproven pairs                                              |           784 |           2,124 |
| … both statements impure                                    |           276 |           1,672 |
| … definition reachable from the impure statement's own code |            55 |              16 |
| … reachable only through earlier leaks                      |           453 |             436 |
| Statements in an unproven pair                              |           392 |             390 |
| Modules in an unproven pair, of all                         |   170 / 1,301 |     108 / 1,271 |
| Groups of modules to co-locate (largest)                    |       2 (167) |         1 (108) |
| Modules no unproven pair touches                            |         1,131 |           1,163 |

The current effect verdict puts 994 (`main`) and 1,065 (`editor`) modules in
one component.

Statements no module lists form the entry residual. The report counts damage
both ways: `module_damage` with the residual as one module,
`modules_merging_into_entry` for the authored modules an unproven pair chains
to it, and `authored_module_damage` over pairs with both statements in
authored modules, which is the measure of what authored modules must merge
with each other. The stress assignment has no residual to speak of, so the two
agree here to within one module.

- "Reachable only through earlier leaks" is the price of closing the legacy
  gate's gap (it follows a statement's own closure, not callbacks leaked by
  earlier statements).
- "Both impure" pairs are swaps the legacy gate allows because it treats one
  side as pure. `tier0` calls a statement impure for arithmetic on a binding
  (`a = 2 * b + 1`, 20 in `editor`), `new Uint8Array([…])`, member reads and
  calls of local functions. Candidates for a bounded upgrade: constant-number
  bindings, native constructors with literal arguments, local callees proven
  to only allocate.
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
| none                                                  |             784 |                170 (167) |             2,124 |                  108 (108) |
| operators over primitive-only bindings                |             521 |                170 (167) |             1,848 |                    47 (43) |
| + intrinsic allocators (`new Map()`, typed arrays, …) |             515 |                167 (167) |             1,821 |                    47 (43) |
| + `class X extends <intrinsic>`                       |             515 |                167 (167) |               902 |                    43 (39) |
| + intrinsic member reads                              |             515 |                167 (167) |               902 |                    43 (39) |

The committed spec stays at 0 unproven pairs throughout.

The intrinsic rules consult a census of what the program writes on globals
(`x/summary_effects/intrinsics.rs`): a use is pure only if the global binding
and the properties the use depends on are untouched. `main` writes
`Array.prototype.at` and `Error.prepareStackTrace`; neither disables a rule,
because each rule names the properties it depends on.

Using an intrinsic as a value is not a write. The census follows an intrinsic
constructor, its `prototype` and the global object through local bindings
(`const M = typeof WeakMap == "function" ? WeakMap : Map`), and through the
parameters of local functions called by name or where they are written, and
charges each store through such an alias to the intrinsic, by property name.
The global object is not followed into a function called by name: through
shared parameters it reached an unrelated `x[i] = …` in `main` and refused
everything. A value that goes anywhere else (a literal, a member, a return, an
argument of code the chunk does not define) has _escaped_. An escaped
intrinsic is charged only with the stores the program makes through
`<anything>.prototype`, by property name; that no other store lands on it is
a premise (§ Preconditions). On this corpus the global object escapes in
`main` and in `editor`, so every intrinsic counts as escaped; the program
makes 89 distinctly named stores through an unfollowed `.prototype` and none
under a computed key. One of the names is `toString` (a class giving itself
one), which costs `editor` its two reads of `Object.prototype.toString`.

A store through an unfollowed `.prototype` is charged to an escaped intrinsic
only for uses that depend on a member of its prototype: a native allocation
that calls one (`new Map(entries)`), array iteration, a read of
`G.prototype.x`. A subclass of an escaped intrinsic, a read of a static
member (`Object.defineProperty`), a `Symbol(…)` binding and an empty
collection depend on none, so such a store, named or not, does not refuse
them. `X.prototype[k] = v` is not recorded at all when `X` only ever holds a
function or class of the chunk: a binding declared once by one and never
assigned, or a parameter of a function that is only ever called by name with
such a binding in that position. That the global object escaped refuses
nothing by itself; it only makes every intrinsic count as escaped.

The report lists each store that counts as a write of a whole global the
language defines (`whole_write_sites`): the family, the chunk, the byte
offset of the store's target, how it counted (the binding assigned, a member
of the global object, an unnamed key on the value or on its `prototype`, a
store deeper inside), the mutating function if the store is a call of one,
and the byte offsets of the values that carried the global to the target.

A read of a static member counts as independent of every prototype only for
a name the namespace owns (`Object.defineProperty`, `Math.PI`; a fixed
table). Any other name (`Math.toString`, a name that does not exist) is
answered by `Function.prototype` or `Object.prototype` and depends on both.

A store into the record a global holds, `(x[K] ??= {})[u] = v` or through a
binding initialized with `x[K] ??= {}`, is charged to the one global `K`
names when `K` resolves to a string (a literal, a binding that only ever
holds one, a concatenation of those); `x` may be a destructured parameter
whose default is the global object. With a name the census cannot resolve
the record may be any intrinsic and the store stays unnamed.

A callee summary for local functions that only allocate was not built. The
statements it was meant for are not allocator calls: in `editor`, 160 of 172
local-callee statements are `X = wrap(importedNamespace.jsxs(…))`, and the
rest store into a parameter (`zero(table)`, the class-field helper).

What is left is cross-chunk. Estimates only (`--what-if-pure`, unsound: it
counts statements impure for a cause as pure), with the first two upgrades:

| Counted as pure                    | `main` unproven / modules | `editor` unproven / modules |
| ---------------------------------- | ------------------------: | --------------------------: |
| nothing                            |                 515 / 167 |                  1,821 / 47 |
| calls of imported functions        |                  268 / 94 |                  1,792 / 44 |
| calls of members                   |                 420 / 119 |                  1,650 / 45 |
| calls of local functions           |                 497 / 167 |                    559 / 47 |
| all three                          |                  123 / 35 |                    343 / 42 |
| all three and `X.p = …` statements |                   18 / 10 |                    331 / 42 |

### Preconditions

The report's `preconditions` block says, for the input at hand, whether each
assumption holds and what the analysis fell back to.

| Precondition                                                     | Check                                                                                                                                                                                  | Fallback                                   |
| ---------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------ |
| No `eval` or `with`                                              | syntax                                                                                                                                                                                 | every statement is impure                  |
| No top-level `await`                                             | syntax                                                                                                                                                                                 | the statement is impure; exports reachable |
| Not on an import cycle                                           | `--program-chunks <dir>` (with `--program-file-list` where chunks are stored under aliases): static cycle, or a dynamic `import()` in the chunk's static dependencies that may load it | exports reachable (`--exports-reachable`)  |
| Intrinsics unmodified by program code                            | the census, over the chunk or over `--program-chunks`                                                                                                                                  | the intrinsic use is impure                |
| Escaped intrinsics are modified only through `.prototype` stores | none: a stated premise for `intrinsic_families_escaped`                                                                                                                                |                                            |
| Intrinsics unmodified by unknown code                            | none: a stated premise for `intrinsic_families_used`                                                                                                                                   |                                            |

A dynamic `import()` closes a cycle only when some chunk of the program has
a top-level `await`. Its promise settles in a later job, and without a
top-level `await` a module graph evaluates in one job, so no job can run
while the chunk is half evaluated; the callbacks of `import()` run after.
With a top-level `await` anywhere, evaluation can be suspended, and the
earlier rule stands: a dependency that dynamically imports the chunk, or a
chunk that statically reaches it, or an unknown specifier, counts.

A program chunk that does not parse is taken to do anything: it writes every
global, awaits at its top level, and may load any chunk, so a chunk that
imports it counts as on a cycle (`program_unparsed_chunks`).

With `--program-file-list` only the files it names are read; other `.js`
files in the directory are not part of the program.

`--program-file-list` maps imported file names to chunks stored under an
alias, and `--program-specifier-root <prefix>` makes absolute specifiers
under that prefix name files of the chunk directory. An import that resolves
to no chunk is no edge, so a program whose imports do not resolve has a
vacuous cycle verdict.

`import.meta.require("<literal>")` counts as a static import wherever it is
written: it evaluates the required chunk before it returns, so a chunk that
requires one that statically imports it is on a cycle. A `require` of anything
but a string literal, in the chunk or its dependencies, counts as a cycle.

A computed store on the global object refuses every intrinsic use, unless
the census can say what the key is: a string or number, a symbol (which is
no binding's name), a literal prefix (``window[`cb_${id}`]``, which touches
only globals whose names start so), or a binding declared once and never
assigned that holds one of those. A loop variable over an array literal of
strings or of `[name, value]` pairs, or over the keys or entries of an object
literal, stands for those names. `Object.assign(target, { a, b })` writes
`a` and `b`. A key built from such bindings counts too: `P + this.id` and
`` `${P}${id}` `` start with what `P` holds, and `A + "_registry"` is one
name when `A` holds a string. A store into a record read off the global
object (`root.registry.items[k] = v`, or through `root[K] ??= {}`) is a write
to that global, not to all.
`global_object_unnamed_store_sites` lists the refusing stores as chunk index
(in path order) and byte offset.

The escape premise does not cover an intrinsic obtained without naming it
(`[].constructor`, `Object.getPrototypeOf(x)`) or through a destructuring
pattern; the census does not see those either way.

On the Excalidraw corpus (79 chunks, read in about 0.3 s): no `eval`, `with`
or top-level `await`; neither `main` nor `editor` is on an import cycle, so
`editor`'s exports need not count as reachable. The program-wide census
refuses 2 of `editor`'s 38 intrinsic uses (the `toString` reads above) and
none of `main`'s 17; the unproven pairs and modules are those of the last
upgrade row. Before the census followed aliases it refused all of them, and
`editor` fell back to the primitive-bindings row.

The fixture `tier0_export_cycle_needs_the_precondition` shows the pair the
cycle precondition decides.

### Premises and the contained class

`--premises <json>` gives tier 0 trusted contracts. Tier 0 does not check
them.

```json
{
  "callees": [
    {
      "callee": { "by": "import", "specifier": "./dep.js", "export": "build" },
      "kind": "pure_call"
    },
    { "callee": { "by": "local", "unit": 12 }, "kind": "pure_new" },
    {
      "callee": {
        "by": "import_member",
        "specifier": "./dep.js",
        "export": "*",
        "member": "lazy"
      },
      "kind": "callback_factory"
    },
    {
      "callee": { "by": "local_member", "unit": 3, "member": "make" },
      "kind": "pure_call"
    },
    {
      "callee": { "by": "local", "unit": 4 },
      "kind": "decorator_helper",
      "target_arg": 1
    },
    {
      "callee": { "by": "import", "specifier": "./dep.js", "export": "field" },
      "kind": "decorator"
    }
  ],
  "sites": [
    { "unit": 40, "kind": "pure" },
    { "unit": 41, "kind": "native_operation" }
  ],
  "intrinsics_never_written": ["Map", "Set"],
  "commuting": [[17, 230]]
}
```

A callee is named the way the chunk names it: `export` is the exported name,
`default`, or `*` for a namespace import; `unit` is the source-unit ordinal
that declares the binding. The call may be written `f(…)`, `ns.member(…)` or
`(0, ns.member)(…)`. Every field but `callee` and `kind` is optional
(`target_arg` defaults to 1); unknown fields are rejected. The report gives
`premises_given`, `premises_matched` (callees that named a binding of this
chunk), `premise_uses` per kind, `site_premise_uses` and
`premised_reading_imports`.

A callee may be given several kinds. One that is both `pure_call` and
`callback_factory` only allocates and retains its arguments: its call is a
pure definition that reads neither its callbacks' bodies nor its argument
objects. A callee that is both `decorator` and `callback_factory` is a
decorator where a helper applies it and contained where it is called. A site
`pure` premise overrides a contained callee.

A callee premise applies only where every argument is itself a pure
definition (literals, functions, bindings, nested premised calls) and none is
spread.

| Kind                  | Spec counterpart                                       | The author asserts, for every such call at the chunk's top level                                                                                                                                                                                                      | Class                                                            |
| --------------------- | ------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| `pure_call`           | `pure_exports`, `pure_members`, `purity: pure`         | The call cannot throw, writes nothing but its fresh result, keeps no reference to its arguments outside the result, and reads only its arguments, what they reference, and state nothing in the program's initialization changes. A function it is given it may call. | pure definition                                                  |
| `pure_new`            | `pure_new_exports`, `purity: pure_new`                 | The same for `new F(…)`. It says nothing of `F(…)`.                                                                                                                                                                                                                   | pure definition                                                  |
| `callback_factory`    | `callback_factory_exports`, `callback_factory_members` | The call cannot throw and returns a fresh result that retains its arguments: it calls none of the functions among them and reads none of the objects among them. It reads and writes nothing of the importer. It may read and write state private to its provider.    | contained                                                        |
| `decorator_helper`    | the decorator-helper effect family                     | `f(decorators, target, …)` cannot throw, applies each decorator to the object at `target_arg`, and otherwise writes only that object. It invokes no method, accessor or constructor of the target, publishes nothing, and returns the target or a replacement.        | target-local, if every decorator has a premise; otherwise impure |
| `decorator`           | none                                                   | The function, or the function it returns when called, is a decorator: applied by a helper it cannot throw, writes only the target it is given, and reads only that target and its arguments.                                                                          | lets a helper statement be target-local                          |
| `contained_decorator` | none                                                   | A decorator that may also read and write state private to its provider.                                                                                                                                                                                               | target-local and contained                                       |

Site kinds: `pure` (`initializer_purity: pure`) makes the unit a pure
definition that reads every binding it mentions outside function bodies and
what those reference; `native_operation` (`native_operation_inputs`) waives
the intrinsic census for the unit. `intrinsics_never_written` waives it for a
global everywhere; `commuting` certifies a pair (`commuting_statements`).

What tier 0 still orders, per class:

- **Pure definition by premise.** It reads the bindings its arguments can
  reference, including those the bodies of function arguments mention (the
  callee may call them). It stays after their definitions and ordered against
  any impure statement that can reach or write one. An argument that reads an
  import keeps it ordered against every impure statement
  (`premised_reading_imports`): another chunk's code may change what the
  import holds.
- **Contained.** A definition whose call may touch state private to a
  provider: the import source of the callee, or this chunk for a local
  callee. Against definitions it behaves as a pure definition whose eager
  reads are those of its arguments, without the bodies of function arguments;
  reach does not apply, because the call runs no code of this chunk. It stays
  ordered against every impure statement, which may call into any provider,
  and against every contained or target-local statement of the same provider
  (`provider_state`). Its result holding a callback is not a leak: the
  callback can only run through the result binding, and a statement that
  mentions that binding has the callback's mentions in its reach.
- **Target-local.** `f([…], T, …)`, `f([…], T.prototype, …)` and
  `T = f([…], T)`, with the decorators as the first argument. Ordered after
  `T`'s definition, against any impure statement that can reach `T`, against
  any definition that reads `T` eagerly or through a premised call, and
  against any other target-local statement on `T` (`shared_target`). With a
  `contained_decorator` it is also contained in that decorator's provider.

Two points differ from what was asked, both for soundness:

- A helper statement with a decorator that has no premise stays fully impure.
  The helper premise says the helper invokes nothing of the target; an
  unknown decorator may, so the statement keeps the target's whole closure in
  its reach.
- Decorator statements that share a `contained_decorator` provider stay
  mutually ordered. Only `decorator` (no private state) lets two classes'
  decorator statements pass each other.

`premise_fixtures.txt` holds 39 programs, each run under Node in both orders:
17 where a premise lets a swap through and Node agrees, 18 where the premises
hold but the swap is observable and tier 0 refuses, and 4 where a premise is
false (a `pure_call` that counts, a `callback_factory` that calls its
callback, a `decorator` that writes a shared registry, a `decorator` that
reads outside state) and tier 0 accepts a swap Node observes. Without
premises tier 0 refuses all 39.

### Derived contracts

`--derive` reads callee bodies instead of taking premises; with
`--program-chunks` it follows imports into the providing chunk
(`x/summary_effects/derive.rs`). Three derivations, all syntactic:

- **Allocator.** A function whose body only builds a fresh value from its
  parameters: literals, closures, parameters and never-reassigned top-level
  bindings as values, `p === void 0 ? … : …`, stores into objects the body
  itself allocated, calls of other allocators, functions declared in the
  body (declaring one runs nothing, and an `async` or generator body runs
  only when called), `new` of an empty `Map`, `Set`, `WeakMap` or `WeakSet`
  where the census leaves that intrinsic alone, and `new` of a class whose
  constructors only store values into `this` and whose field initializers
  are themselves in this grammar, with a base class of the same kind.
  Accessors may be defined, as long as no constructor of the class or its
  bases stores under a name that has one. Parameters may have defaults that
  are constant data. It calls no parameter and
  cannot throw. A call with pure-definition arguments is a pure definition
  that retains its arguments and eagerly reads the top-level bindings the
  body uses. Two extensions:
  - It may read `param.name`; the call must then pass a plain object literal
    (no accessor, spread or computed key) at that position.
  - It may step a counter, append to a list (`push`, `unshift`,
    `list = list.concat(…)`) or add to a `Map`/`Set` that is a top-level,
    unexported variable of its chunk which the chunk assigns in no other
    way. A call is then _contained_ in that chunk.
- **Bundler helpers, by shape.** The lazy CommonJS wrapper and the interop
  namespace helper are recognized by comparing their bodies, up to renaming
  and aliases of `Object.*`, with canonical sources; so is the
  function-naming helper, which is pure when its first argument is a
  function or class written in place. A helper written
  differently is not recognized; the premise kinds `lazy_module`,
  `interop_namespace` and `allocator` state the same contracts by hand.
- **Primitive imports.** An imported binding only ever holds a primitive
  when its exporting chunk declares it once, never assigns it, and
  initializes it with operators over literals and other such bindings. Off
  an import cycle the exporting chunk has run before the importer starts, so
  the binding already holds its one value: it joins the importer's
  primitive-only bindings and needs no ordering. On a cycle, or without the
  program, it stays a foreign read.
- **Settled imported literals.** `imp.member` is a pure definition (and a
  primitive, when the member is written as a primitive literal) if the
  exporting chunk declares the binding once, never assigns it, initializes
  it with an object or array literal of pure parts, and no statement of any
  chunk mentions the binding other than to read a named member, import it or
  re-export it. Any other mention (a store, a call receiver, an argument, a
  spread, a computed read) disqualifies it, as does a chunk that is opened
  whole (a namespace import, a dynamic load, `export *`), an import that
  does not resolve inside the program, or any load of an unknown specifier.
  It needs the chunk off an import cycle, like primitive imports. Excalidraw
  has 22 such imports in `main` and 1 in `editor`; the stress numbers do not
  move.
- **Aliases of a lazy module's member.** `const alias = ns.member` (or
  `ns.default.member`), declared once and never assigned, in this chunk or
  exported by another, is an allocator when the member is: the module has
  assigned its exports by the time the alias is declared.
- **Members of a lazy module.** `ns.member` and `ns.default.member`, where
  `ns` is a lazy module's exports directly or through the interop helper, are
  resolved to the single top-level `exports.member = …` of the module body
  (following `module.exports = other()`), and classified as allocators.

A lazy module's thunk runs the module body the first time it is called. The
first definition in source order that calls a thunk is _contained_ in every
provider (the body is arbitrary code of another chunk) and conflicts with any
definition an earlier impure statement leaked, since the body may call what
was leaked. If the body is in this chunk the statement stays impure. Every
later definition that calls the same thunk is a pure definition that stays
after the first one and, because it reads the provider's exports object,
ordered against every impure statement.

Not derived, by decision: anything with a loop, a branch or a call outside
the allocator set (the JSX factory copies its argument in a loop). Not
resolved: classes and helpers declared inside a lazy module's body, `export
*`. Not checked: that a lazy module's exports are assigned only by the
top-level statements of its body.

`derive_fixtures.txt` holds 31 two-chunk programs run under Node in both
orders (16 admitted with Node agreeing, 15 refused with Node showing the
divergence). `tier0_allocators_stop_at_their_near_misses` lists bodies that
are allocators and near-misses that are not, and
`tier0_derivation_classifies_callee_families` has one synthetic definition
per callee family of the large private consumer corpus with the class
derivation gives a call of it: context creator, message-catalog definer,
route or lazy-panel definer, schema-object builder, function-naming helper,
CommonJS-style wrapper, `memo`- and `forwardRef`-style wrappers and an
element factory over a literal are pure; a registering store creator, a
lazy-thunk wrapper that registers a reset hook and a numbering factory are
contained; a store creator that calls its setup, the naming helper on an
existing function, an element factory that copies its argument and a
registering factory whose list is reassigned elsewhere stay impure.

Excalidraw, all upgrades, program-wide census. The hand-written premise files
are 2 callees for `main` (the action-registering factory, which derivation
now also finds, and a local atom factory, both `callback_factory`) and 3 for `editor` (the icon wrapper and the
two JSX factories, `pure_call`); they are premises written by reading the
callees, not derived.

| Stress assignment       | `main` unproven | `main` modules, authored (largest) | `editor` unproven | `editor` modules, authored (largest) |
| ----------------------- | --------------: | ---------------------------------: | ----------------: | -----------------------------------: |
| upgrades only           |             515 |                          166 (166) |               902 |                              42 (38) |
| + derivation            |             271 |                            52 (52) |               822 |                              40 (36) |
| + hand-written premises |             258 |                            51 (51) |               722 |                              40 (36) |

| Statements impure for a call of | `main`: upgrades → derived → with premises | `editor`       |
| ------------------------------- | ------------------------------------------ | -------------- |
| an imported function            | 434 → 4 → 4                                | 26 → 4 → 4     |
| a member                        | 82 → 14 → 14                               | 32 → 15 → 13   |
| a local function                | 31 → 34 → 19                               | 172 → 178 → 51 |

- Derivation alone resolves all 325 `interop(lazy())` statements of `main`
  (8 first uses, 317 later ones), its 12 wrapper calls, 68 member calls
  (`forwardRef`, `memo`, `createContext`, …), and its 90 calls of the
  action-registering factory, which appends to a private list and is
  contained; in `editor`, 14 wrapper calls
  and 17 `memo` calls. The 8 first uses of `editor`'s lazy modules are of
  local bodies and stay impure, now counted as local calls.
- None of `editor`'s 160 icon statements (a local wrapper around a JSX call)
  is derived: the wrapper calls a class-name helper and the JSX factory reads
  its argument. With the three premises 127 of them become pure definitions;
  the rest have an argument that is not a pure definition.
- `editor` keeps 404 `object_state` pairs with premises: 101 icon statements
  pass one shared attributes object, and four impure statements come after
  an impure statement that can reach it.
- The committed spec stays at 0 unproven pairs in every row.
- `main`'s 317 later uses stay ordered against every impure statement
  (`premised_reading_imports` 320).

An impure declaration is now charged with writing its bindings only when it
runs, not whenever a later impure statement runs; that alone took `editor`'s
upgrade row from 1,153 to 902.

### Later upgrades

Four more `--upgrade` values, each a rule for a statement shape that stayed
impure:

- `local-heritage`: `class X extends B` where `B` is a local binding that
  only ever holds one class (declared once by a class declaration or a class
  expression, never assigned). The definition reads `B` eagerly and runs
  nothing: a class's `prototype` cannot be replaced. A function as base does
  not qualify, since its `prototype` can.
- `static-fields`: a static field whose initializer is a pure-definition
  expression; it defines an own property of the new class.
- `literal-member-reads`: `table.key` where `table` only ever holds one plain
  object literal that has `key`. The definition reads `table` eagerly and
  stays ordered against any impure statement that can reach the object. It
  is a pure definition only if no impure statement can have reached the
  object before it, which is decided by assuming every such read pure and
  then making impure each one an earlier impure statement reaches, until
  none is left.
- `import-reads`: a read of an imported binding is a pure definition that
  stays ordered against every impure statement, since another chunk's code
  may change what the import holds.

Two rules ride on those upgrades and the first four:

- A computed key, in a class or an object literal, converts without running
  code when it is a literal, an expression over primitive-only bindings, a
  well-known symbol (`Symbol.iterator`), a binding that only ever holds one
  `Symbol(…)`, or an entry of a literal table written as a primitive (under
  the rule for property reads of literals).
- `{ ...table }` copies data properties when `table` only ever holds one
  plain object literal that no impure statement can have reached, and
  `[...list]` reads elements when `list` only ever holds one such array
  literal and the census leaves `Array.prototype[Symbol.iterator]` alone.

Conversions the corpus still has (19 statements in `main` and `editor`) are
mostly not of that kind: tests of the host (`typeof document < "u"`,
`"clipboard" in navigator`), arithmetic over an intrinsic constant
(`Math.PI / 12`), template literals around a call of a global function, and
spreads of arrays. None is a conversion of an object with a default
`toString`.

`extension_fixtures.txt` holds 27 programs run under Node in both orders (11
admitted with Node agreeing, 16 refused with Node showing the divergence:
a base that is rebound, a function base whose prototype is replaced, a
literal changed between, a property made an accessor by an earlier
statement, a property the literal lacks, a guarded polyfill
`G.m || (G.m = f)` against a read of `G.m`). A named write refuses only
reads of that member: the polyfill commutes with a read of another static of
the same global. The 129 order-dependent statements
of § Soundness fixtures are refused with all eight upgrades on.

Excalidraw, stress assignment, program-wide census, unproven pairs / authored
modules in a pair (largest group):

| Configuration                         |          `main` |      `editor` |
| ------------------------------------- | --------------: | ------------: |
| first four upgrades                   | 515 / 166 (166) | 902 / 42 (38) |
| all eight upgrades                    |     341 / 4 (4) | 464 / 29 (25) |
| all eight + derivation                |     128 / 4 (4) | 422 / 27 (23) |
| all eight + derivation + 2/3 premises |     106 / 3 (3) | 372 / 27 (23) |

`main`'s drop from 166 modules to 4 is one statement: an object literal that
reads two imports. As an impure statement it conflicted with every definition
an earlier impure statement had leaked (174 pairs across the chunk); as a
definition that reads imports it conflicts only with the impure statements,
which share a few modules. The committed spec stays at 0 unproven pairs in
every row.

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

### The pipeline's refusal controls

`e2e_rejection_fixtures.txt` holds every fixture the pipeline's `e2e/` tests
run through `run_emit_on_rejection_fixture` on `devel` at `bd88ea50`: 88
distinct pairs of a single-chunk source and an emitted order, from 14 test
files, with the current engine's effect verdict of that order. They were
collected by making that helper write the source and its
`effect_translation_validation.json` to the test's undeclared outputs in a
scratch checkout; nothing in `e2e/` is changed here.

- The engine's effect verdict refuses 79 of the orders (60 `conflict`, 19
  `incomplete`). `tier0` refuses all 79, with no upgrade and with all eight
  upgrades and derivation; `tier0_refuses_the_orders_the_engine_refuses`
  holds it to that.
- The engine's effect verdict accepts the other 9; the pipeline rejects them
  because a statement would assign a binding through a read-only import, a
  question of placement that neither effect analysis decides. Of these, 3
  swap nothing, `tier0` refuses 4 anyway, and it accepts 2, as the engine's
  effect verdict does: a function declaration moved after its callers, and
  `new` of a class with no constructor moved before a binding its methods
  write.

Those 2 are not a gap in `tier0`. Where a binding may be assigned from, and
whether a split would assign one through a read-only import, are placement
obligations the pipeline checks separately from the order of statements; an
effect verdict that accepts such an order is correct, and the pipeline still
rejects the split.

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
`--derive` enables § Derived contracts; § Later upgrades lists four more
`--upgrade` values. `--upgrade` takes any of `primitive-bindings`, `native-allocators`,
`intrinsic-heritage`, `intrinsic-member-reads`; `--program-chunks <dir>` reads
every chunk of the program for the cycle check and the census; `--premises
<json>` is described under § Premises.
Without `--examples` the report holds counts, ordinals, classes and timings
only: no source text, identifiers or property names. `--mode points-to` takes
`--function-heap {site,function,global}`.
