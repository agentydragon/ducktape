# Remaining shared-execution work

The supported local/imported operations now share prepared provider bodies,
qualified identities and the operation kernel. The current gate remains
authoritative; effect coverage is report-only. Unsupported execution may stay
conservative. The lasting execution rules belong in
[the design](../docs/design.md), not this completion checklist.

## Acceptance still required

- Complete the unchanged canonical Excalidraw and pinned upstream icons candidate
  under the normal gate, then run their actual Node/browser output checks.
- Complete the private Claude CLI and Tana analyses and their apply/runtime checks.
- Obtain a completed same-corpus comparison against the original engine,
  separating source-analysis CPU/memory from ESM scheduling. The
  [active performance investigation](../debug/2026_11_15_maintained_spec_resource_limits.md)
  records the measured bottleneck and reproducible small corpus.

A passing reduced fixture, a diagnostic emitted tree, or a capped profile does
not establish canonical extraction acceptance.

## Precision boundaries

| Remaining boundary                                                                                                                   | Behavioral controls                                                                                                        |
| ------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------- |
| Indirect eval; derived construction or prototype access with a retained writer                                                       | `effect_tv_conditional_logical_test.rs`: exact schedule refusal, diagnostic Node parity and accepted colocated controls    |
| Unproved inversions through some native iteration results and nonempty Map construction                                              | Intrinsic iteration and Map suites: retain independent graph evidence, refuse the missing completion proof                 |
| Audited factory/async callback phase crossings                                                                                       | Constructor and async suites: keep creation/registration and captured initialization in source order                       |
| Opaque host/package loader cuts                                                                                                      | Cross-chunk operation suite: no manufactured provider readiness; a graph or purity receipt does not discharge loader order |
| General loops, try/await/yield, destructuring parameters, dynamic keys, object spread and arbitrary receivers outside the ordered IR | Existing conservative source effects remain; they do not become ordered completion receipts                                |

Broader precision is optional. Improve a boundary only with a general transfer,
a checked recognition or an explicit author premise, accompanied by a causal
bundle + spec → accepted behavior or refusal test. Reuse must preserve live
cells, heap/capture invalidation, native activation, and ordered availability;
source identity alone is not an execution witness.
