# Excalidraw certificate coverage

Run the experimental Lean checker against the public, pinned Excalidraw bundle:

```sh
bbr test //devinfra/js/debundle/x/split_proof/experiments/translation:excalidraw_certificate_census
```

This manual investigation target inventories every JS chunk, then probes four:
the main entry, shared editor, shared roughjs, and smallest chunk with more than
ten source statements. The first three are selected from stable entry/module
identities, not content hashes. Each probe uses the actual Effects output, with
default premises, and the CoreJS frontend with outside imports treated as the
world. No source code, import.meta expression, or unsupported statement is removed.

For each selected chunk, the harness tries all statements in one module
(`identity`), a prefix in another module (`prefix`), and alternating statements
in two modules (`alternating`). Imports and exports stay in the entry module.
These are diagnostic placements, not recovered upstream module boundaries or a
translation of the Rust Excalidraw spec. They are not a search over partitions.
Even the identity control emits into a new `entry.mjs`; it does not preserve the
original module URL. A successful investigation target means the measurements
completed, not that its splits were certified or executed successfully in a browser.

Each subprocess has a 180-second budget. A killed process is reported separately
from rejection. Outputs retain the complete AST, model, actual footprints, plans,
checker stdout/stderr, and `--move-report` JSON. The latter reports individual
statement categories and up to 64 failing inverted pairs in original source IDs;
it is diagnostic evidence, not a certificate bypassing the checker's other premises.

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
