# Saved native census protobuf

`saved_native.proto` describes the whole-program native-integrity census saved
between analyze and apply. The real saved-program-summary path writes and reads
one native protobuf census inside the existing compressed artifact container.
The container and the other saved facts remain MessagePack during migration.

Generated messages replace `SavedNativeIntegrity`, `SavedNativeMutationWitness`,
`SavedNativeUnreachableBranchSpans`, and `SavedNativePrototypeMutation`. The
runtime uses generated `NativeMutationOperation`, `NativePrototypeMutationKind`,
and `NeverWrittenReach` enums directly. `NativeIntrinsic` and callable argument
evidence use the shared saved schemas rather than another native vocabulary.

The explicit boundary preserves source spans omitted from report JSON, author
evidence and consumed dependencies, receiver/key sum types, and override witness
indices. A property-name message distinguishes absent (any property) from a
present empty set (only Symbol keys). Optional strings preserve present-empty
values. Evidence includes its author-input key once; writing checks agreement
with the runtime map key, and decoding rejects duplicate keys.

Source locations, spans, host keys, spec selectors, and callback evidence reuse
their existing named boundary functions. There is no adapter framework or
generated code committed to the repository. The existing engine identity
invalidates old artifacts; this format makes no cross-version promise.

Correctness checks cover the real direct-versus-saved CLI path, nested spans,
property presence, native slot variants, and incomplete evidence. No additional
performance measurements are part of this migration.
