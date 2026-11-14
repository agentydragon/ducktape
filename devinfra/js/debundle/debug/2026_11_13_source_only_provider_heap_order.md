# Provider heap ordering after lexical initializer assertions

`initializer_binding_effects: source_only` describes importer lexical bindings.
It does not certify a provider's metadata registry, captured configuration or
published values. A graph with no modeled local heap cells must retain unknown
heap frontiers after this assertion removes conservative binding coupling.

Three neutral bundle/spec regressions extract two calls into one module while
leaving an intervening observer or configuration change in the entry:

| Provider behavior                                | Original Node output | Previously accepted output |
| ------------------------------------------------ | -------------------- | -------------------------- |
| Reuse one WeakMap key for both results           | `a b`                | `b b`                      |
| Publish fresh keys through a provider variable   | `a b`                | `b b`                      |
| Read configuration changed between constructions | `a0 b1`              | `a0 b0`                    |

These lexical assertions are valid: construction does not touch hidden lexical
bindings in the importing chunk. There is no purity or external-isolation
assertion. [Before-fix invocation](https://app.buildbuddy.io/invocation/9f5e1fd9-22ad-49b3-813e-93adf10b63bd)
runs both original and emitted programs, verifies those outputs, then fails at
the required rejection assertion. The committed regressions assert normal CLI
cycle rejection; diagnostic output from a refused split is not a contract.

The repair makes the heap-order scan retain possible external storage whenever
a lexical initializer assertion was applied. It scans the complete chunk first,
so opaque operations before the selected initializer are preserved too. Actual
heap cells, allocation births, source facts and operation receipts are unchanged.
The separate external-isolation contract still has its own admission and graph
consumer.

Enabling heap ordering for every opaque invocation was also tried. It rejected
all stateless-provider sweep cases; see
[the broader probe](https://app.buildbuddy.io/invocation/a20250f0-3d64-43cc-a113-9fa0e8465513).
The scoped repair preserves that acceptance coverage and rejects the three
counterexamples. One valid native encoder fixture now states its existing
external-isolation premise explicitly; its emitted exports and Node output
remain pinned.

[The focused matrix](https://app.buildbuddy.io/invocation/aeaed60a-7cab-40e9-b987-afd0026cf78d)
passes `initializer_source_only_test`, `foreign_state_node_differential_sweep_test`,
`commuting_statements_gate_test` and `local_array_push_order_test`. In particular,
the differential sweep still requires accepted stateless splits and checks all
accepted programs against Node.
