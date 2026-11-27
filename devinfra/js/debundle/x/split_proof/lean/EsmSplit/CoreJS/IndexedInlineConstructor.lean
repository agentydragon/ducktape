import EsmSplit.CoreJS.IndexedCheckedSplit
import EsmSplit.CoreJS.IndexedInlinePlan
import EsmSplit.CoreJS.PrimitiveRhs

/-! A proof-carrying indexed constructor for the actual decoded inline source
and authored plan. Its output is the same indexed program used by local export
rows; no emitted program or execution pair is supplied as evidence. -/

namespace CoreJS.IndexedInlineConstructor

open LinkedBehavior

/-- Source-only admission for unique lexical declarations with primitive
initializers and reads from earlier bindings. A lexical let without an
initializer retains the model's `undef` initializer. -/
def staticDeclarations : List Name → List (Nat × Stmt) → Bool
  | _, [] => true
  | earlier, (_, .decl .const name init) :: rest =>
      !earlier.contains name && (PrimitiveRhs.check earlier.contains init).isSome &&
        staticDeclarations (name :: earlier) rest
  | earlier, (_, .decl .let_ name .undef) :: rest =>
      !earlier.contains name && staticDeclarations (name :: earlier) rest
  | earlier, (_, .decl .let_ name init) :: rest =>
      !earlier.contains name && (PrimitiveRhs.check earlier.contains init).isSome &&
        staticDeclarations (name :: earlier) rest
  | _, _ => false

/-- Count authored selectors before the planner deduplicates their units. -/
def SelectorsOnce (body : List (Nat × Stmt)) (B : EsmSplit.Bundle) (S : EsmSplit.Spec) : Prop :=
  ∀ unit ∈ body, (S.modules.flatMap fun module => module.2.filter fun selector =>
    selector.claims B unit.1).length = 1

instance (body : List (Nat × Stmt)) (B : EsmSplit.Bundle) (S : EsmSplit.Spec) :
    Decidable (SelectorsOnce body B S) := by
  unfold SelectorsOnce
  infer_instance

structure Construction (decoded : LinkedInput.IndexedSource) (B : EsmSplit.Bundle) (S : EsmSplit.Spec) where
  checked : IndexedInlinePlan.Checked decoded B S
  staticSource : staticDeclarations [] decoded.source.body = true
  publicEntry : S.privateEntryBody = false
  sequential : S.sequential = true
  selectorsOnce : SelectorsOnce decoded.source.body B S
  schedule : checked.schedule.plan.evalOrder = S.order ++ [S.entry]
  assignment : List UnitPlacement
  assignmentEq : assignment = IndexedCheckedSplit.assignments decoded.source.body checked.schedule.plan
  program : Program
  constructed : program = splitProgramIndexed decoded.source.body B checked.schedule.plan
  placement : Places assignment decoded.program program
  authored : ∀ id owner, (id, owner) ∈ assignment → S.claimants B id = [owner]
  sameExports : SameExports decoded.program program
  allOutcomes : ∀ w fuel, runProgram w fuel decoded.program = runProgram w fuel program

/-- Check only decoded-source and authored-plan facts, then return the actual
indexed construction with its placement, live exports and all-outcome proofs. -/
def construct (decoded : LinkedInput.IndexedSource) (B : EsmSplit.Bundle) (S : EsmSplit.Spec) :
    Except String (Construction decoded B S) := do
  let .isTrue staticSource := (inferInstance : Decidable (staticDeclarations [] decoded.source.body = true)) |
    throw "expected unique lexical declarations with primitive initializers and earlier reads"
  let .isTrue publicEntry := (inferInstance : Decidable (S.privateEntryBody = false)) |
    throw "checked split requires no private entry body"
  let .isTrue sequential := (inferInstance : Decidable (S.sequential = true)) |
    throw "checked split requires a sequential request"
  let .isTrue selectorsOnce := (inferInstance : Decidable (SelectorsOnce decoded.source.body B S)) |
    throw "each executable row must be selected exactly once"
  let checked ← IndexedInlinePlan.check decoded B S
  let P := checked.schedule.plan
  let .isTrue schedule := (inferInstance : Decidable (P.evalOrder = S.order ++ [S.entry])) |
    throw "constructed modules do not follow the requested order"
  let labels := IndexedCheckedSplit.assignments decoded.source.body P
  let .isTrue coverage := (inferInstance : Decidable
      ((labels.map Prod.fst).Perm (decoded.source.body.map Prod.fst))) |
    throw "plan does not cover original executable units once"
  have unique := (IndexedInputChecks.checked_footprints checked.footprints).1.uniqueIds
  have placed := IndexedCheckedSplit.places rfl decoded.singleton checked.source.safePath
    checked.schedule.safePaths unique coverage
  have ownerProof : ∀ id owner, (id, owner) ∈ labels → S.claimants B id = [owner] := by
    intro id owner member
    apply IndexedInlinePlan.placementOwner checked
    rw [SparseProjectedOrder.split_indexed_fileUnitPlacements checked.schedule.safePaths]
    exact member
  return ⟨checked, staticSource, publicEntry, sequential, selectorsOnce, schedule, labels, rfl,
    splitProgramIndexed decoded.source.body B P, rfl, placed, ownerProof,
    IndexedInlinePlan.sameExports checked, IndexedInlinePlan.allOutcomes checked⟩

/-- For an original dense body, the indexed constructor yields exactly the
existing dense CoreJS program when supplied the same source-derived bundle. -/
theorem dense_program {decoded : LinkedInput.IndexedSource} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    (built : Construction decoded B S) {statements : List Stmt}
    (dense : decoded.source.body = (List.range statements.length).zip statements) :
    built.program = splitProgram statements B (EsmSplit.mkPlan B S) := by
  rw [built.constructed, built.checked.schedule.planEq, dense]
  exact ExportRows.indexed_program_dense statements B (EsmSplit.mkPlan B S)

#print axioms dense_program
#print axioms construct

end CoreJS.IndexedInlineConstructor
