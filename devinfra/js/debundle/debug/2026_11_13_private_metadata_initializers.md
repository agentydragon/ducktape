# Private metadata initializer contract

An imported factory can allocate a fresh result, register its description in a
provider-owned WeakMap and observe stable provider configuration. This need not
satisfy initializer purity or external isolation. A lexical `source_only`
promise alone does not describe these provider effects.

`initializer_external_effects: private_metadata` adds the audited contract in
[SPEC](../SPEC.md#initializer-external-effects). It requires fresh result-owned
metadata identities, no independent enumeration/interception/publication,
stable provider/configuration/environment observations and observationally
idempotent private memoization across all admitted schedules. Application
reentry and observable host/global writes are excluded. The author must audit
these premises for the configured code and provider lifetime.

The frontend resolves both external-effect variants into one typed
binding-to-contract map. Admission preserves the existing global-property and
known-external-observation guards. External isolation additionally rejects
source-observed content cells without a fresh ownership proof; private metadata
retains those cells for ordinary ordering checks. The post-execution overlay
records author provenance without changing raw facts, completion or operation
receipts. The heap-order consumer refines only unknown frontiers and assumed
accesses; explicit cells, allocation events and other effect domains stay active.

## Behavioral proof

The three neutral cases in `e2e/initializer_source_only_test.rs` give the real
CLI a bundle and spec requesting one module for two constructions separated by
independent allocations. They cover fresh WeakMap entries, stable provider
configuration and a cached primitive prefix with fresh results on every call.
They assert original/emitted Node output and module exports before acceptance.

With the typed contract admitted but its graph consumer disabled,
[the same cases reject][red] only at their final acceptance assertion: runtime
parity and exports already pass. The raw-analysis replay case also rejects at
materialization. All 14 existing tests and the source-visible shared-cell
negative control pass in that run.

With the consumer enabled, [all 19 initializer tests pass][green], including
raw projection/artifact identity and direct-versus-loaded emission through both
logical-module and chunk-rename annotation maps. A comma-declarator case pins
scope to the selected initializer. Shared-array mutation/read ordering still
rejects, and both contract variants reject source-known global accesses.
Existing opaque source-only counterexamples for shared metadata keys, provider
publication and changing configuration remain rejected. The foreign-state
Node differential sweep, commuting-statement gate and local-array ordering
suites also pass in that invocation.

This is an author contract, not inference of safe WeakMap use. An opaque false
promise is not detected by these guards. General inference remains tracked in
[TODO](../TODO.md); changes need source proofs and a receiving extraction gain.

## Nested allocation proof

The first receiving batch still rejected after the metadata overlay: nested
array arguments retained real cells but lacked the direct initializer's birth
proof, creating dependencies on earlier opaque constructions. The neutral
`private_metadata_recognizes_fresh_nested_array_arguments` fixture reduces this
to a discarded construction followed by two extracted constructions. Its
original and emitted programs both print `a b`, and exports are correct;
[before the birth fix][array-red] only required gate acceptance fails.

The existing heap-content AST collector now records all eager array literals
inside a simple top-level initializer. Each source occurrence executes at most
once. Callable bodies, object setters (including parameter defaults) and class
syntax are excluded, and the graph still
excludes duplicate and deferred sites. Facts and saved analysis carry the same
set. This does not infer freshness from a pooled site identifier or erase any
known cell. Direct certified native Set construction retains its existing proof.

[After the fix][array-green], all 20 initializer tests and the local-array,
native Set, commuting-statement and foreign-state differential suites pass.
The repeated-callable allocation control and known shared-cell ordering remain
active.

## Receiving result

Two private receiving specs independently accept the same reviewed batch of 39
schema initializers in two coherent module owners. Each grows from 7,984 to
8,023 binding claims and from 1,668 to 1,670 owners. Every previous raw/readable
claim and per-owner source-unit set, including anonymous selections, remains
unchanged. Gate and translation acceptance are true with zero conflicts and
placement splits.

Both normal browser-load tests and their schema runtime tests pass on RBE.
Each schema test checks 39 valid and 39 invalid cases, all 33 public exports
and six nested private helpers, plus nine defaults/coercion/bounds/description
checks. All four browser runs report zero page errors and no failed transformed
assets. Three runs report zero failed requests; the baseline schema run records
three aborted third-party analytics requests, accepted by its existing network
policy. This demonstrates a receiving extraction gain under audited promises,
not full canonical acceptance or general inference. Detailed source audits and
receiving invocation records remain in the private repository.

[red]: https://app.buildbuddy.io/invocation/8a14a284-9c65-4744-8e13-9959bf8442b4
[green]: https://app.buildbuddy.io/invocation/5a73d083-2f04-4a51-bf84-1fe2af8e9670
[array-red]: https://app.buildbuddy.io/invocation/65138e68-2ce2-47b7-aca7-ed6cea77f5f3
[array-green]: https://app.buildbuddy.io/invocation/2fd96075-7258-4003-9cf2-b0ad08dcf070
