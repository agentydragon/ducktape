# Primitive member reads in escape analysis

The neutral CLI fixture `passing_primitive_property_does_not_escape_the_receiver`
initially failed with an atomic conflict between the collection and later value.
The input stores a callback, declares an ordinary constant lookup table, and uses
`new Set([lookup.key])`. An unrelated function passes `lookup.key` to a consumer.

`PlainDataWriteScanner::disqualify_value_escape` collected every identifier in
that argument. It therefore treated the lookup receiver as an escaped object,
even though the argument was a literal string. Losing PlainData also lost the
standard Set primitive-iterable proof and fabricated synchronous callback reentry.

The repair caches own primitive literal property keys from the already collected
constant initializers. The escape visitor excludes only those exact static member
values, using binding IDs. The same literal-property helper serves Set inference.
All writes, actual object escapes, alias mutations and unknown expressions retain
the conservative scan. The formatted-message fixture covers a nested read; the
method-returning-receiver control keeps identity exposure conservative. Existing
callback-valued, duplicate-property, getter, proxy and shadowed-Set controls remain.

This does not replace value-flow escape analysis with an empty-result test.
Primitives, `this` and some unresolved expressions can all have empty tracked
origin sets, so that test cannot justify an escape exemption.
