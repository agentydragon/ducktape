# Proving footprint certificates against CoreJS

The goal is a compositional soundness framework for existing Effects footprints,
with proved certificates usable by the debundling equivalence checker. Extend the
proved fragment where the effort is justified; fix incorrect rules when found.
Public workloads can guide priorities without access to private bundles.

## The guarantee

For an admitted CoreJS program, a checked footprint certificate must justify the
accesses and updates attributed to its statements. Disjoint certified footprints
then justify commutation, and commutation justifies a valid reordered split.

The first implementation checks the actual summaries supplied by `Effects.footprints`
after conversion to an `EsmSplit.Bundle`. It may reject a summary. Its theorem
says that every accepted summary covers the supported statement's behavior; it
does not say that every invocation of Effects produces an acceptable summary.
Overestimated reads, writes, or effects are conservative.

JavaScript-to-CoreJS translation and emitted JavaScript still have the existing
frontend correspondence boundary. `FootprintsOfSyntax` checks declarations and
free-name coverage; it does not prove the semantic correctness of either frontend.

## Semantic interface

[`MovesSound`](../lean/Experiments/CoreJS/Move.lean) separates the semantic premise
from the procedure that establishes it. For each bundle scope produced by a
successful `declareTops` allocation and each execution bound, every allowed pair
must commute in both directions. Using an actual allocation trace preserves the
freshness evidence needed to connect distinct names to distinct cells. The existing `MayMove`
relation satisfies this contract through `mayMove_sound`.

`moves_preserve_run_of_sound` and `moves_preserve_of_sound` combine that contract
with `Valid`, `FootprintsOfSyntax`, and the plan's pairwise move checks. Their
conclusion equates successful executions and final states in the same world.
It does not equate throwing, unknown, or nonterminating executions. The existing
syntax-based theorems remain specializations, and this interface alone grants no
new checker acceptance.

This contract currently quantifies over arbitrary concrete states. A later
certificate that depends on a heap invariant must establish that invariant at
linking and throughout the relevant reordered prefixes, or extend the theorem to
carry it. Soundness along only the original source-order execution is insufficient.

## Initial certificate fragment

The first new fragment is literals, variable reads, direct variable assignments,
declaration initialization, expression sequencing, binary operations, heap-free unary
operations, short-circuiting, and conditional expressions. It excludes property
operations, `typeof`, calls, callbacks, compound assignment/update forms, and block-local allocation. Values copied between
bindings may be references; the fragment does not inspect their heap contents.

Its semantic footprint must account for complete binding records, including TDZ
and mutability checks on assignment targets. It must bound reads and writes,
preserve the heap and world on successful execution, compose through sequencing,
and justify commutation when writes are disjoint from the other operation's reads
and writes. Linking must connect distinct names to distinct concrete binding cells.

The abstract/concrete connection for this fragment is `bindingAddresses env names`:
an abstract binding name denotes the cell found by `env.vars.lookup`. The
`BindingSummary.readCells` and `writeCells` predicates lift name sets through that
lookup. `declareTops_bundleEnv_injective` proves that distinct names in the linked
bundle scope cannot alias a cell; it uses the actual allocation trace, including
when the initial store is nonempty. Imported aliases are not assumed injective.

`CellFootprint rd wr computation` requires each successful run to produce a finite
assignment patch supported on `wr`. Replaying the computation from any state that
agrees on the complete binding records in `rd ∪ wr` must return the same value and
apply the same patch. Including `wr` in the agreement premise accounts for TDZ and
mutability checks on assignment targets. The patch preserves heap, world, and
output, and describes the exact resulting store rather than only lookup equality.
This is a relation between certified name sets and concrete cells, not a simulation
of the producer's entire abstract heap or memoization state.

Binary operations and unary operations other than `typeof` use state-free value
operations. Coercions that CoreJS does not model still return `unknown`; certifying
a footprint does not extend the modeled JavaScript subset. `typeof` can inspect an
object's heap record to determine callability and therefore needs a stronger read
contract. Short-circuit and conditional summaries conservatively include every
branch, including branches not taken in a particular execution.

The executable certificate must check required reads against `B.reads i`, required
writes against `(B.stmt i).defs ++ (B.stmt i).writes`, and reject incorrect hoisted
labels for active statements. Existing scope and `AssignsHome` checks remain
necessary: moving an assignment must not turn its target into an immutable import.

The executable path is `bindingCertificate` → `bindingMayMove` →
`certifiedMovesOk`. The checker caches per-statement certificates and read sets.
`bindingMayMove_sound` connects accepted summaries to semantic commutation using
`Stmt.bindingFootprint` and `declareTops_bundleEnv_injective`;
`moves_preserve_of_certifiedMovesOk` supplies the whole-program result.
`LowerSplit.sliceLines` reports this as `moves (certified bindings)` when the
existing syntax-only rule cannot cover a plan.

The integration regressions swap independent assignments such as `x = 1` and
`y = 2`, keeping each assignment with its declaration as `AssignsHome` requires.
They also copy object references without inspecting their heaps during the moved
statements. Generated variants exercise literals, reads and sequencing, and must
reject a corresponding dependency-bearing plan. A mutation control reloads actual
Effects output, removes a required write, and requires the semantic certificate
to reject it. Unsupported code gains no permission from the new certificate.

## Class heritage

For `var Child = class extends Base { ... }`, class creation reads both the
binding of `Base` and its constructor's `prototype` property. A getter could
observe a crossed declaration. The `moves (certified class heritage)` check
looks for an earlier anonymous base class with literal instance methods or
accessors. Its creation establishes the bound constructor and its own data
property. The checker tracks those facts through intervening statements,
including another plain subclass of the same base.

The checker tries one subclass per plan. It first verifies an intermediate
order with the existing certified moves. It then checks that the subclass can
cross a contiguous block of inert or allocation-only declarations whose eager
reads exclude the subclass binding. Both orders must share the prefix that
establishes the base. The resulting theorem quantifies over all CoreJS worlds
and fuel bounds and compares the entire final state on successful execution.
[Checker and theorem](../lean/Experiments/CoreJS/HeritageCertificate.lean)

## Current proof inventory

| Rule family                                                                                         | Existing proof                                               | Remaining footprint obligation                                                   |
| --------------------------------------------------------------------------------------------------- | ------------------------------------------------------------ | -------------------------------------------------------------------------------- |
| Inert statements, allocation-only declarations, and fixed-key field-free plain classes              | CoreJS locality and syntax-based commutation                 | Certifying the corresponding Effects output                                      |
| Binding reads, initialization, assignment, sequencing, value operations and conditional expressions | `BindingFootprint`, `BindingMoves`, checked actual summaries | Producer completeness; abrupt-execution equivalence                              |
| Opaque binding and own-key requests                                                                 | Reachability envelope and request framing                    | Analyzer-state correspondence, other requests, and whole-conversation invariants |
| Objects, closures, control flow, classes                                                            | CoreJS semantics and selected locality results               | Soundness of the corresponding Effects summaries                                 |
| Intrinsics and unknown-code fallback                                                                | Handwritten and generated regression coverage                | Per-rule semantic contracts and conservative fallback proofs                     |

The [`model` coverage matrix](../experiments/model/README.md) and
[`effects` tests](../experiments/effects/README.md) describe executable evidence.
Passing those tests is not a footprint soundness proof.

## Implementation boundaries and milestones

Most of `Effects.eval` and `Effects.exec` is a mutually recursive `partial def`
block. Those definitions do not expose ordinary kernel unfolding equations.
Several total-looking wrappers also call partial helpers. Direct producer
correctness additionally needs contracts for opaque cached code metadata and
allocation/memo keys; hash values cannot silently be assumed collision-free.

The initial total certificate checker avoids trusting those producer internals.
A future direct proof can extract genuinely transparent transfer functions or
refactor a bounded evaluator, with explicit correspondence proofs and performance
validation. A whole-interpreter rewrite requires reconsidering the scope first.

Land coherent increments:

1. General semantic commutation interface, with the existing syntax rule as an
   audited specialization.
2. Compositional cell footprints, primitive rules, and the total certificate
   definitions with negative examples.
3. Checker integration that certifies actual Effects summaries and accepts a
   previously rejected independent-assignment split, with differential tests.
4. Additional existing rule families where their proof and integration costs are
   justified by the supported analysis behavior and public workload measurements.
