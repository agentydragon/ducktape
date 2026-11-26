import EsmSplit.CoreJS.Split

/-! Connect an original declaring unit and its authored owner to the name
resolution used by the plan. The source bundle may include non-executable
metadata; no CoreJS footprint length assumption is needed. -/

namespace CoreJS.DeclaringOwner

/-- A uniquely defined source name resolves to the owner of its original
declaring unit. Faithfulness also identifies that owner as the unique authored
claimant of the original ordinal. -/
theorem declared_in_original_owner {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (unique : EsmSplit.UniqueDefs B) (faithful : EsmSplit.Faithful B S P)
    {owner : EsmSplit.Module} (ownerIn : owner ∈ P.mods) {ordinal : Nat}
    (owned : ordinal ∈ owner.stmts) {u : Name} (declared : u ∈ (B.stmt ordinal).defs) :
    S.claimants B ordinal = [owner.name] ∧ P.declaredIn B u = .declared owner.name := by
  have ordinalBound : ordinal < B.length := faithful.2.2.2.2.2.1 owner ownerIn ordinal owned
  have ownerClaim : S.claimants B ordinal = [owner.name] :=
    (faithful.2.2.2.2.1 owner ownerIn ordinal (List.mem_range.2 ordinalBound)).1 owned
  have ownerDeclares : owner.declares B u = true :=
    List.any_eq_true.2 ⟨ordinal, owned, List.contains_iff_mem.2 declared⟩
  have present : (P.mods.find? (·.declares B u)).isSome = true :=
    List.find?_isSome.2 ⟨owner, ownerIn, ownerDeclares⟩
  refine ⟨ownerClaim, ?_⟩
  unfold EsmSplit.Plan.declaredIn
  cases found : P.mods.find? (·.declares B u) with
  | none => simp [found] at present
  | some md =>
    have mdIn : md ∈ P.mods := List.mem_of_find?_eq_some found
    have mdDeclares := @List.find?_some EsmSplit.Module (fun m => m.declares B u) md P.mods found
    obtain ⟨j, hj, hjDecl⟩ := List.any_eq_true.1 mdDeclares
    have jBound : j < B.length := faithful.2.2.2.2.2.1 md mdIn j hj
    have same : j = ordinal := unique_defs unique jBound ordinalBound (List.contains_iff_mem.1 hjDecl) declared
    subst j
    have mdClaim : S.claimants B ordinal = [md.name] :=
      (faithful.2.2.2.2.1 md mdIn ordinal (List.mem_range.2 ordinalBound)).1 hj
    rw [ownerClaim] at mdClaim
    have names : owner.name = md.name := by simpa using mdClaim
    simp [names]

#print axioms declared_in_original_owner

end CoreJS.DeclaringOwner
