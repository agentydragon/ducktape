import EsmSplit.CoreJS.LinkedBehavior
import EsmSplit.CoreJS.Move
import EsmSplit.CoreJS.Split

/-! A common linked provider prefix and an order-preserving consumer suffix schedule. -/

namespace CoreJS.LinkedBehavior

variable {w : World}

/-- The provider loads first, exactly once, and its actual units are the prefix. -/
def ProviderPrefix (name : String) (linked : List (Module × Env)) (pre : List LinkedUnit) : Prop :=
  ∃ m env, linked.head? = some (m, env) ∧ m.name = name ∧ m.imports = [] ∧
    linked.filter (fun pair => pair.1.name == name) = [(m, env)] ∧ unitsOf [(m, env)] = pre

/-- The two real links share a ready state, related provider prefixes and their live cells.
The importer continuations are supplied separately and their ordered units are checked below. -/
structure SharedProvider (source candidate : Program) (w : World) where
  before : CheckedLink source w
  after : CheckedLink candidate w
  name : String
  cells : EntryExports.Bindings
  sourcePre : List LinkedUnit
  candidatePre : List LinkedUnit
  sourceTail : List LinkedUnit
  candidateTail : List LinkedUnit
  ready : before.ready = after.ready
  providerPrefixSame : SameUnits sourcePre candidatePre
  sourceSchedule : unitsOf before.linked = sourcePre ++ sourceTail
  candidateSchedule : unitsOf after.linked = candidatePre ++ candidateTail
  sourceProvider : ProviderPrefix name before.linked sourcePre
  candidateProvider : ProviderPrefix name after.linked candidatePre
  sourceCells : EntryExports.resolve name before.linked = .ok cells
  candidateCells : EntryExports.resolve name after.linked = .ok cells
  entryCells : exportsMatchFrom before after = true

/-- The single shared provider prefix has exactly the same result in both links at every fuel bound.
This includes its writes, world effects, abrupt result, and fuel exhaustion. -/
theorem SharedProvider.providerPrefixResult {source candidate : Program}
    (boundary : SharedProvider source candidate w) (fuel : Nat) :
    runUnits fuel boundary.sourcePre boundary.before.ready =
      runUnits fuel boundary.candidatePre boundary.after.ready := by
  rw [boundary.ready]
  exact congrFun (runUnits_same fuel boundary.providerPrefixSame) boundary.after.ready

/-- Lift the order-preserving result for the consumer suffix through one common provider prefix.
The checked links put that provider first and once; identical suffix executions are transported
from every state the provider produces, while abrupt provider outcomes stop both schedules alike. -/
theorem SharedProvider.run_eq {source candidate : Program} (boundary : SharedProvider source candidate w)
    (fuel : Nat) (suffix : SameUnits boundary.sourceTail boundary.candidateTail) :
    runProgram w fuel source = runProgram w fuel candidate := by
  rw [run_after_link fuel boundary.before, run_after_link fuel boundary.after,
    boundary.sourceSchedule, boundary.candidateSchedule, ← boundary.ready, runUnits_append, runUnits_append]
  rw [runUnits_same fuel boundary.providerPrefixSame, runUnits_same fuel suffix]

/-- The common prefix is exactly one linked provider module in both acyclic schedules. -/
theorem SharedProvider.providerOccursOnce {source candidate : Program}
    (boundary : SharedProvider source candidate w) :
    (boundary.before.linked.filter (fun pair => pair.1.name == boundary.name)).length = 1 ∧
    (boundary.after.linked.filter (fun pair => pair.1.name == boundary.name)).length = 1 := by
  obtain ⟨left, leftEnv, _, _, _, leftOnly, _⟩ := boundary.sourceProvider
  obtain ⟨right, rightEnv, _, _, _, rightOnly, _⟩ := boundary.candidateProvider
  constructor
  · simpa using congrArg List.length leftOnly
  · simpa using congrArg List.length rightOnly

/-- The public entry names resolve to the same live cells in every world. -/
theorem SharedProvider.sameExports {source candidate : Program}
    (boundary : ∀ w, SharedProvider source candidate w) : SameExports source candidate := by
  apply sameExports_of_match
  intro w
  rw [exportsMatch_from_checked (boundary w).before (boundary w).after]
  exact (boundary w).entryCells


end CoreJS.LinkedBehavior
