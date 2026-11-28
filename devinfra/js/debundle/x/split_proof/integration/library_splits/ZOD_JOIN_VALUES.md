# Zod's default separator in a checked split

The readable Zod 4.6.7 bundle exposes `joinValues` through `z.util`. Its
statement #17 calls `map` with an arrow that captures `stringifyPrimitive`
(statement #54), then calls `join` with the parameter `separator = "|"`.
The two original declarations are copied verbatim into the test projection.
A new standalone export row exposes the same utility from the projection.
The request places both declarations in `format.mjs`, preserving their
relative order, and keeps the export row in `entry.mjs`.

On devel `51fe4166`, the [unchanged projection request](https://app.buildbuddy.io/invocation/13108f1a-771a-4ac9-80d6-f38374bbfe0e)
reached `indexed-exported-function` and refused: “expected an ordinary
one-parameter return predicate.” The parameter already lowered to saved-slot
initializers. The new placement rule admits the defaulted mapper together with
its closed formatter. The existing proof checks the complete ordered units,
captured cells and public export cell, and applies to the independently
reparsed output. Neither method lookup nor invocation is replaced with an
intrinsic.

```sh
p=//devinfra/js/debundle/x/split_proof
bbr test "$p/integration/library_splits:zod_join_values_test" "$p:corejs_indexed_exported_function_axioms_test"
```

The original bundle SHA-256 is
`5d67ee00c72ddfbaa8d82f88913330687b364e6767faf5065538238d21495538`.
The test pins the two declarations separately, runs the full original
`z.util.joinValues` in Node, and records the projection's digest, receipt
and disk verdict. Omitted and explicit `undefined` separators select
`"|"`; `null` is passed to the receiver unchanged. A supplied receiver
records both method lookups and calls without using an array intrinsic.
An adversarial `map` saves the actual callback, calls it, mutates the receiver,
and throws an exact sentinel before `join`. The mutation survives and the
saved closure can still call its formatter after the original call unwinds.
Both Node and CoreJS exercise this case; Node also checks the public function
name and the emitted entry's identity with the helper export.
Separate fixtures exercise earlier-parameter reads, left-to-right getter
effects and a later parameter's temporal dead zone. Changing the default
separator changes Node's result and fails readback; placing the formatter
in the entry refuses and preserves the existing output.

The ordinary request on all 1,072 original bundle units still refuses.
The empty-profile classifier stops at the ambient `Object`
in unit #0. The checked projection models a supplied receiver; the direct
native `Array.prototype.map` call remains unknown in CoreJS. The Node
array comparison is a runtime observation. No iterator binding is involved
in the default-separator rule.
