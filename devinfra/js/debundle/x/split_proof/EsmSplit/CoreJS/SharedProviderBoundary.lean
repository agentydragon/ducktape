import EsmSplit.CoreJS.LinkedBehavior
import EsmSplit.CoreJS.Move
import EsmSplit.CoreJS.Split

/-! Transport a consumer schedule proof through one common linked provider prefix. -/

namespace CoreJS.LinkedBehavior

variable {w : World}

/-- The provider loads first, exactly once, and its actual units are the prefix. -/
def ProviderPrefix (name : String) (linked : List (Module × Env)) (pre : List LinkedUnit) : Prop :=
  ∃ m env,
    linked.head? = some (m, env) ∧
      m.name = name ∧
      m.imports = [] ∧
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

/-- Check the consumer suffix only at a state reached by successful provider evaluation.
The checked links put the provider first and once, so the suffix may rely on facts established
by its initializer. A provider throw, unknown result or fuel exhaustion skips both suffixes. -/
theorem SharedProvider.run_eq_after_prefix {source candidate : Program}
    (boundary : SharedProvider source candidate w) (fuel : Nat)
    (suffix :
      ∀ state,
        runUnits fuel boundary.sourcePre boundary.before.ready = .ok () state →
          runUnits fuel boundary.sourceTail state = runUnits fuel boundary.candidateTail state) :
    runProgram w fuel source = runProgram w fuel candidate := by
  rw [run_after_link fuel boundary.before, run_after_link fuel boundary.after,
    boundary.sourceSchedule, boundary.candidateSchedule, runUnits_append, runUnits_append]
  simp only [M.bind_apply, ← boundary.providerPrefixResult fuel]
  cases outcome : runUnits fuel boundary.sourcePre boundary.before.ready with
  | ok value state =>
    cases value
    exact suffix state outcome
  | throw
  | unknown
  | fuel => rfl

/-- Preserve a consumer suffix whose units and their scopes agree in order. -/
theorem SharedProvider.run_eq {source candidate : Program}
    (boundary : SharedProvider source candidate w) (fuel : Nat)
    (suffix : SameUnits boundary.sourceTail boundary.candidateTail) :
    runProgram w fuel source = runProgram w fuel candidate :=
  boundary.run_eq_after_prefix fuel fun state _ => congrFun (runUnits_same fuel suffix) state

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
