import EsmSplit.CoreJS.IndexedAllocatedCells
import EsmSplit.CoreJS.EntryExports
import EsmSplit.CoreJS.ProjectedOrder

/-! Structural facts for the actual CoreJS scope and entry-linking operations.
No source/candidate execution comparison is used. -/

namespace CoreJS.IndexedEntryLink

variable {W : World}

/-- Declaration statements do not perform the linker's function-hoisting
step, regardless of their initializer or module environment. -/
theorem initFunctions_consts (units : List LinkedUnit)
    (allConst : ∀ u ∈ units, ∃ name init, u.stmt = .decl .const name init) :
    (initFunctions units : M W Unit) = pure () := by
  induction units with
  | nil => rfl
  | cons u rest ih =>
    obtain ⟨name, init, shape⟩ := allConst u (by simp)
    have tail : ∀ v ∈ rest, ∃ name init, v.stmt = .decl .const name init :=
      fun v member => allConst v (List.mem_cons_of_mem _ member)
    rcases u with ⟨id, stmt, env⟩
    change stmt = .decl .const name init at shape
    subst stmt
    simp only [initFunctions, initFunction, ih tail, M.pure_bind]

/-- The same fact holds for the linker’s identity-sorted unit list. -/
theorem sorted_initFunctions_consts (linked : List (Module × Env))
    (allConst : ∀ u ∈ unitsOf linked, ∃ name init, u.stmt = .decl .const name init) :
    (initFunctions ((unitsOf linked).mergeSort fun a b => decide (a.id ≤ b.id)) : M W Unit) = pure () := by
  apply initFunctions_consts
  intro u member
  exact allConst u ((List.mergeSort_perm _ _).mem_iff.1 member)

#print axioms initFunctions_consts
#print axioms sorted_initFunctions_consts

end CoreJS.IndexedEntryLink
