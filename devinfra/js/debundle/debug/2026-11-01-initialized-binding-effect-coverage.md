# Initialized local binding reads in owner effect coverage

## Proof boundary

The original-owner initializer certificate previously covered primitive and
ordinary function creation only. A standalone alias declaration consequently
kept unknown global-property, general-heap and host coverage even when its
entire initializer was a known initialized module-local cell read.

The canonical source collector now retains a direct identifier leaf, with
parentheses allowed. The dedicated root initializer transfer checks that
exact hygienic binding against the current prefix's top-level declarations
and initialized cells, excluding imports. This check occurs before evaluating
the read. The lowered expression must still name the same binding; evaluation
must return a nonempty value set with no unavailable alternative. Owner
finalization still requires successful binding initialization, complete
execution, no unknown call, no precision loss and no uninitialized read.

Reading a local declarative cell can copy any value without invoking its
getters, coercions or callable body. Several abstract alternatives, opaque
values and shared objects therefore need no primitive, freshness or locality
proof for this read. The effect report retains the source binding read and
destination initialization write. Member access or later use of the copied
value keeps its ordinary proof requirements.

## Preserved limits

The original whole-statement envelope remains required: exports, split comma
declarations, destructuring, sequences, assignments and other initializer
syntax stay outside this certificate. Imported reads need provider
initialization evidence. A host global may be an accessor. A local binding
must already be initialized at the read; a later owner-finalization update
cannot retroactively prove that availability. Generic child evaluations and
invocation memos still cannot mint or propagate an owner certificate.

This expands the report-only coverage producer. The effect-based validator
and legacy gates retain their existing authority; completing those consumers
requires broader operation coverage and actual emitted-order validation.

## Encountered tooling friction

**F10 — Explain original-owner coverage refusals:** a direct alias read could
appear pure and synchronously complete while its effect report still carried three unknown
domains. Those facts alone do not certify the whole original evaluation
envelope. The SPEC now names the accepted local-cell read and its remaining
import/global/initialization limits. A future report should expose the
original-owner coverage refusal category and site so these missing proofs
can be identified without tracing the private certificate producer.
