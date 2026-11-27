import EsmSplit.CoreJS.TerminalPlanOwnership
import EsmSplit.CoreJS.TerminalExportPlacement

/-! Connect the original selector resolver to the unchanged full-bundle plan.
The executable check compares the two independently produced assignments;
it does not create a compact bundle or inspect a candidate execution. -/

namespace CoreJS.TerminalPlanSelection

/-- Every resolved original-unit label agrees with the authored claimants on
the same full bundle that the actual planner uses. -/
def ClaimantsMatch (B : EsmSplit.Bundle) (S : EsmSplit.Spec) (selected : List UnitPlacement) : Prop :=
  ∀ label ∈ selected, S.claimants B label.1 = [label.2]

instance (B : EsmSplit.Bundle) (S : EsmSplit.Spec) (selected : List UnitPlacement) :
    Decidable (ClaimantsMatch B S selected) := by
  unfold ClaimantsMatch
  infer_instance

/-- The original selection and actual plan place the same executable units
in exactly the same modules. The selected terminal metadata belongs to the
actual entry; the resolver's separate uniqueness and coverage proofs still
reject repeated or missing selector occurrences. -/
theorem selected_owners {ss : List Stmt} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    (row : EsmSplit.LocalExportUnit) (terminal : row.ordinal = ss.length)
    (allUnits : B.length = row.ordinal + 1)
    (valid : EsmSplit.Valid B S (EsmSplit.mkPlan B S)) (entry : EsmSplit.Module)
    (found : (EsmSplit.mkPlan B S).find (EsmSplit.mkPlan B S).entry = some entry)
    (owned : row.ordinal ∈ entry.stmts)
    (source : TerminalExportPlacement.TerminalSource)
    (sourceOrdinal : source.exportOrdinal = row.ordinal)
    (sourceIds : source.module.body.map Prod.fst = List.range ss.length)
    (selection : TerminalExportPlacement.Selection source S)
    (matching : ClaimantsMatch B S selection.selected) :
    (∀ m ∈ (EsmSplit.mkPlan B S).mods, ∀ i (hi : i < ss.length),
      (i, ss[i]) ∈ (moduleOf ss B (EsmSplit.mkPlan B S) m).body ↔ (i, m.name) ∈ selection.selected) ∧
    (row.ordinal, entry.name) ∈ selection.selected ∧
    (∀ owner, (row.ordinal, owner) ∈ selection.selected → owner = entry.name) := by
  have owners := TerminalPlanOwnership.all_original_owners row terminal allUnits valid entry found owned
  have selectedId : ∀ i, i < ss.length → ∃ owner, (i, owner) ∈ selection.selected := by
    intro i hi
    have hin : i ∈ source.exportOrdinal :: source.module.body.map Prod.fst := by
      simp [sourceIds, hi]
    have hsel : i ∈ selection.selected.map Prod.fst := selection.coverage.mem_iff.2 hin
    obtain ⟨⟨id, owner⟩, member, eq⟩ := List.mem_map.1 hsel
    simp only at eq
    subst id
    exact ⟨owner, member⟩
  have exportMember : (row.ordinal, S.entry) ∈ selection.selected := by
    simpa [sourceOrdinal] using selection.exportOwner
  have entryName : entry.name = S.entry := by
    have h := matching (row.ordinal, S.entry) exportMember
    rw [owners.2.2.2.1] at h
    simpa using h
  refine ⟨?_, ?_, ?_⟩
  · intro m hm i hi
    constructor
    · intro placed
      have claimant := ((owners.2.1 m hm i hi).1 placed)
      obtain ⟨owner, member⟩ := selectedId i hi
      have resolved := matching (i, owner) member
      rw [claimant] at resolved
      have eq : m.name = owner := by simpa using resolved
      simpa [eq] using member
    · intro member
      exact (owners.2.1 m hm i hi).2 (matching (i, m.name) member)
  · simpa [entryName] using exportMember
  · intro owner member
    have h := matching (row.ordinal, owner) member
    rw [owners.2.2.2.1] at h
    exact (by simpa using h : entry.name = owner).symm

#print axioms selected_owners

end CoreJS.TerminalPlanSelection
