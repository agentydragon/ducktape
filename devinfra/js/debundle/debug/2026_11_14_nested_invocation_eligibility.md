# Nested invocation eligibility: no corpus reuse

## Decision

The bounded exact-state memo experiment found no reusable entries on the frozen
Excalidraw workload. Stop this route; neither its frame refactor nor a replay
cache is proposed for production. The positive fixture proves the probe can
recognize repeated nested work, but the corpus does not meet its guards.
This result does not rule out other memo designs.

## Experiment and checks

The pushed experiment branch is
`experiment/nested-memo-eligibility-20261114`. Its implementation is
`84b99b5bd4b0396e5d7880f7ff6c18d22ff118fa`, above an exact reachability
preflight and base `fc6336654ba14c2e87b1c814d7d911ae3d5cb020`.
It moves active lexical-cell bindings into application/provider path state,
preserves joins and GC roots, and assigns nonrewound mutable-access tokens
outside semantic World equality.

The probe surrounds the whole Invoke transfer, including wrapper work and
prerequisite settlement. It requires an unchanged World token, exact unchanged
non-World semantic context and observations, the unchanged legacy memo table,
exact recursion/depth context, and sufficient budget for the same invocation
cost. Only complete, normally completing, primitive-returning ordinary calls
qualify. New receipts and pending ancestor snapshots are dropped before either
weak interner observes liveness. Every transfer still executes; no replay occurs.

The real CLI/Node anchor exercises both colocated and split modules and prints
`7`. Each layout records eight genuine nested would-hits. A would-hit additionally
requires the second executed transfer to reproduce the full Outcome, evaluation
ledger, proof dependencies, and budget cost.

[Default exact-head validation](https://app.buildbuddy.io/invocation/739c0eee-d5d1-487e-bad9-4c9a0af0f497)
and
[diagnostics-enabled opt validation](https://app.buildbuddy.io/invocation/3d103c8c-9cd4-4db4-b09b-ac3513ed1202)
each executed and passed nine targets, including 373 core tests. Coverage includes
application/provider frame divergence, empty-join allocator continuity, unequal
revisions, rewinds, transitive cell roots, weak capture lifetime, native guards,
callback effects, recursion/budgets, and concrete branch/factory behavior.

## One enabled corpus analysis

The workload is the unchanged 79-chunk, 196-file source/spec snapshot from
`df69d1c9d2ddc4d511bb44f9c796da6408fdbee3`. The input manifest SHA256 is
`8154a3c4ce085b8cd0279f9473a56fa6ca84c622749bdf810781daa3220abfc3`.
The three direct opt binaries came from the diagnostics-enabled validation,
with advertised artifact sizes and SHA256 digests verified before execution.

| Counter                                   |  Total |
| ----------------------------------------- | -----: |
| Attempted tainted invocations             | 23,509 |
| Individually eligible completed transfers |  1,805 |
| Repeated complete entry/receipt matches   |      0 |
| Nested would-hits                         |      0 |

The 1,385 diagnostic rows record 18,980 mutable-World-access refusals, 1,411
completion refusals, 1,199 interner invalidations, 87 changes to invoked-callable
tracking, 26 budget refusals, and one fallback-outcome change. The 1,805 eligible
transfers were entry misses. Mutable access is a conservative certificate
failure, not proof that semantic contents actually changed. Eligible and
refusal/miss counts are not disjoint categories.

One disabled control pipeline and one pipeline with exactly one enabled source
analysis both matched the preflight parent's projection, all 118 JavaScript
file hashes, and every report JSON after replacing only output-root paths.
The input manifest remained unchanged. Application ran with diagnostics off.
No timing or speedup claim follows from this eligibility experiment.

Raw commands, stderr records, the aggregate `eligibility-summary.json`, and
parity results are under
`/tmp/agents/2026_11_14/excalidraw-speed/nested-memo-eligibility/`.
The source audit and proof contract are in the adjacent task scratch
`frame-world-memo-route/contract.md`; the committed experiment preserves the
implementation and executable positive control.
