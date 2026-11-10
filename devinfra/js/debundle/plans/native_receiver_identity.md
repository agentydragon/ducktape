# Native receiver identity

Design follow-up for the [dispatch queue](../TODO.md). This is reconstruction
precision work, separate from call purity. Preserve the native-integrity census
and its conservative fallback while adding evidence upstream of its consumers.

## Safety boundary

Unknown global values remain live through descendant reads. Mutating an unknown
receiver invalidates native slots conservatively; the resulting opaque-call
fallback can add eager dependencies and atomic groups. A legacy acceptance does
not prove that an unknown receiver is disjoint from native objects.

Keep the two controls in `e2e/intrinsic_array_iteration_test.rs`:

- Assigning `{}` to a global property does not prove read-back identity: a
  setter can discard the value and a getter can return `Array.prototype`.
- Storing `this` from a class method does not prove a fresh instance: the method
  can be borrowed with `Array.prototype` as its receiver.

Both controls preserve the original program's observed initialization order.

## Remaining certificate boundary

Failed native premises already expose their defining mutation witnesses (the
contract is in `SPEC.md` § Native integrity inputs; query behavior is in
`docs/cli.md#native-integrity-guards`; neutral checks are in
`e2e/native_operation_input_test.rs`). The open work is to prove receiver
identity for ordinary source-owned global slots. A sound certificate must
establish:

1. Descriptor and read-back identity, including host getters, inherited
   setters, proxies, and initial values before the first source assignment.
2. A closed set of stores and descriptor/prototype changes across chunks,
   following aliases and computed keys. An outer object's identity does not
   certify values read from its fields.
3. Receiver and call-target provenance for every admitted method writer,
   including borrowed methods and opaque helpers that receive an instance
   before its publication method runs.
4. Initialization and lifetime coverage, including hoisted exports, cyclic
   reentry, host consumers, and later replacement.

Use resolver identities throughout. A name-only global-alias helper can
conservatively overapproximate effects, but cannot certify a parameter or block
binding named `window` as a clean host receiver. Keep inferred identity separate
from author assertions; any new author contract needs descriptor, alias,
receiver, and lifetime audit plus contradiction checks.

## Deferred host capability

`synchronous_module_loader: none` promises loader namespace timing; it does not
imply a Window realm. Add a typed Window-to-Location premise only when a
consumer can use that capability independently. Keep generic globals, shadowed
bindings, mutable aliases, and arbitrary Location descendants conservative; any
host behavior must be verified in a real browser on RBE.

## Required evidence

A new certificate needs a neutral CLI case that rejects before the change,
accepts afterward, and preserves emitted runtime observations. Negative cases
must retain fallback for accessors, replacement, borrowed receivers,
cross-chunk native writes, unsupported aliases, and pre-initialization reentry.
Keep a consumer's existing working pin until its exact source/spec pair passes
the canonical adapter, regeneration, and runtime checks.
