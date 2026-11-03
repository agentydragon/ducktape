# RegExp literal owner coverage reproduction

The neutral CLI fixture moves `const pattern = /^x$/;` into a module before an
independent marker declaration. A local `RegExp` binding throws if invoked;
the emitted program still prints `true 1`, and the report has two swapped
pairs with zero Havoc pairs. The `pattern.test` initializer and output call
remain outside whole-owner coverage, with two unknown-coverage statements per
effect domain. The later prototype-getter fixture also preserves its output
and reports one harmless swap while retaining the unsupported member-read
refusal and unknown effects.

A separate forward-read fixture requests a split between `const observed =
pattern;` and `var pattern = /^x$/;`. The graph rejects that plan as an atomic
group conflict. Its effect row still records the RegExp owner's observed
`Binding(pattern)` write and the reader's observed read. The RegExp creation
endpoint has no unknown coverage; the independent reader remains unproved and
accounts for the pair's Havoc. This is a rejection control, not an accepted
reorder. The constructor control keeps `new RegExp(...)` unsupported and
preserves the throwing constructor behavior.

The proof extends the existing original-initializer certificate only for a
root regex literal. ECMA-262 §13.2.7 evaluates a validated literal through
`RegExpCreate`, which does not look up the mutable global `RegExp` binding or
run source-defined code. The dedicated transfer must still produce the exact
Object allocation, and the owner must finish initializing its binding.
Creation coverage proves no RegExp internal slots, prototype integrity,
method behavior, or later property/getter/call effects. It relies on the input
parser rejecting invalid literal syntax.

The report's `full_semantic_precision: false` is a fixed policy field; use
per-chunk coverage/refusal counts and conflict rows to interpret these
fixtures. Focused verification passed with
`bbr test //devinfra/js/debundle/e2e:effect_translation_validation_test
--test_output=errors` on RBE (BuildBuddy invocation
`0a5e59ae-6b3c-4772-bcc4-03d32a24b12a`).

Sources: [ECMA-262 §13.2.7](https://tc39.es/ecma262/2026/multipage/ecmascript-language-expressions.html#sec-regexp-literals),
[§22.2.3.1](https://tc39.es/ecma262/2026/multipage/text-processing.html#sec-regexpcreate).
