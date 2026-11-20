# Property access and local-constructor effects

`PropertyRead.lean` is an independent, deliberately small model of one property on an argument and one on a receiver. Reading an own data property bypasses an inherited accessor; reading a missing property can invoke it. Writing an own data property bypasses an inherited setter; writing to a fresh receiver can invoke it. The accessor appends a marker to a trace.

The `initialize_preserves_trace` theorem composes a read and write under explicit facts about the two states actually passed to the call: there is no modeled accessor on the read path, and the receiver already has an own data property. It proves only preservation of this model's trace. The own-data read theorem separately shows that the read is independent of the modeled inherited slot; reads of missing properties still depend on that slot and must retain that dependency.

The model uses `MJS.World` for its trace. `local_call_then_initialize_preserves_trace` composes the existing MiniJS local-call theorem with the property theorem, under an explicitly supplied closure binding and supplied object states. The two evaluations are separate; this is not a semantics for a JavaScript function body containing a property operation.

This is not ECMAScript or MiniJS object semantics. It does not model arbitrary getter or setter code, exceptions, proxies, multiple prototype levels, descriptor flags, heap aliasing, escaping allocations, callee identity, or JavaScript module evaluation. In particular, a missing own property can read an inherited data value, and writing to an own or inherited non-writable property can throw in strict-mode JavaScript. The model treats data slots as writable and does not model that exception; this is an additional obligation for any correspondence to JavaScript. The classifier examines an already-given state; no source analysis establishes that state or proves that external code cannot change it. The theorem does not discharge Tana obligations or alter the checker.

Run `bbr test //devinfra/js/debundle/x/split_proof/lean:property_read_axioms_test`.
