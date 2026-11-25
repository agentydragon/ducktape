# Class blockers in the public corpora

The corrected [probe](README.md) found 111 class-bearing source units: 30 in
debundle cases, 26 in the six library bundles and 55 in the 79 Excalidraw
chunks. These counts include a class nested in a function or another expression;
all 111 happen to contain one class each. Of these, 78 units are allocation-only.
The remaining 33 are below. Two are inert function declarations whose classes
are deferred, so **31 units are neither allocation-only nor inert**. The 10 zod
`Error` subclasses below are five classes counted once in the normal bundle and
once in its minified variant.

This is a static feature census, like E3, not a proof that each present feature
independently prevents a move. A standard global in a deferred method, for
example, does not run when its class is created. Scope resolution distinguishes
unresolved global references from shadowing lexical bindings. “Known class”
means the heritage identifier resolves syntactically to a class definition;
it does not establish `ParentAt` or prove that the binding and constructor stay
unchanged. A unit can contribute to several rows.

| Feature in a non-allocation-only class-bearing unit | Debundle | Libraries | Excalidraw | Total |
| --------------------------------------------------- | -------: | --------: | ---------: | ----: |
| `extends` a syntactically known class binding       |        0 |         0 |          2 |     2 |
| `extends` another binding                           |        0 |        12 |          7 |    19 |
| Of those, an unresolved global binding              |        0 |        10 |          7 |    17 |
| Other heritage expression                           |        3 |         0 |          1 |     4 |
| `extends null`                                      |        0 |         0 |          0 |     0 |
| Computed class key                                  |        1 |         0 |          0 |     1 |
| Static block                                        |        1 |         0 |          1 |     2 |
| Static initializer calls or constructs              |        0 |         0 |          0 |     0 |
| Static initializer reads a global                   |        0 |         0 |          0 |     0 |
| Standard-global reference anywhere in the unit      |        1 |        16 |         11 |    28 |
| Standard-global reference eager in class creation   |        1 |        10 |          6 |    17 |
| Standard-global reference in deferred class code    |        0 |         4 |          5 |     9 |
| Eager call or construction in the enclosing unit    |        2 |         4 |          0 |     6 |
| Class occurs in deferred code                       |        0 |         6 |          0 |     6 |
| `import()` syntax anywhere in the unit              |        0 |         0 |          1 |     1 |
| `import.meta` syntax anywhere in the unit           |        0 |         0 |          1 |     1 |
| `await` syntax anywhere in the unit                 |        1 |         0 |          2 |     3 |
| Private class element                               |        0 |         0 |          0 |     0 |

The eager class-global references are `Error` in 16 units and `globalThis` in
one static block. Another Excalidraw class extends the browser global
`DOMException`. `Object`, `Map`, and other standard globals also appear in
deferred code or elsewhere in the containing units. The two same-source class
bindings are Excalidraw roughjs units 8 and 9; their `Object` and `Math`
references are deferred. The two inert library units contain a deferred class
that extends another local binding.

The following table partitions all 33 units by the selected overlapping
features. `G-eager` and `G-deferred` refer to standard globals inside the
class; `G-unit` means one appears anywhere in its containing unit. `B` is an
`extends` binding without a syntactically known class definition, `K` a known
class binding, and `X` another heritage expression. `Call` is an eager call or
construction anywhere in the containing unit. The labels describe presence,
not independent semantic causes.

| Feature combination                                      | Units |
| -------------------------------------------------------- | ----: |
| B + G-eager + G-unit                                     |    16 |
| G-deferred + G-unit + Call                               |     4 |
| X                                                        |     2 |
| B + G-unit + inert                                       |     2 |
| K + G-deferred + G-unit                                  |     2 |
| Static block + G-eager + G-unit                          |     1 |
| Computed key + Call + `await` + refused                  |     1 |
| X + Call                                                 |     1 |
| B                                                        |     1 |
| G-deferred + G-unit + `import.meta` + `await` + refused  |     1 |
| Static block + G-deferred + G-unit                       |     1 |
| X + G-deferred + G-unit + `import()` + `await` + refused |     1 |

“Refused” means the linked census could neither lower nor retain that unit. It
occurs in three of the 33 units: an awaited computed key, an `import.meta` use,
and an async body requiring an `import()` context. The archive retains every
unit's overlapping feature set, syntax node kinds, globals, source ordinal and
the lowerer's diagnostic. It also retains all 78 allocation-only class units;
there were no `extends null` occurrences in any of the 111 class-bearing units,
including deferred classes. The scope is the same 169 parseable single-input
debundle cases, six libraries and 79 Excalidraw chunks used in N4. Three invalid
debundle sources and 12 multi-input cases were not analyzed.

The dominant actionable pattern is an ambient superclass: 17 non-allocation-only
units extend an unresolved global, 16 of them `Error`. This depends on the host
layers and realm-intrinsic work: a stable reference alone does not establish the
constructor and `prototype` facts needed to avoid a getter. The two known-class
bindings would instead need the state-dependent move interface and preservation
of `ParentAt`; they do not dominate this corpus. No new heritage proof is
proposed here.

[Corrected before](https://app.buildbuddy.io/invocation/6bc867b6-c877-40af-aedd-295ecaea193e) ·
[Corrected after and blocker details](https://app.buildbuddy.io/invocation/99cfb18e-b367-4e47-ab4d-8043dd300f0a)
