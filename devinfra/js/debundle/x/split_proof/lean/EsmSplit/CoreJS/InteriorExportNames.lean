import EsmSplit.CoreJS.DeclaringOwner
import EsmSplit.CoreJS.ExportRows
import EsmSplit.LocalExportUnit
import EsmSplit.MetadataImports

/-! Source/plan facts for one non-executable local export list at its original
ordinal, independent of its position among executable units. -/

namespace CoreJS.InteriorExportNames

/-- The actual source table and original identity of a local export
list. The typed row comes from the same source as the CoreJS module. -/
structure SourceRow (source : Module) (row : EsmSplit.LocalExportUnit) : Prop where
  exports : source.exports = row.names
  absent : row.ordinal ∉ source.body.map Prod.fst

/-- An authored owner in a faithful full plan retains the original metadata
unit. The row must not occur as a source executable unit. -/
theorem row_owner {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    {source : Module} {row : EsmSplit.LocalExportUnit} (shape : SourceRow source row)
    (faithful : EsmSplit.Faithful B S P) {owner : EsmSplit.Module} (ownerIn : owner ∈ P.mods)
    (within : row.ordinal < B.length) (authored : S.claimants B row.ordinal = [owner.name]) :
    row.ordinal ∈ owner.stmts ∧ ∀ stmt, (row.ordinal, stmt) ∉ source.body := by
  refine ⟨(faithful.2.2.2.2.1 owner ownerIn row.ordinal (List.mem_range.2 within)).2 authored, ?_⟩
  intro stmt member
  exact shape.absent (List.mem_map.mpr ⟨(row.ordinal, stmt), member, rfl⟩)

/-- The ordered source pair is present in the owner module's actual planner
export metadata, with the public and local names kept distinct. -/
theorem row_pair_published {B : EsmSplit.Bundle} {owner : EsmSplit.Module} {row : EsmSplit.LocalExportUnit}
    (owned : row.ordinal ∈ owner.stmts) (footprint : row.FootprintMatches (B.stmt row.ordinal))
    {publicName localName : EsmSplit.Name} (pair : (publicName, localName) ∈ row.names) :
    (publicName, localName) ∈ owner.namedExports B := by
  have hexport := (row.matched_local_use_and_public footprint pair).2
  unfold EsmSplit.Module.namedExports EsmSplit.Module.namedExportsFrom
  apply List.mem_append_right
  unfold EsmSplit.localExports EsmSplit.Module.publishedFrom
  apply List.mem_filterMap.mpr
  refine ⟨(publicName, some localName), ?_, rfl⟩
  apply List.mem_flatMap.mpr
  refine ⟨row.ordinal, owned, ?_⟩
  simpa only [EsmSplit.Bundle.toArray_getD] using hexport

/-- If the one original row is the only exporting unit in the authored entry,
its actual planner export table is precisely the original ordered row. The
entry can also own executable units before or after the metadata. -/
theorem exact_public_table {B : EsmSplit.Bundle} {owner : EsmSplit.Module} {row : EsmSplit.LocalExportUnit}
    (unique : owner.stmts.Nodup) (owned : row.ordinal ∈ owner.stmts) (glue : owner.exports = [])
    (onlyRow : ∀ id ∈ owner.stmts, id ≠ row.ordinal → (B.stmt id).exports = [])
    (footprint : row.FootprintMatches (B.stmt row.ordinal)) : owner.namedExports B = row.names := by
  have matched := ExportRows.singleton_matches footprint onlyRow
  have selected := ExportRows.singleton_selection row owner.stmts unique owned
  simpa using ExportRows.exact_entry_exports matched selected glue

/-- Every row local resolves to its authored original declaration owner. The
row owner either declares it or imports it from that exact owner; no value
comparison or input-pair execution is involved. -/
theorem row_pair_binding {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (valid : EsmSplit.Valid B S P) {rowOwner declarationOwner : EsmSplit.Module}
    (rowOwnerIn : rowOwner ∈ P.mods) (declarationOwnerIn : declarationOwner ∈ P.mods)
    {row : EsmSplit.LocalExportUnit} (owned : row.ordinal ∈ rowOwner.stmts)
    (footprint : row.FootprintMatches (B.stmt row.ordinal))
    {declOrdinal : Nat} (declOwned : declOrdinal ∈ declarationOwner.stmts)
    {publicName localName : EsmSplit.Name} (pair : (publicName, localName) ∈ row.names)
    (declared : localName ∈ (B.stmt declOrdinal).defs) :
    (publicName, localName) ∈ rowOwner.namedExports B ∧
    S.claimants B declOrdinal = [declarationOwner.name] ∧
    P.declaredIn B localName = .declared declarationOwner.name ∧
    ((∃ j ∈ rowOwner.stmts, localName ∈ (B.stmt j).defs) ∨
      ∃ e ∈ rowOwner.imports, localName ∈ e.2 ∧ e.1 = declarationOwner.name) := by
  have ⟨claim, binding⟩ := DeclaringOwner.declared_in_original_owner valid.1 valid.2.2.1
    declarationOwnerIn declOwned declared
  refine ⟨row_pair_published owned footprint pair, claim, binding, ?_⟩
  by_cases localDecl : rowOwner.declares B localName = true
  · left
    obtain ⟨j, hj, hname⟩ := List.any_eq_true.1 localDecl
    exact ⟨j, hj, List.contains_iff_mem.1 hname⟩
  · right
    have notLocal : rowOwner.declares B localName = false := Bool.eq_false_iff.mpr localDecl
    exact EsmSplit.MetadataImports.external_use_imported valid rowOwnerIn owned
      (row.matched_local_use_and_public footprint pair).1 binding notLocal

#print axioms exact_public_table
#print axioms row_owner
#print axioms row_pair_published
#print axioms row_pair_binding

end CoreJS.InteriorExportNames
