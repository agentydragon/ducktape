# Native receiver identity

Design follow-up for the [dispatch queue](../TODO.md). This is reconstruction
precision work, separate from call purity. Preserve the native-integrity census
and its conservative fallback while adding evidence upstream of its consumers.

## Established boundary

The census retains unknown global values through descendant reads. Mutating one
can invalidate native families, disabling array and local-constructor models.
The resulting opaque-call fallback can add eager dependencies and atomic groups.
An accepted older engine is not evidence that an unknown receiver is disjoint
from natives: that engine may have dropped the alias uncertainty.

Two real-CLI controls in `intrinsic_array_iteration_test.rs` protect against
incorrect receiver certificates:

- An assignment of `{}` to a global property does not prove read-back identity.
  A setter can discard the RHS while a getter returns `Array.prototype`.
- A class method storing `this` in a global does not necessarily store a fresh
  instance. The method can be borrowed with `Array.prototype` as its receiver.

Both controls observe the original program's native mutation and reject a split
that would change the observed initialization order. Keep these controls when
adding a more precise inference.

## Next implementation boundary

Failed native premises now expose their defining mutation witnesses, including
cross-chunk and computed-key dependencies. A bounded source-only local proof
also records direct receiver certificates with initializer/producer provenance;
its supported boundary is in the [spec](../SPEC.md#inferred-local-receiver-identity).
These do not certify ordinary global-slot read-back or clear independent native
mutations. Failed premises are not claims that one write caused every loss.

For an ordinary source-owned global slot, a new certificate must establish:

1. Descriptor and read-back identity, including host getters, inherited setters,
   proxies and initial values before the first source assignment.
2. A closed set of stores and descriptor/prototype changes across chunks,
   following aliases and computed keys; an outer object's identity does not
   certify values read from its fields.
3. Receiver and call-target provenance for every admitted method writer,
   including borrowed methods and an opaque helper receiving an instance before
   its publication method runs.
4. Initialization and lifetime coverage, including hoisted exports, cyclic
   reentry, host consumers and later replacement.

Use resolver identities throughout. The name-only global-alias helper is
conservative about inner-scope shadowing for effect overapproximation; it cannot
justify a clean receiver for a parameter or block binding named `window`.

Keep inferred identity separate from author assertions in `AnalysisAssumptions`.
The existing deferred-publication and nonreentrant-membership assertions certify
different facts. Any new author contract needs its own explicit descriptor,
alias, receiver and lifetime audit plus recognized-contradiction checks.

## Separate host capability

`native_browser_esm` promises loader namespace timing. A browser worker can
satisfy it while having a custom `window` property, so it cannot silently imply
a Window realm.

An explicit typed Window host premise could independently prove the exact
Window-to-Location receiver path using HTML's unforgeable host properties.
Keep generic globals, shadowed bindings, mutable aliases and arbitrary Location
descendants conservative. Preserve navigation, coercion, throws and reentry
effects. Verify host behavior in a real browser on RBE.

This partial host model does not solve ordinary application-owned global slots;
defer its API until a consumer can use the partial capability independently.
Primary contracts: [Window](https://html.spec.whatwg.org/multipage/nav-history-apis.html#the-window-object),
[Location](https://html.spec.whatwg.org/multipage/nav-history-apis.html#the-location-interface),
and [Web IDL unforgeable properties](https://webidl.spec.whatwg.org/#LegacyUnforgeable).

## Acceptance evidence

Every new certificate needs a neutral CLI case that rejects before the change,
accepts afterward and preserves emitted runtime observations. Negative controls
must retain fallback for accessors, replacement, borrowed receivers, cross-chunk
native writes, unsupported aliases and pre-initialization reentry. Keep the
consumer's working pin until its exact source/spec pair passes the canonical
adapter, regeneration and runtime checks.
