import EsmSplit.CoreJS.Fuel

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

/-- Kernel reduction discharges both premises with a symbolic world. A matching throw,
unknown result or exhausted bound cannot discharge the first premise. -/
theorem of_evaluation {source candidate : Program} (fuel : Nat)
    (completed : ∀ W, normal (runProgram W fuel source) = true)
    (equal : ∀ W, runProgram W fuel source = runProgram W fuel candidate) :
    Equivalent source candidate := by
  apply of_runs fuel
  intro W
  have h := completed W
  have eq := equal W
  cases hs : runProgram W fuel source with
  | ok _ t => exact ⟨t, rfl, eq.symm.trans hs⟩
  | _ => simp [hs, normal] at h

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

end CoreJS.LinkedBehavior
