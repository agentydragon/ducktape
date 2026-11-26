import EsmSplit.CoreJS.DeclaringOwner
import EsmSplit.LocalExportUnit
import EsmSplit.MetadataImports

/-! Source-plan linkage facts for an original local-export unit. They retain
its original ordinal, public/local pairs and the authored declaring owner;
they do not assert successful CoreJS linking or address preservation. -/

namespace CoreJS.TerminalEntryNames

/-- Every row pair is published by the module owning the original unit. -/
theorem published {B : EsmSplit.Bundle} {md : EsmSplit.Module} {row : EsmSplit.LocalExportUnit}
    (owned : row.ordinal ∈ md.stmts) (footprint : row.FootprintMatches (B.stmt row.ordinal))
    {publicName localName : EsmSplit.Name} (pair : (publicName, localName) ∈ row.names) :
    (publicName, localName) ∈ md.namedExports B := by
  have hexport := (row.matched_local_use_and_public footprint pair).2
  unfold EsmSplit.Module.namedExports EsmSplit.Module.namedExportsFrom
  apply List.mem_append_right
  unfold EsmSplit.localExports EsmSplit.Module.publishedFrom
  apply List.mem_filterMap.mpr
  refine ⟨(publicName, some localName), ?_, rfl⟩
  apply List.mem_flatMap.mpr
  refine ⟨row.ordinal, owned, ?_⟩
  simpa only [EsmSplit.Bundle.toArray_getD] using hexport

/-- A terminal export's local resolves to its original authored declaring
owner. It is either declared in the entry module or imported from that exact
owner. No value comparison, executable projection, or candidate execution is
used. -/
theorem bound_to_original_owner {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (valid : EsmSplit.Valid B S P) {entry owner : EsmSplit.Module}
    (entryIn : entry ∈ P.mods) (ownerIn : owner ∈ P.mods)
    {row : EsmSplit.LocalExportUnit} (owned : row.ordinal ∈ entry.stmts)
    (footprint : row.FootprintMatches (B.stmt row.ordinal))
    {declOrdinal : Nat} (declOwned : declOrdinal ∈ owner.stmts)
    {publicName localName : EsmSplit.Name} (pair : (publicName, localName) ∈ row.names)
    (declared : localName ∈ (B.stmt declOrdinal).defs) :
    (publicName, localName) ∈ entry.namedExports B ∧
    S.claimants B declOrdinal = [owner.name] ∧ P.declaredIn B localName = .declared owner.name ∧
    ((∃ j ∈ entry.stmts, localName ∈ (B.stmt j).defs) ∨
      ∃ e ∈ entry.imports, localName ∈ e.2 ∧ e.1 = owner.name) := by
  have ⟨claim, ownerBinding⟩ := DeclaringOwner.declared_in_original_owner valid.1 valid.2.2.1 ownerIn declOwned declared
  refine ⟨published owned footprint pair, claim, ownerBinding, ?_⟩
  by_cases localDecl : entry.declares B localName = true
  · left
    obtain ⟨j, hj, hname⟩ := List.any_eq_true.1 localDecl
    exact ⟨j, hj, List.contains_iff_mem.1 hname⟩
  · right
    have notLocal : entry.declares B localName = false := Bool.eq_false_iff.mpr localDecl
    exact EsmSplit.MetadataImports.external_use_imported valid entryIn owned
      (row.matched_local_use_and_public footprint pair).1 ownerBinding notLocal

#print axioms published
#print axioms bound_to_original_owner

end CoreJS.TerminalEntryNames
