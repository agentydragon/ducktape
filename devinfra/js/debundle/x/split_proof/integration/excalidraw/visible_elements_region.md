# The visible-elements call in the pinned Excalidraw scroll path

The earlier [scroll pair](scroll_refusal.md) moves original units `#1337` and
`#1338`. Its smaller wrapper crossing keeps `[4,5,1337,1786]`.
In the same pinned editor, `calculateScrollCenter` (`#1338`, `Qb`) calls
both `#1337` and `getVisibleElements` (`#1745`, `Lm`). The lexical closure
of the scroll pair has 337 rows before export metadata and still includes
source imports `#0,#1,#3,#4`. Following the filter edge at `#1745` instead
gives a closed region with six executable rows. The original filter calls
`isInvisiblySmallElement` (`#1699`), which calls the two type predicates.

| Original ordinals  | Authored owner                               | Retained code                                                             |
| ------------------ | -------------------------------------------- | ------------------------------------------------------------------------- |
| 616, 617, 618, 621 | `packages/excalidraw/element/typechecks.mjs` | The free-draw and linear predicates and their type tests                  |
| 1699, 1745         | Entry                                        | The small-element predicate and its visible-elements consumer             |
| 1786               | Entry                                        | Original export aliases `hc`, `kc`, `zj` for the retained public bindings |

The projection copies the six declarators in source order, restores the
shared `var` keyword per declarator, and keeps only the three relevant
specifiers from the original export row. There are no free global names,
static source imports, dynamic imports, or `import.meta` sites. The authored
modules evaluate in the order shown, preserving every projected ordinal.
The pinned chunk is `chunk-PN7KOLDJ.js` at SHA-256
`fa1cfc7930fe3c0e2b4d95e6cb14ebde21068014cd92db303622509d9a0d455c`;
the projection is
`3653cc7b60e31948d0945861863790696164a9b12348e7fb2d744900cfa4877e`.

The flat source and the hand-wired Node files both keep `box`, `line`, and
`freedraw` in order, with the same object references supplied to the filter.
The observer calls the original private filter from inside a separate copy
of the entry, so the proof input needs no added exports. A separate control
changes only the import for the private free-draw predicate: it points to
the original linear-type test, which expects a type string instead of an
element object. That layout incorrectly keeps `short-freedraw`. The
observations execute the actual filter using static module files.

The full chunk still refuses the unchanged scroll-pair request before any
placement proof. The authenticated decoder stops at original `#41`,
`unsupported global Uint8Array`. The visible-elements projection decodes, and
the diagnostic planner finds zero order conflicts. The ordinary CLI still
returns `no_constructed_rule` and creates no output to recheck. The indexed
inline constructor probe refuses at the standalone export row. The ordinary
local-export route handles concrete `const` initializers, while the callable
routes begin with `FunctionDeclaration`. These six rows are initialized
`var` arrows. The missing admission for this fixture is their order-preserving
placement with the original alias row and crossed captures; the measured
refusal does not require a host or loader extension.

```sh
bbr test //devinfra/js/debundle/x/split_proof/integration/excalidraw:visible_elements_region --test_output=all
```

The artifacts include both authored requests, the projection, the diagnostic
plan, the Node layouts, and separate full-chunk and region results.

## Checked initialization and the first call boundary

The test runner checks the actual sequential plan with the sparse source
schedule and placement checks. Lean's ordered-initialization proof checks
both linker results, every statement and its captured cells, and the live
export cells. `var` allocates a mutable `undefined` cell during linking.
Executing the original initializer allocates the closure in the row's
region and assigns it to that cell. The proof retains the complete resulting
state at the same fuel, so each allocated function keeps its identity.
The runner uses the production printer and staged ESTree/CoreJS comparison
for this proof, then independently rereads the test artifacts. The public
aliases resolve to `hc → (3,0)`, `kc → (4,0)`, and `zj → (5,0)` in both
layouts.

On the staged files Node again returns a fresh array containing the
same element objects. Rewiring the private predicate is rejected on disk
and reproduces the extra short free-draw element in Node. An eager
`typeof st` before the original initializer returns `"undefined"` in the
flat source but `"function"` if the typechecks owner runs first. The authored
plan check and the ordinary CLI reject that request; the previous output is
unchanged.

At fuel 128 the actual `Vc` call for the short free-draw element completes
with `true` in both CoreJS programs. The actual `Lm` call on a modeled array
reaches `unknown: host function Array.prototype.filter is called`. The
method dispatch has not run the filter callback or allocated any returned
array. With an ordinary receiver that supplies its own `filter`, dispatch
calls that property; defining an own non-index property on an array is
separately unsupported by the current model. No intrinsic assumption or
call certificate follows from the equal initialization states. Ordinary
`lowersplit` still refuses the original region without publishing.

[The staged replay and axiom audit](https://app.buildbuddy.io/invocation/39c7a34d-d0f1-4f22-86f8-2fc0c7a3f785)
exercise the model and Node on this fixed projection.

The [extended replay](https://app.buildbuddy.io/invocation/cad2e2ff-0988-42c9-b087-e2d2cf436852)
calls `Vc` through the actual `zj` export. The proof checks that observations
through the same public cell start from the same complete initialized state
at equal fuel. On a supplied ordinary receiver, the original `Lm` calls the
supplied `filter` with its own callback. The callback excludes the short
free-draw element and retains the original rectangle. If the first element's
getter for `isDeleted` throws, the exact sentinel escapes, the receiver's
first write survives, and the second element is never tested. The wrong
private-cell layout keeps both objects in Node. These observations leave
the native-array filter boundary and ordinary CLI refusal in place.
