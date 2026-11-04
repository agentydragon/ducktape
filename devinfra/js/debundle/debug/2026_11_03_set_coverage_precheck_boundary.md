# Primitive Set coverage precheck: preservation boundary

A missing `SetIterableProof::dependencies_for_constructor(exact_span)` is a
necessary-condition failure for primitive-array Set coverage. The new native
admission helper requires that entry. It is not a certificate that the
initializer's value evaluation contributes no state or effects.

The bounded proposal was to refuse coverage before `EvaluateInitializer` when
that entry is absent, retain ordinary value evaluation for needed bindings,
and skip coverage-only evaluation for unused bindings. No runtime change was
made because preservation of the latter skip was not established.

## Source boundary

Inspected `devel` revision: `4d07f7bff3`. The Set owner change is already merged;
earlier draft status is obsolete.

`OriginalInitializer::collect` recognizes a primitive-array constructor by the
identifier spelling `Set`. It does not establish that this identifier denotes
the native constructor. A locally shadowed class can therefore enter the same
initializer route while lacking the native iterable proof.

`evaluate_initializer_transfer` calls `evaluate_transfer`. Its `ValueExpr::New`
branch can fall through native resolution to `construct_local_class`. That
method transfers into `construct_local_class_transfer`, which can taint
exported objects, record inference dependencies, and invoke a constructor with
the current state. An unused result does not make this evaluation state-free.

The ordinary `execute_call_transfer` lane also constructs local classes before
assignment value evaluation. This establishes that a skip would preserve the
ordinary effect lane. It does not alone establish that the omitted later
evaluation's state, dependencies and allocation judgments are redundant with
that earlier execution: the latter starts from a later prefix and can follow
different availability or precision paths. The proof needed here is a relation
between those two lanes, not just a missing native iterable entry.

## Required invariant before implementation

For an exact collected primitive-array Set owner with no iterable dependency,
show that deleting only the unused-binding dedicated evaluation preserves all
required effects and never makes later heap or lexical facts less conservative.
Needed bindings must still evaluate normally, and the owner must retain
`InitializerResultUnproved`; missing prerequisites cannot become a completed
owner certificate. Existing effect-lane refusal and fallback diagnostics must
remain available.

The current shared lookup supplies the necessary-condition rejection but not
the preservation invariant. Establishing a stronger native-callee prerequisite
or validating lane equivalence is separate work. This investigation stopped
before adding either. No CPU or memory saving is established.

No tests, builds, Analyze runs, benchmarks, profiles or BuildBuddy queries were
performed. The earlier bounded source and receipt audit is commit
`277410dbf115750d37a1ec2be6d95c83c19a0a68`.
