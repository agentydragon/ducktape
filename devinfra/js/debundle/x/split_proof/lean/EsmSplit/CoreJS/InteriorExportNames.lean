import EsmSplit.CoreJS.DeclaringOwner
import EsmSplit.LocalExportUnit
import EsmSplit.MetadataImports

/-! Source/plan facts for one non-executable local export list at its original
ordinal. The source may contain executable units on either side of the row. -/

namespace CoreJS.InteriorExportNames

/-- The actual source table and original identity of an interior local export
list. The typed row comes from the same source as the CoreJS module. -/
structure SourceRow (source : Module) (row : EsmSplit.LocalExportUnit) : Prop where
  exports : source.exports = row.names
  absent : row.ordinal ∉ source.body.map Prod.fst
  before : ∃ id ∈ source.body.map Prod.fst, id < row.ordinal
  after : ∃ id ∈ source.body.map Prod.fst, row.ordinal < id

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

private theorem exports_without_row (B : EsmSplit.Bundle) (ordinal : Nat) :
    ∀ ids : List Nat, ordinal ∉ ids →
      (∀ id ∈ ids, id ≠ ordinal → (B.stmt id).exports = []) →
      ids.flatMap (fun id => (B.stmt id).exports) = []
  | [], _, _ => rfl
  | id :: rest, absent, onlyRow => by
    have notHead : id ≠ ordinal := fun eq => absent (by simp [eq])
    have headEmpty := onlyRow id (by simp) notHead
    have absentRest : ordinal ∉ rest := fun h => absent (by simp [h])
    have restOnly : ∀ id ∈ rest, id ≠ ordinal → (B.stmt id).exports = [] :=
      fun id member ne => onlyRow id (by simp [member]) ne
    simp [headEmpty, exports_without_row B ordinal rest absentRest restOnly]

private theorem exports_with_one_row (B : EsmSplit.Bundle) (ordinal : Nat) :
    ∀ ids : List Nat, ids.Nodup → ordinal ∈ ids →
      (∀ id ∈ ids, id ≠ ordinal → (B.stmt id).exports = []) →
      ids.flatMap (fun id => (B.stmt id).exports) = (B.stmt ordinal).exports
  | [], _, selected, _ => by simp at selected
  | id :: rest, unique, selected, only => by
    by_cases same : id = ordinal
    · subst id
      have absent : ordinal ∉ rest := (List.nodup_cons.mp unique).1
      have tail : rest.flatMap (fun id => (B.stmt id).exports) = [] :=
        exports_without_row B ordinal rest absent (fun j hj ne => only j (by simp [hj]) ne)
      simp [tail]
    · have later : ordinal ∈ rest := (List.mem_cons.mp selected).resolve_left (Ne.symm same)
      have head := only id (by simp) same
      have tailOnly : ∀ j ∈ rest, j ≠ ordinal → (B.stmt j).exports = [] :=
        fun j hj ne => only j (by simp [hj]) ne
      have tail := exports_with_one_row B ordinal rest (List.nodup_cons.mp unique).2 later tailOnly
      simp [head, tail]

/-- If the one original row is the only exporting unit in the authored entry,
its actual planner export table is precisely the original ordered row. The
entry can also own executable units before or after the metadata. -/
theorem exact_public_table {B : EsmSplit.Bundle} {owner : EsmSplit.Module} {row : EsmSplit.LocalExportUnit}
    (unique : owner.stmts.Nodup) (owned : row.ordinal ∈ owner.stmts) (glue : owner.exports = [])
    (onlyRow : ∀ id ∈ owner.stmts, id ≠ row.ordinal → (B.stmt id).exports = [])
    (footprint : row.FootprintMatches (B.stmt row.ordinal)) : owner.namedExports B = row.names := by
  have full : owner.stmts.flatMap (fun id => (B.stmt id).exports) = (B.stmt row.ordinal).exports :=
    exports_with_one_row B row.ordinal owner.stmts unique owned onlyRow
  unfold EsmSplit.Module.namedExports EsmSplit.Module.namedExportsFrom EsmSplit.Module.publishedFrom
  simp only [glue, List.nil_append, EsmSplit.Bundle.toArray_getD, full, footprint.exports,
    EsmSplit.localExports, List.filterMap_map]
  generalize row.names = names
  induction names with
  | nil => rfl
  | cons pair rest ih => cases pair <;> simp [Function.comp_def]

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
