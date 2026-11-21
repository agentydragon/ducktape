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
after conversion to an `EsmSplit.Bundle`. It may reject a summary. Its theorem will
say that every accepted summary covers the supported statement's behavior; it
will not say that every invocation of Effects produces an acceptable summary.
Overestimated reads, writes, or effects are conservative.

JavaScript-to-CoreJS translation and emitted JavaScript still have the existing
frontend correspondence boundary. `FootprintsOfSyntax` checks declarations and
free-name coverage; it does not prove the semantic correctness of either frontend.

## Semantic interface

[`MovesSound`](../lean/Experiments/CoreJS/Move.lean) separates the semantic premise
from the procedure that establishes it. For each linked bundle scope and execution
bound, every allowed pair must commute in both directions. The existing `MayMove`
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
declaration initialization, and expression sequencing. It excludes property
operations, calls, callbacks, and block-local allocation. Values copied between
bindings may be references; the fragment does not inspect their heap contents.

Its semantic footprint must account for complete binding records, including TDZ
and mutability checks on assignment targets. It must bound reads and writes,
preserve the heap and world on successful execution, compose through sequencing,
and justify commutation when writes are disjoint from the other operation's reads
and writes. Linking must connect distinct names to distinct concrete binding cells.

The executable certificate must check required reads against `B.reads i`, required
writes against `(B.stmt i).defs ++ (B.stmt i).writes`, and reject incorrect hoisted
labels for active statements. Existing scope and `AssignsHome` checks remain
necessary: moving an assignment must not turn its target into an immutable import.

A useful integration regression is a split that swaps independent assignments
such as `x = 1` and `y = 2`, which the existing syntax-only `MayMove` rejects.
Removing a required write or read from a supplied summary must reject its
certificate. Unsupported code must gain no new permission.

## Current proof inventory

| Rule family                                       | Existing proof                                 | Remaining footprint obligation                                                   |
| ------------------------------------------------- | ---------------------------------------------- | -------------------------------------------------------------------------------- |
| Inert statements and allocation-only declarations | CoreJS locality and syntax-based commutation   | Certifying the corresponding Effects output                                      |
| Binding reads, initialization, assignment         | Primitive semantics and opaque request frames  | Compositional read/write footprint and certificate integration                   |
| Opaque binding and own-key requests               | Reachability envelope and request framing      | Analyzer-state correspondence, other requests, and whole-conversation invariants |
| Objects, closures, control flow, classes          | CoreJS semantics and selected locality results | Soundness of the corresponding Effects summaries                                 |
| Intrinsics and unknown-code fallback              | Handwritten and generated regression coverage  | Per-rule semantic contracts and conservative fallback proofs                     |

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
