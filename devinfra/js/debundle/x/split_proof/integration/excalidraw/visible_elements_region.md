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
