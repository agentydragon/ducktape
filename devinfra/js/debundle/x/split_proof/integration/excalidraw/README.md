# Excalidraw CoreJS proof and artifact coverage

## Certified placement

The goal is a runnable, Lean-certified split of the pinned bundle that recovers the files and members of [Excalidraw v0.18.1](https://github.com/excalidraw/excalidraw/tree/v0.18.1/packages). Review each proposed owner against that source. Track the members still in the entry and the reason each group cannot yet move. The certificate establishes the requested CoreJS guarantee for the emitted placement; source evidence establishes whether a module is a faithful recovery.

| Placement                                                                                     | What the evidence establishes                                                                                                                                                                                                        |
| --------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| [Checked-in editor subset](#browser-smoke)                                                    | Rust resolves the selected owners and import priority. Lean certifies the emitted modules and rechecks them from disk under the browser deployment contract. Chromium exercises the overlay. This is the baseline to extend.         |
| [236-unit scroll proposal](proposals/scroll/README.md)                                        | Additional upstream groups passed emit, disk and Chromium checks with the recorded Lean order. The same owners were refused with the authored priority. The proposal and its order are separate from the checked-in smoke selection. |
| [Full authored editor and main replay](#building-and-replaying-the-authored-excalidraw-specs) | Compares placement and execution order with the Rust reports and records refusals. It does not establish a certificate for the full split.                                                                                           |

## Diagnostic census

Run the experimental Lean checker against the public, pinned Excalidraw bundle:

```sh
bbr test //devinfra/js/debundle/x/split_proof/integration/excalidraw:excalidraw_certificate_census
```

This manual investigation target inventories every JS chunk. It probes the main
entry, shared editor, shared roughjs, and smallest chunk with more than ten
source statements, plus every chunk for which the frontend reports no module
correspondence refusals. Each probe uses the actual Effects output, with
default premises, and the CoreJS frontend with outside imports treated as the
world. No source code, import.meta expression, or unsupported statement is removed.

For each selected chunk, the harness tries all statements in one module
(`identity`), a prefix in another module (`prefix`), and alternating statements
in two modules (`alternating`). Imports and exports stay in the entry module.
These are diagnostic placements, not recovered upstream module boundaries or a
translation of the Rust Excalidraw spec. They are not a search over partitions.
Two editor probes keep the worker factory (unit 872) in the entry. One moves the
usual prefix except for the factory (`prefix-fixed-factory`), and one stops the
prefix just before it (`prefix-before-factory`). Both use the original chunk
filename so the entry can be served at the original URL. The other probes use
`entry.mjs`. A successful investigation target means the measurements
completed, not that its splits were certified or executed successfully in a browser.
The census results below are historical structural and CoreJS model diagnostics.
Only an artifact-verified success from the normal `lowersplit` command certifies
emitted JavaScript; diagnostic model proofs do not validate the printer's output.

Each subprocess has a 180-second budget. A killed process is reported separately
from rejection. Outputs retain the complete AST, model, actual footprints, plans,
checker stdout/stderr, and `--move-report` JSON. The latter reports individual
statement categories and up to 64 failing inverted pairs in original source IDs;
it is diagnostic evidence, not a certificate bypassing the checker's other premises.

For the main entry, the [first renderer placement](main.md) isolates the missing proof for a retained module metadata reader.

## Building and replaying the authored Excalidraw specs

```sh
bbr build --remote_build_event_upload=all //devinfra/js/debundle/x/split_proof/integration/excalidraw:excalidraw_lean_specs
bbr test //devinfra/js/debundle/x/split_proof/integration/excalidraw:excalidraw_authored_placements
```

The build translates the Rust debundler's `spec_graphs` reports into
`excalidraw_lean_specs/editor.json` and `excalidraw_lean_specs/main.json` in
the Bazel output directory for this package. Both are `lowersplit` specs.

Only `editor` and `main` have named module assignments in the Excalidraw spec.
The shared roughjs chunk stays in its entry. The vendor swap on the roughjs
entry stub is a separate transformation.

The replay gives Lean the generated specs and independently reads the Rust reports.
It compares every original source unit's owner and evaluation position in Lean's
plan with the Rust reports, including on rejection.
Lean derives its own imports and exports and checks its own footprints. This
measures agreement on placement and execution order; it does not validate the
Rust emitter's JavaScript or vendor substitutions.

| Chunk  | Units outside entry | Nonentry modules | Inversions in Rust order | Lean rejection                              |
| ------ | ------------------: | ---------------: | -----------------------: | ------------------------------------------- |
| Editor |        272 of 1,787 |               35 |                  295,384 | `OrderOk`                                   |
| Main   |      1,628 of 1,628 |               43 |                  136,940 | Static imports 0–7 outside entry; `OrderOk` |

Main's 43 modules include 19 generated residual groups. Editor passes the other
eight structural checks. One `OrderOk` witness reverses adjacent editor units:
474 evaluates `S8.includes(devicePixelRatio)` in the entry, and 475 declares
the arrow function `Ii` in `packages/excalidraw/appstate`. Lean's footprint for
474 includes `Ii` among 1,545 possible writes. Moving the declaration ahead of
the call therefore reverses a pair that Lean requires to stay ordered. The next
question is which possible writes the checker can soundly exclude for that call.
Both replays stop at structural checks before reaching the module URL guard.
[Exact schedules, checker logs, and move reports](https://app.buildbuddy.io/invocation/03ee0f61-d9eb-48d8-b30e-63e9cf62e0b8)

### Strict certification of the scroll module

```sh
bbr test //devinfra/js/debundle/x/split_proof/integration/excalidraw:excalidraw_scroll_certificate
bbr test //devinfra/js/debundle/x/split_proof/integration/excalidraw:excalidraw_scroll_single_certificate
```

The generated editor spec assigns `isOutsideViewPort` (unit 1336) and the two
exported helpers (1337 and 1338) to `packages/excalidraw/scene/scroll.mjs`.
CI keeps the two exported helpers as the strict namespace-refusal replay. The
on-demand single trial moves only `#1337`. Each keeps the other units in source
order in `chunk-PN7KOLDJ.js`. The replay resolves the stable `editor.js` alias
via the generated JS roster and runs the normal splitter on that original
esbuild file beside its actual providers with the `browser-page` profile. It
checks that all 13 `import()` and
`import.meta` sites stay in that entry, the 11 relative dynamic imports resolve
to existing bundle files, and the scroll module uses a new asset path.
The decoder retains the React factory in statement 5, lowers the `Uint8Array`
constructor reference in statement 41 and accepts the flat object bindings in
statements 104 and 105. The concrete-checker trials lower the module-level
`try` and `for` in statements 176 and 178. They reject the dynamic import
in statement 354 and leave the output directory absent.
Each target records the source filename, placement spec and verdict.

One target needs the existing editor module record. Unit `#1751` imports
`image-GAAHSSAO-YDUOJAAS.js`, which statically imports `ok`, `pk` and `qk`
from `chunk-PN7KOLDJ.js`. This unit follows the moved scroll function in both
orders. A behavioral proof must preserve the live export cells seen on re-entry
and account for the promise returned by the loader.

Both targets render two diagnostic placements with `split_preview … --force` into separate
directories, reparses their entries and compares their public names against the editor's 611 names.
Those outputs are uncertified previews. The `--plan` report counts source-order conflicts in
Lean's structural checker; it is diagnostic evidence only.

| Units moved                         | Entry names added    | Entry names lost | Inverted conflicts | Failed check |
| ----------------------------------- | -------------------- | ---------------: | -----------------: | ------------ |
| Exported scroll pair, 1337 and 1338 | `Eb$1`, `Lm`, `qb$1` |                0 |                518 | `OrderOk`    |
| 1337 only                           | none                 |                0 |                259 | `OrderOk`    |

Unit 1337 creates the arrow `Jb`. Its body reads no module bindings. The other arrow, `Qb`,
reads `Jb` and five bindings resident in the entry. The renderer imports `ct` and `vn`
through their existing public names and adds exports for the other three. Comparing export
names here detects additions and removals. Artifact certification also needs to establish
which live binding each name denotes.
[Alias replay](https://app.buildbuddy.io/invocation/50a13338-069e-4061-a8b3-d049f7740076)

The first five conflicts for the single-arrow placement are with units 5–9. Unit 5 is the
first statement after five named-import declarations and initializes `po` with `Ve(callback)`.
The import resolves to export `b` of `./chunk-BLPUKQJE.js`. The probe calls that exact
provider export with a counting callback. Creating the wrapper calls the callback zero times.
Loading it twice calls the callback once and returns the same exports object both times.
The probe runs the provider in Node. It does not execute the complete editor.

The separate CoreJS theorem covers wrapper creation and the independent arrow declaration from
a common linked environment. Both orders reach exactly the same successful final states.
[Linked reproduction](../../lean/EsmSplit/CoreJS/WrapperLinked.lean)

The single-arrow move plan records 259 structural source-order conflicts. All
cross `#1337`, including the conflict with the first source unit after imports.
The test records them in `scroll-move-report.json` alongside
`import-boundary.json` and `entry-exports.json`. The reduced case in
[`host_profile_test.mjs`](../../translation/linked/host_profile_test.mjs) uses a
small imported wrapper and reproduces the ambient `Symbol.for` refusal. A
lexical `Symbol` binding passes strict certification and the Node control in
the existing local host-binding proof.
[Scroll experiment](https://app.buildbuddy.io/invocation/15221e72-0c85-46fe-8be6-354e108878e3)

### Certified browser deployment

The [ownership subset](../../../../excalidraw/deployment_smoke_subset.json)
selects the main renderer and 365 editor bindings in 45 upstream modules.
The standalone Rust placement resolver solves selectors across both chunks and exports
the canonical and selected placements. The smoke verifies the source hashes,
checks each selected owner against the canonical result, and retains unselected
units in their original entries. See the [recovery notes](recovery.md) for
remaining editor candidates and the [spec authoring instructions](../../../../excalidraw/AGENTS.md)
for choosing an owner from the pinned upstream source.

```sh
p=//devinfra/js/debundle/x/split_proof/integration/excalidraw
bbr test --config=ci "${p}:excalidraw_certified_browser_smoke"
```

To check the boundary in the build graph, query the smoke target's dependencies.
Of these targets, only the placement resolver should appear:

```sh
d=//devinfra/js/debundle
bazel query "deps(${p}:excalidraw_certified_browser_smoke) intersect set(${d}:debundle_resolve_placement ${d}:gate ${d}:lowering ${d}:pipeline ${d}/excalidraw:debundle)"
```

Lean emits both splits and the fixed static providers as one deployment rooted
at `main.js`. It certifies the staged files, then rechecks the published tree
with `--check-output`. Both verdicts record the browser-page contract and the
stable `Math.PI@1`, native metadata, and empty import-map premises. Chromium
compares the original and checked deployments, including the served module
bytes, editor export names, drawing, Undo, and the extracted helpers.

Run `:excalidraw_original_editor_browser_smoke` for the original app baseline.
The [deployment notes](joint.md) and [smoke test](browser_smoke_test.py) describe
the shared closure and browser replay.

### Direct AST lowering for negated initializers

On the same authored placement, adding logical negation and negated number literals to Lean's ESTree lowerer gave these counts. Each row counts source units after static imports are removed. In each replay's `moves.json`, subtract `externallyLowered` from `statements.length` to get the number Lean lowered directly.

| Chunk  | Non-import units | Directly lowered before | Directly lowered after | Still externally lowered |
| ------ | ---------------: | ----------------------: | ---------------------: | -----------------------: |
| Editor |            1,782 |                     469 |                    472 |                    1,310 |
| Main   |            1,620 |                     169 |                    179 |                    1,441 |

Both placements still fail `OrderOk`. The editor still has 70,439 inverted pairs without a move proof. These operators were already available to the move checker through the external lowering. [Baseline replay](https://app.buildbuddy.io/invocation/2f60d14e-bc44-4045-80f7-59d6c223643b) · [Unary lowering replay](https://app.buildbuddy.io/invocation/2151901a-bb18-401a-b5b7-e7fe709aaddb)

### Proved moves for negated initializers

The editor replay has 294,024 inverted pairs after excluding imports. On the
same schedule, extending the allocation proof to logical negation and negated
number literals reduced pairs lacking a move proof from 73,489 to 70,439.
All 3,050 newly covered pairs already passed the structural independence check,
leaving 19,024 such pairs without a move proof. Unit 1584, a tool-options
object initialized with `!0` and `!1`, accounts for 1,024 newly covered pairs.
Unit 103, a zlib constants object with negative error codes, accounts for 236.
The complete editor schedule still fails `OrderOk`.
[Baseline pair report](https://app.buildbuddy.io/invocation/9b56495c-aa2a-4034-a2ce-674fc72cf55b) ·
[Extended proof pair report](https://app.buildbuddy.io/invocation/30d817ef-4e12-4d32-84a2-8ace0c26cda6)

## First run: 2026-11-21

[BuildBuddy invocation](https://app.buildbuddy.io/invocation/541244a8-ccf7-415e-a939-3081ff2ac7b9).
The test completed in 82.5 seconds; no subprocess hit its budget.
The 79 chunks contain 4,937,119 bytes and 5,312 numbered source statements.
The frontend produced 3,277 modeled units and 1,806 opaque units; the remaining
229 numbered statements do not become executable units. There were no frontend
failures. Separately, the frontend flagged 14 occurrences of `import.meta` and
12 of dynamic `import()` for correspondence restrictions.

| Chunk                                    | Statements | Modeled units | Opaque units | Effects touching the world |
| ---------------------------------------- | ---------: | ------------: | -----------: | -------------------------: |
| Main (`main.js`)                         |      1,628 |           567 |        1,053 |                      1,442 |
| Editor (`chunk-PN7KOLDJ.js`)             |      1,787 |         1,119 |          662 |                        277 |
| Roughjs (`chunk-X4ON7R5K.js`)            |         73 |            37 |           35 |                          0 |
| Small shared chunk (`chunk-BLPUKQJE.js`) |         11 |             2 |            8 |                          1 |

Opaque counts are not counts of unprovable moves: existing rules cover some
opaque closure allocations. Reasons reflect the first unsupported construct
encountered in a unit, not all unsupported constructs or executed operations.

| Chunk              | Identity                                             | Prefix                                               | Alternating                                                       |
| ------------------ | ---------------------------------------------------- | ---------------------------------------------------- | ----------------------------------------------------------------- |
| Main               | Structural pass; `import.meta` blocks semantic slice | `OrderOk` fails                                      | `AssignsHome` and `OrderOk` fail                                  |
| Editor             | Structural pass; `import.meta` blocks semantic slice | Structural pass; `import.meta` blocks semantic slice | `AssignsHome` and `OrderOk` fail                                  |
| Roughjs            | Proved order-kept slice                              | Proved order-kept slice                              | Structural pass; 21 of 630 inverted pairs lack a proved move rule |
| Small shared chunk | Proved order-kept slice                              | Proved order-kept slice                              | `OrderOk` fails                                                   |

There are four proved order-kept results, three structurally accepted results
blocked by module identity, one structurally accepted reorder lacking move proofs,
and four structurally rejected plans. None of these probes uses the new
certified-binding move slice.

The first module-identity blocker in main is statement 23, a function whose body
reads `import.meta.env.MODE`. In editor it is statement 872, a worker-pool class
whose static factory compares its argument against `import.meta.url`. Leaving
these as opaque code does not remove their dependence on the file URL.

Roughjs is the useful next proof probe: its alternating plan passes all nine
structural checks, with no world effects, but the first failed move crosses
statement 10 before statement 9. Statement 9 is a derived class; statement 10 is
an opaque plain class whose methods use `Object.assign`. Expanding binding-only
operator proofs cannot cover this pair. The follow-up report separates closure
allocation coverage from missing class/heap semantics before choosing a rule.

## Follow-up: the actual missing move rules

[Diagnostic run](https://app.buildbuddy.io/invocation/46409525-90bb-4ad7-80fb-62badb4255b6)
completed in 108 seconds, with the same verdicts and no subprocess timeouts.
`FootprintsOfSyntax` passes for all four selected chunks. Accepted binding
certificates number 175 in main, 469 in editor, two in roughjs, and zero in the
small chunk. In every case every syntactically supported binding summary is
covered by its actual Effects footprint. That is useful coverage, but does not
satisfy the other premises of a complete split theorem.

All 21 failing roughjs pairs involve at least one of eight opaque class
initializers: source units 9, 10, 11, 12, 16, 67, 68, and 70. Seven are plain
classes; unit 9 has `extends`. The other endpoints are these classes, object
initializers (13, 17, 26), or string literals (66, 69). The plain classes become
opaque because their deferred bodies use constructs such as `Object.assign`,
spread arguments, exponentiation, `for…of`, or a browser global. Those bodies do
not execute just because the class is created. Today the frontend's opaque
allocation metadata recognizes functions/arrows, not classes, so the checker
cannot apply the same allocation reasoning here.

**Recommended next experiment:** preserve a plain class's creation structure while
representing unsupported method/constructor bodies as opaque code, then prove or
reuse the relevant allocation commutation rule. Keep computed keys, static
initializers, inheritance, and other eager class work explicit. Re-run the exact
roughjs plan and measure the reduction from 21 missing pairs. The derived class
is a separate semantic obligation; none of this permits labeling arbitrary class
evaluation inert. This is a better-supported priority than adding more binding
operators or implementing every unsupported method-body feature.

For main/editor, module identity is an independent workstream: determine whether
a split preserves the relevant URL/environment observation or whether the model
and emitter need an explicit source-module-identity treatment. Do not erase
`import.meta` or silently assume its value is irrelevant.

## Plain-class implementation result

The first integrated run with plain-class creation and opaque bodies is
[071626dd-6cc2-4e93-9b2b-0e41123c4dcb](https://app.buildbuddy.io/invocation/071626dd-6cc2-4e93-9b2b-0e41123c4dcb).
The census and axiom audit passed. The exact roughjs alternating placement still
passes every structural check. Missing pair proofs decrease from **21 to 7** out
of the same 630 inverted pairs. All remaining pairs cross derived class unit 9:
`(10,9)`, `(12,9)`, `(16,9)`, `(26,9)`, `(66,9)`, `(68,9)`, `(70,9)`.
The complete reordered split therefore remains uncertified; no derived-class
permission was introduced to make it pass.

| Chunk              | Allocation-only statements before | After | Whole opaque statements before | After |
| ------------------ | --------------------------------: | ----: | -----------------------------: | ----: |
| Main               |                               785 |   799 |                          1,053 | 1,039 |
| Editor             |                             1,305 | 1,322 |                            662 |   649 |
| Roughjs            |                                31 |    39 |                             35 |    27 |
| Small shared chunk |                                 3 |     3 |                              8 |     8 |

These categories overlap with inert statements; they are not a partition.
A modeled top-level class can still contain opaque bodies, which `chunks.json`
now records separately as `opaqueBodies`. Main/editor's module-identity guard and
the rejected structural plans remain unchanged. The next roughjs obstruction is
inheritance, which needs its own treatment of superclass evaluation and prototype
access; it is not covered by the plain-class allocation proof.

## Default-derived semantics result

[Census and proof audit](https://app.buildbuddy.io/invocation/278a43b6-d8cf-4bd5-8aec-1ce30a373bee)
passed with default-derived class semantics. Roughjs unit 9 is now modeled
(`opaque: false`); it remains outside `allocatesOnly` and the binding certificate.
The same alternating plan still has **7 missing proofs among 630 inverted pairs**,
with the same endpoints listed above. The other plans retain their verdicts.

The model covers this subclass's implicit constructor and opaque method body.
Closing the seven proof gaps requires stable reads of the existing superclass and
its prototype; the plain-class locality theorem does not permit those heap reads.

## Roughjs alternating split: model proof

The diagnostic checker now covers every move in the alternating placement of the 73 roughjs units.
This proves the plan in CoreJS under its stated premises; it does not verify emitted JavaScript artifacts.
All 630 inverted pairs are covered. The JSON move report records the seven
pairs listed above under `pairwiseFailedPairExamples`, sets
`certifiedClassHeritage` to true, and reports zero remaining failures.

Unit 6 binds the plain base class `T`. Unit 8 creates another subclass of
`T` and preserves its constructor and prototype. With those facts established,
unit 9 can move past the seven active declarations in the alternating plan.
For every CoreJS world, the split ends normally in a given state exactly when
the bundle does. The other eleven probe verdicts match the default-derived
census. [Census and inheritance differential suite](https://app.buildbuddy.io/invocation/6d319638-a866-4e58-a82c-144fbcff326f)

## How much can be split without modeling module URLs?

The pinned bundle contains 76 chunks (1,890 numbered source units) for which the
frontend flags neither `import.meta` nor dynamic `import()`. Main, editor, and
the subset worker are outside this count. Each row counts only placements that
move at least one unit into `part.mjs`. A unit can appear in both rows.

| Placement   | Nonempty candidates | Pass structural checks | CoreJS model proof applies | Units moved in model-proved plans |
| ----------- | ------------------: | ---------------------: | -------------------------: | --------------------------------: |
| Prefix      |                  64 |                     64 |                         64 |                               783 |
| Alternating |                  65 |                     60 |                         60 |                               801 |

Choosing the larger placement with a CoreJS model proof per chunk gives **837 units moved across
65 chunks**. The 53 nonempty locale chunks account for 742 of those units
(14 apiece); the other 12 chunks contribute 95. Each model proof covers one complete chunk under the checker's
outside-import premises. The census checks each chunk separately. It does not
establish equivalence of loading all 65 splits together.

All 64 prefixes with a model proof preserve order. Of the 60 alternating plans with a model proof,
57 invert at least one pair. All five rejected alternating splits fail `OrderOk`;
`chunk-ZQ22LJCO.js` also fails `AssignsHome`. Eleven chunks have no nonempty
placement in either family. One of those, the pica wrapper, fails even the
identity control: CoreJS expects statement 2 to declare `*default*`, while the
Effects footprint declares no name.

In editor, keeping the factory in place and moving the other 887 prefix units
fails `OrderOk`. Units 873 through 892 would run before unit 872; the move
report lists all 20 inversions as unproved.

Ending the prefix immediately before the factory moves **867 units** and passes
all nine structural checks with zero inversions. The emitted plan places unit
872 among the 920 units in the original editor filename. The CoreJS model checker still declines
a model proof because its correspondence check sees the factory's
`import.meta.url` in the complete input. This run checked the placement and
model-proof verdict; it did not verify emitted artifacts or execute the editor in a browser.
[Census results and emitted plans](https://app.buildbuddy.io/invocation/eac77bf3-3e55-42ce-b345-28952b567496)

## Editor units 474 and 475

The authored split runs the app-state factory declaration (475) before the
pixel-ratio lookup (474). Unit 442 creates `S8 = [1, 2, 3]`. Unit 474 calls
`S8.includes(devicePixelRatio)`. Both the array and the factory binding `Ii`
are exported, as `pa` and `La`. The analysis treats exports as reachable by
outside code during initialization. Its default premises allow an observer to
install an own getter on an escaped array. In a diagnostic replay, the first
reason unit 474 ran unknown code was a property read on an object held by the
world. Its direct writes excluded `Ii`. When Effects added the escaped names,
`Ii` entered the final set of 1,545 possible writes. The authored editor
placement still failed `OrderOk`.
[Diagnostic replay and editor checker log](https://app.buildbuddy.io/invocation/5dad087a-2a49-4a2d-ae87-c8b1b5bff654)

A reduced regression in `inert_escape_test` gives a host an array and a callback
that reads the factory binding. The host adds a getter at index zero. The
standard `includes(2)` reads that index and the getter calls the callback.
The source prints `undefined` and the reordered program prints `function`;
both then print the same scale and exit normally. Both Lean checkers reject
the move. With a private dense array and a primitive search value, both accept
the control and Node prints the same output for both orders.
[Executed regression and control](https://app.buildbuddy.io/invocation/bad734bf-9b16-4cdc-b1f3-842aaba0c8c4)

Certifying this pair for Excalidraw would require evidence about what outside code
can observe during this initialization, including the host global read. The test
establishes that the receiver and its observers matter under the current premises.

## Constructor calls crossing opaque arrow allocations

The editor replay after the local-negation proof has 1,619 structurally
independent crossings between opaque `new` initializers and opaque arrow
allocations. Grouping the inverted pairs by the constructor's source unit gives:

| Constructor | Initializers | Crossings | Arguments                                                    |
| ----------- | -----------: | --------: | ------------------------------------------------------------ |
| Array       |            6 |       804 | Three numeric binding reads and three arithmetic expressions |
| Set         |            5 |       426 | Arrays of string literals                                    |
| WeakMap     |            2 |       199 | No arguments or an empty array                               |
| RegExp      |            1 |       130 | Template with two substitutions                              |
| Map         |            1 |        60 | No arguments                                                 |

For unit 46, `new Array((Oi + 2) * 2)`, the preceding initializers give
`Yi = 256`, `SU = 29` and `Oi = Yi + 1 + SU`. That explains the intended
576-element sparse array. It does not establish the identity of the global
constructor at evaluation time. CoreJS lowers all five global names here to an
opaque statement. Effects tracks visible intrinsic patches and assumes P1 for
unseen patches. Its `Array` summary does not provide a CoreJS constructor or a
proof of correspondence for sparse arrays.

The executable controls in [inert_escape_test.mjs](../../translation/inert_escape_test.mjs)
move a factory initializer across (1) an in-bundle replacement of `Array`
and (2) a call to the original `Array` whose length expression runs `valueOf`.
Each original prints `undefined`, each forced split prints `function`, and both
checkers refuse both moves. A constructor-name rule would miss these cases.
The next candidate independent of intrinsic constructor identity is destructuring:
eight declarators crossing opaque arrows account for 1,072 pairs. Unit 105 reads
named fields of unit 103's zlib constants object. A proof would need to establish
which object is read and that each property access is an own data-property read.

[Pair report](https://app.buildbuddy.io/invocation/30d817ef-4e12-4d32-84a2-8ace0c26cda6)

## Flat object destructuring in the editor

All eight declarators in the constructor census follow-up now lower as property
reads in a single numbered unit. Units 105, 188, 220 and 287 read 18, 9, 11
and 8 keys, respectively, from unit 103's zlib constants object `Ui`. Together
they account for 536 of the 1,072 candidate crossings. Unit 104 reads a
different object (`s6`) between the creation of `Ui` and its first pattern.
Unit 200 aliases `Ui` before the last two patterns. A certificate needs to
establish the receiver and its descriptors in the state at the proposed swap.
The editor still has 70,439 inverted pairs without move proofs and fails
`OrderOk`.
[Authored replay and move report](https://app.buildbuddy.io/invocation/6d74c931-dd6b-4b00-b02f-39a97300c68d)
