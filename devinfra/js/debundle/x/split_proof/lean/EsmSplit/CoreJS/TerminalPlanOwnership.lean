import EsmSplit.CoreJS.TerminalSource
import EsmSplit.Split

/-! Original-unit ownership for the actual full-bundle plan. This is separate
from execution and export equivalence: selectors and source ordinals remain
visible even though the terminal export unit is not a CoreJS statement. -/

namespace CoreJS.TerminalPlanOwnership

/-- Every original executable ordinal has exactly its authored owner in the
constructed module body; the sole terminal metadata ordinal has exactly its
authored owner in the plan, belongs to the entry, and emits no CoreJS unit.
The bundle-length premise must be checked against the full original input. -/
theorem all_original_owners {ss : List Stmt} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    (row : EsmSplit.LocalExportUnit) (terminal : row.ordinal = ss.length)
    (allUnits : B.length = row.ordinal + 1)
    (valid : EsmSplit.Valid B S (EsmSplit.mkPlan B S)) (entry : EsmSplit.Module)
    (found : (EsmSplit.mkPlan B S).find (EsmSplit.mkPlan B S).entry = some entry)
    (units : entry.stmts = [row.ordinal]) :
    (∀ i, i < B.length → i < ss.length ∨ i = row.ordinal) ∧
    (∀ m ∈ (EsmSplit.mkPlan B S).mods, ∀ i (hi : i < ss.length),
      (i, ss[i]) ∈ (moduleOf ss B (EsmSplit.mkPlan B S) m).body ↔ S.claimants B i = [m.name]) ∧
    (∀ m ∈ (EsmSplit.mkPlan B S).mods,
      row.ordinal ∈ m.stmts ↔ S.claimants B row.ordinal = [m.name]) ∧
    S.claimants B row.ordinal = [entry.name] ∧
    (moduleOf ss B (EsmSplit.mkPlan B S) entry).body = [] := by
  have faithful := valid.2.2.1
  have rowBound : row.ordinal < B.length := by omega
  have entryIn : entry ∈ (EsmSplit.mkPlan B S).mods := List.mem_of_find?_eq_some found
  have rowOwned : row.ordinal ∈ entry.stmts := by rw [units]; simp
  refine ⟨?_, ?_, ?_, ?_, ?_⟩
  · intro i hi
    omega
  · intro m hm i hi
    apply ProjectedOrder.authored_executable_owner faithful hm hi
    omega
  · intro m hm
    exact faithful.2.2.2.2.1 m hm row.ordinal (List.mem_range.2 rowBound)
  · exact (faithful.2.2.2.2.1 entry entryIn row.ordinal (List.mem_range.2 rowBound)).1 rowOwned
  · change entry.stmts.filterMap (numbered ss) = []
    rw [units, terminal]
    simp [numbered]

#print axioms all_original_owners

end CoreJS.TerminalPlanOwnership
