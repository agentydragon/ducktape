import EsmSplit.CoreJS.IndexedPlanCells
import EsmSplit.CoreJS.InteriorExportNames

/-! Public name-to-cell linkage for an authored interior export row. The
projection fields describe the actual constructor, not a checked output pair. -/

namespace CoreJS.InteriorPlanExports

/-- Full-plan coverage gives each declared source unit an owner without a
second owner search or a fabricated placement. -/
theorem declaration_owner {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (valid : EsmSplit.Valid B S P) {id : Nat} (within : id < B.length) :
    ∃ owner ∈ P.mods, id ∈ owner.stmts := by
  obtain ⟨-, -, -, -, -, -, -, ⟨-, -, all⟩, -⟩ := valid
  have planned : id ∈ P.execOrder := all id (List.mem_range.2 within)
  simp only [EsmSplit.Plan.execOrder, List.mem_flatMap] at planned
  obtain ⟨name, _, hasUnit⟩ := planned
  cases found : P.find name with
  | none => simp [found] at hasUnit
  | some owner =>
    simp only [found, Option.map_some, Option.getD_some] at hasUnit
    exact ⟨owner, List.mem_of_find?_eq_some found, hasUnit⟩

/-- A valid authored plan keeps the interior row in its original owner, emits
exactly the original ordered public aliases there, and binds each local name
in that module to the same present source address. Other executable units may
occur before or after the row. Import resolution is the real CoreJS resolver. -/
theorem row_scope_cells {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} {program : Program} {emit : EsmSplit.Module → Module}
    (valid : EsmSplit.Valid B S P) (projection : IndexedPlanCells.Projection body B P program emit)
    (footprints : IndexedExportCells.Footprints body B) (complete : IndexedPlanCells.DefinitionsPresent body B)
    {frames : List (Nat × List (Name × Addr))} (frameNames : FramesOf body frames)
    {source : Module} (sourceBody : source.body = body) {row : EsmSplit.LocalExportUnit}
    (sourceRow : InteriorExportNames.SourceRow source row)
    (rowFootprint : row.FootprintMatches (B.stmt row.ordinal))
    {owner : EsmSplit.Module} (ownerIn : owner ∈ P.mods) (rowWithin : row.ordinal < B.length)
    (authored : S.claimants B row.ordinal = [owner.name]) (unitsUnique : owner.stmts.Nodup)
    (glue : owner.exports = [])
    (onlyRow : ∀ id ∈ owner.stmts, id ≠ row.ordinal → (B.stmt id).exports = [])
    (sourceDefs : ∀ pair ∈ row.names, ∃ id, id < B.length ∧ pair.2 ∈ (B.stmt id).defs) :
    ∃ cells, row.ordinal ∈ owner.stmts ∧
      (emit owner).exports = source.exports ∧ program.imported frames (emit owner).imports = .ok cells ∧
      ∀ pair ∈ row.names, ∃ a, (source.scope frames).lookup pair.2 = some a ∧
        ((emit owner).scope frames ++ cells).lookup pair.2 = some a := by
  have ⟨owned, _⟩ := InteriorExportNames.row_owner sourceRow valid.2.2.1 ownerIn rowWithin authored
  obtain ⟨cells, imported, agree⟩ :=
    IndexedPlanCells.module_scope_cells valid projection footprints complete frameNames sourceBody ownerIn
  refine ⟨cells, owned, ?_, imported, ?_⟩
  · rw [projection.exports, InteriorExportNames.exact_public_table unitsUnique owned glue onlyRow rowFootprint,
      sourceRow.exports]
  · intro pair member
    obtain ⟨id, within, declared⟩ := sourceDefs pair member
    obtain ⟨declarationOwner, declarationOwnerIn, declOwned⟩ := declaration_owner valid within
    have binding := InteriorExportNames.row_pair_binding valid ownerIn declarationOwnerIn owned rowFootprint
      declOwned member declared
    have bound : (∃ j ∈ owner.stmts, pair.2 ∈ (B.stmt j).defs) ∨ pair.2 ∈ owner.imports.flatMap (·.2) := by
      rcases binding.2.2.2 with hlocal | ⟨edge, edgeIn, inEdge, _⟩
      · exact Or.inl hlocal
      · exact Or.inr (List.mem_flatMap.mpr ⟨edge, edgeIn, inEdge⟩)
    obtain ⟨stmt, inSource⟩ := complete id within pair.2 declared
    have sourceUnit : (id, stmt) ∈ source.body := by rw [sourceBody]; exact lookup_mem inSource
    obtain ⟨a, original, _⟩ := IndexedExportCells.source_and_module_cell footprints frameNames valid.1
      sourceBody inSource sourceUnit declared
    exact ⟨a, original, (agree pair.2 bound).trans original⟩

#print axioms declaration_owner
#print axioms row_scope_cells

end CoreJS.InteriorPlanExports
