import EsmSplit.Plan

/-! Name resolution for source units that publish local names. A terminal
export-list unit has no executable body, but its referenced locals must still
appear among its source-plan uses. -/

namespace EsmSplit.MetadataImports

/-- An external source binding used by an owned unit must be imported from
its original declaring owner in any valid plan. The unit may be an export
list: it need not be an executable CoreJS statement. -/
theorem external_use_imported {B : Bundle} {S : Spec} {P : Plan} (valid : Valid B S P)
    {md : Module} (moduleIn : md ∈ P.mods) {ordinal : Nat} (owned : ordinal ∈ md.stmts)
    {u : Name} (used : u ∈ (B.stmt ordinal).eager ++ (B.stmt ordinal).lazy)
    {owner : ModName} (sourceOwner : P.declaredIn B u = .declared owner)
    (notLocal : md.declares B u = false) :
    ∃ e ∈ md.imports, u ∈ e.2 ∧ e.1 = owner := by
  have h := valid.2.2.2.2.2.1 md moduleIn ordinal owned u used
  rw [sourceOwner] at h
  unfold Plan.resolve at h
  rw [notLocal] at h
  simp only [Bool.false_eq_true, ↓reduceIte] at h
  cases found : md.imports.find? (fun e => e.2.contains u) with
  | none => rw [found] at h; cases h
  | some e =>
    rw [found] at h
    have inImports : e ∈ md.imports := List.mem_of_find?_eq_some found
    have selected := @List.find?_some (ModName × List Name) (fun e => e.2.contains u) e md.imports found
    have contains : u ∈ e.2 := List.contains_iff_mem.1 selected
    cases ok : P.declaresIn B e.1 u && P.exportsName B e.1 u with
    | false => simp [ok] at h
    | true =>
      simp [ok] at h
      exact ⟨e, inImports, contains, h⟩

#print axioms external_use_imported

end EsmSplit.MetadataImports
