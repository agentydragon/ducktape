import EsmSplit.CoreJS.Fuel
import EsmSplit.CoreJS.Scope

namespace CoreJS.LinkedBehavior

/-- Both module graphs finish normally with the same entire state in each world. -/
def Equivalent (source candidate : Program) : Prop :=
  ∀ W, ∃ t : State W, Ends W source (.ok () t) ∧ Ends W candidate (.ok () t)

/-- Convert a pair of bounded runs into a fuel-independent result. -/
theorem of_runs {source candidate : Program} (fuel : Nat)
    (runs : ∀ W, ∃ t : State W,
      runProgram W fuel source = .ok () t ∧ runProgram W fuel candidate = .ok () t) :
    Equivalent source candidate := by
  intro W
  obtain ⟨t, hs, hc⟩ := runs W
  exact ⟨t, ⟨fuel, hs, by simp⟩, ⟨fuel, hc, by simp⟩⟩

def normal {W : World} : Res W Unit → Bool
  | .ok .. => true
  | _ => false

/-- Normal completion and execution equality share the source result during
kernel reduction in a symbolic world. -/
theorem of_evaluation {source candidate : Program} (fuel : Nat)
    (checks : ∀ W, normal (runProgram W fuel source) = true ∧
      runProgram W fuel source = runProgram W fuel candidate) :
    Equivalent source candidate := by
  apply of_runs fuel
  intro W
  obtain ⟨h, eq⟩ := checks W
  cases hs : runProgram W fuel source with
  | ok _ t => exact ⟨t, rfl, eq.symm.trans hs⟩
  | _ => simp [hs, normal] at h

/-- Two normal probe executions with different final states cannot satisfy the
all-world linked behavior claim. `Ends.unique` transports each concrete result
to the shared state required by `Equivalent`. -/
theorem different_probe_states_refute_equivalence {w : World} {source candidate : Program} {fuel : Nat}
    {sourceState candidateState : State w}
    (sourceRun : runProgram w fuel source = .ok () sourceState)
    (candidateRun : runProgram w fuel candidate = .ok () candidateState)
    (different : sourceState ≠ candidateState) :
    ¬ Equivalent source candidate := by
  intro equivalent
  obtain ⟨shared, sourceEnds, candidateEnds⟩ := equivalent w
  have sourceProbeEnds : Ends w source (.ok () sourceState) :=
    ⟨fuel, sourceRun, by simp⟩
  have candidateProbeEnds : Ends w candidate (.ok () candidateState) :=
    ⟨fuel, candidateRun, by simp⟩
  have sourceStateEq : sourceState = shared := by
    have sameResult := Ends.unique sourceProbeEnds sourceEnds
    injection sameResult with _ stateEq
  have candidateStateEq : candidateState = shared := by
    have sameResult := Ends.unique candidateProbeEnds candidateEnds
    injection sameResult with _ stateEq
  exact different (sourceStateEq.trans candidateStateEq.symm)

private def entryExports (entry : String) (linked : List (Module × Env)) :
    Option (List (Name × Addr)) := do
  let (md, env) ← linked.find? (fun (md, _) => md.name == entry)
  md.exports.mapM fun (name, binding) => do return (name, ← env.vars.lookup binding)

private def readExports (p : Program) (r : Res W (List (Module × Env))) : Option (List (Name × Addr)) :=
  match r with
  | .ok linked _ => entryExports p.entry linked
  | _ => none

/-- The entry's public names and the live cells the real linker binds them to. -/
def exports (W : World) (p : Program) : Option (List (Name × Addr)) :=
  readExports p (link p (initialState W p.regions))

def SameExports (source candidate : Program) : Prop :=
  ∀ W, ∃ a b, exports W source = some a ∧ exports W candidate = some b ∧ a.Perm b

private def compareExports : Option (List (Name × Addr)) → Option (List (Name × Addr)) → Bool
  | some a, some b => decide (a.Perm b)
  | _, _ => false

def exportsMatch (W : World) (source candidate : Program) : Bool :=
  compareExports (exports W source) (exports W candidate)

theorem sameExports_of_match {source candidate : Program} (h : ∀ W, exportsMatch W source candidate = true) :
    SameExports source candidate := by
  intro W
  have yes := h W
  change compareExports (exports W source) (exports W candidate) = true at yes
  cases a : exports W source <;> cases b : exports W candidate <;> simp [a, b, compareExports] at yes
  exact ⟨_, _, rfl, rfl, yes⟩

theorem ends_iff {source candidate : Program} (h : Equivalent source candidate) (W : World)
    (r : Res W Unit) : Ends W source r ↔ Ends W candidate r := by
  obtain ⟨_, hs, hc⟩ := h W
  constructor
  · intro hr
    exact hr.unique hs ▸ hc
  · intro hr
    exact hr.unique hc ▸ hs

/-- A successful link together with the resulting scopes and initial execution state. -/
structure CheckedLink (p : Program) (w : World) where
  linked : List (Module × Env)
  ready : State w
  equation : link p (initialState w p.regions) = .ok linked ready

/-- Match public names and live cells using previously checked linker output. -/
def exportsMatchFrom {w : World} {source candidate : Program}
    (before : CheckedLink source w) (after : CheckedLink candidate w) : Bool :=
  compareExports (entryExports source.entry before.linked) (entryExports candidate.entry after.linked)

theorem exportsMatch_from_checked {w : World} {source candidate : Program}
    (before : CheckedLink source w) (after : CheckedLink candidate w) :
    exportsMatch w source candidate = exportsMatchFrom before after := by
  unfold exportsMatch exports
  rw [before.equation, after.equation]
  rfl

theorem run_after_link {p : Program} {w : World} (fuel : Nat) (checked : CheckedLink p w) :
    runProgram w fuel p = runUnits fuel (unitsOf checked.linked) checked.ready := by
  unfold runProgram
  rw [M.bind_apply, checked.equation]

end CoreJS.LinkedBehavior
