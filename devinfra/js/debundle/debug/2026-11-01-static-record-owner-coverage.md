# Static record initialization effect coverage

## Missing producer coverage

A standalone initializer such as this had no original-owner effect certificate,
even after its local inputs were initialized:

```js
const supplied = 1;
const record = { supplied, nested: { value: supplied } };
```

The original-owner producer now accepts static ordinary records whose values
are primitive literals, initialized local declarative reads, or nested records
in that subset. It retains a set of source read identities and the root
allocation site, rather than another copy of the initializer tree.

The canonical original-owner collector checks the complete source envelope.
It accepts identifier and string data keys and shorthand fields. It rejects
computed keys, spreads, accessors, methods, colon-form `__proto__` fields,
arrays, function-valued fields and other expressions. In particular,
`const record = { value: source.current }` still needs a getter-aware property
transfer and cannot receive this certificate.

Before executing the root initializer, the interpreter checks every retained
read identity against the initialized module-local declarative cells. Imports
and globals cannot provide this proof. Duplicate fields retain all their
source reads, including overwritten values; a later initialization cannot
retroactively justify an earlier read. The ordinary allocation transfer must
return the exact root allocation with no unavailable alternative. Existing
owner finalization still requires completed binding initialization, complete
execution, no unknown call, no precision loss and no uninitialized read.

Static data fields use ordinary own-property creation. The copied values can
be opaque, shared or callable without invoking their properties or bodies.
This establishes neither heap locality nor immutable values. Binding reads,
the destination initialization and fresh allocation effects remain recorded.
The producer only closes missing global-property, general-heap and host-effect
coverage for this checked source owner; legacy graph decisions and validator
authority are unchanged.

## Review and remaining work

Reviewed the canonical source collector, root transfer's pre-evaluation check,
ordinary record allocation transfer and terminal owner certificate consumer.
The existing initializer-coverage fixture now expects its static record case
to be covered. No tests, corpus runs, profiling or benchmarks were added or run
locally; hosted PR CI supplies compilation and the existing regression checks.

F10's missing owner-refusal explanation is being addressed separately. Property
reads and calls still need ordered value and effect transfers that retain
getter/callback behavior and the established native/array proofs. Source
purity or an empty effect ledger cannot replace that work.
