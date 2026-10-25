# Empty collection construction and callback timing

The neutral CLI cases publish `() => later`, create an empty collection, then
initialize `later` in a separate logical module. They initially reject empty
Map, Set and WeakSet construction with an atomic conflict between collection
and later. Empty WeakMap construction already passes; the shadowed constructor
and supplied-iterator controls correctly reject a split that changes reentry.

Synchronous execution had an explicit empty-WeakMap exception. Other empty
collections took the opaque-call path and promoted already published callback
reads, despite the intrinsic constructor not invoking user code without an
iterable argument.

Use the same exact undeclared/unshadowed global-identifier and no-arguments
gates for all four keyed collection constructors. The existing intrinsic
integrity premise applies. Keep nonempty arguments, dynamic or shadowed callees,
and later collection operations on the conservative path. The soundness notes
link the constructor algorithms: each returns before adder/iterator lookup
when no iterable is supplied.
