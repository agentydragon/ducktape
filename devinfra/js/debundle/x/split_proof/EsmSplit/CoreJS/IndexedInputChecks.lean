import EsmSplit.CoreJS.IndexedUnitsAgree

/-! Finite checks for the original sparse CoreJS body and its actual planner bundle. -/

namespace CoreJS.IndexedInputChecks

/-- All quantifiers range over source rows, declarations or free names. -/
def FootprintChecks (body : List (Nat × Stmt)) (B : EsmSplit.Bundle) : Prop :=
  (body.map Prod.fst).Nodup ∧ ∀ p ∈ body,
    p.1 < B.length ∧
    (∀ name ∈ (B.stmt p.1).defs, name ∈ p.2.bindings.map Prod.fst) ∧
    (∀ name ∈ p.2.bindings.map Prod.fst, name ∈ (B.stmt p.1).defs) ∧
    (∀ name ∈ p.2.free, name ∈ (B.stmt p.1).defs ∨ name ∈ (B.stmt p.1).eager ++ (B.stmt p.1).lazy)

/-- Every bundle unit declaring a name has its original executable row. -/
def DefinitionChecks (body : List (Nat × Stmt)) (B : EsmSplit.Bundle) : Prop :=
  ∀ id ∈ List.range B.length, (B.stmt id).defs ≠ [] → (body.lookup id).isSome = true

instance (body : List (Nat × Stmt)) (B : EsmSplit.Bundle) : Decidable (FootprintChecks body B) := by
  unfold FootprintChecks
  infer_instance

instance (body : List (Nat × Stmt)) (B : EsmSplit.Bundle) : Decidable (DefinitionChecks body B) := by
  unfold DefinitionChecks
  infer_instance

/-- Finite source/bundle checks imply the reusable scope and link premises. -/
theorem checked_footprints {body : List (Nat × Stmt)} {B : EsmSplit.Bundle}
    (checked : FootprintChecks body B) : IndexedExportCells.Footprints body B ∧ IndexedUnitsAgree.FreeFootprints body B := by
  refine ⟨⟨checked.1, ?_⟩, ?_⟩
  · intro id stmt found
    have row := checked.2 (id, stmt) (lookup_mem found)
    exact ⟨row.1, fun name => ⟨row.2.1 name, row.2.2.1 name⟩⟩
  · intro id stmt found name member
    exact (checked.2 (id, stmt) (lookup_mem found)).2.2.2 name member

theorem checked_definitions {body : List (Nat × Stmt)} {B : EsmSplit.Bundle}
    (checked : DefinitionChecks body B) : IndexedPlanCells.DefinitionsPresent body B := by
  intro id within name member
  have nonempty : (B.stmt id).defs ≠ [] := by
    intro empty
    rw [empty] at member
    cases member
  have someRow := checked id (List.mem_range.2 within) nonempty
  exact Option.isSome_iff_exists.1 someRow


end CoreJS.IndexedInputChecks
