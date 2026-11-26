import EsmSplit.CoreJS.MetadataCells
import EsmSplit.CoreJS.TerminalEntryNames

/-! Plan-bound entry exports resolve to source live cells before execution.
The source declarations, full valid plan and executable footprints are input
facts; no candidate execution or pairwise comparison is used. -/

namespace CoreJS.TerminalEntryCells

/-- Every original row local has a source declaration under its authored
owner. The declaration ID is an executable original source ID. -/
def DeclaredRows (ss : List Stmt) (B : EsmSplit.Bundle) (P : EsmSplit.Plan)
    (row : EsmSplit.LocalExportUnit) : Prop :=
  ∀ pair ∈ row.names, ∃ owner ∈ P.mods, ∃ j ∈ owner.stmts,
    j < ss.length ∧ pair.2 ∈ (B.stmt j).defs

/-- Executable source declarations have owners in any valid full plan. This
requires only the source declaration search, not a second owner search. -/
theorem declared_rows_of_source {ss : List Stmt} {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (valid : EsmSplit.Valid B S P) (footprints : MetadataCells.Footprints ss B)
    {row : EsmSplit.LocalExportUnit}
    (sourceDefs : ∀ pair ∈ row.names, ∃ j, j < ss.length ∧ pair.2 ∈ (B.stmt j).defs) :
    DeclaredRows ss B P row := by
  intro pair member
  obtain ⟨j, within, declared⟩ := sourceDefs pair member
  have jBundle : j < B.length := Nat.lt_of_lt_of_le within footprints.withinBundle
  obtain ⟨-, -, -, -, -, -, -, ⟨-, -, all⟩, -⟩ := valid
  have planned : j ∈ P.execOrder := all j (List.mem_range.2 jBundle)
  simp only [EsmSplit.Plan.execOrder, List.mem_flatMap] at planned
  obtain ⟨name, _, hasUnit⟩ := planned
  cases found : P.find name with
  | none => simp [found] at hasUnit
  | some owner =>
    simp only [found, Option.map_some, Option.getD_some] at hasUnit
    exact ⟨owner, List.mem_of_find?_eq_some found, j, hasUnit, within, declared⟩

/-- The actual import construction succeeds, and every original export local
resolves in the constructed entry scope to the same present source address. -/
theorem entry_cells {ss : List Stmt} {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (valid : EsmSplit.Valid B S P) (footprints : MetadataCells.Footprints ss B)
    {frames : List (Nat × List (Name × Addr))} (hF : FramesOf ((List.range ss.length).zip ss) frames)
    {entry : EsmSplit.Module} (entryIn : entry ∈ P.mods) {row : EsmSplit.LocalExportUnit}
    (owned : row.ordinal ∈ entry.stmts) (rowFootprint : row.FootprintMatches (B.stmt row.ordinal))
    (sourceDefs : DeclaredRows ss B P row) :
    ∃ cells, (splitProgram ss B P).imported frames (moduleOf ss B P entry).imports = .ok cells ∧
      ∀ pair ∈ row.names, ∃ a, (bundleEnv ss frames).vars.lookup pair.2 = some a ∧
        ((moduleOf ss B P entry).scope frames ++ cells).lookup pair.2 = some a := by
  obtain ⟨cells, imported, agree⟩ := MetadataCells.module_scope_cells valid footprints hF entryIn
  refine ⟨cells, imported, ?_⟩
  intro pair member
  obtain ⟨owner, ownerIn, j, jOwned, within, declared⟩ := sourceDefs pair member
  obtain ⟨a, sourceCell⟩ := MetadataCells.bundle_decl_cell footprints valid.1 hF within declared
  have bound := TerminalEntryNames.bound_to_original_owner valid entryIn ownerIn owned rowFootprint jOwned member declared
  have admission : (∃ j ∈ entry.stmts, pair.2 ∈ (B.stmt j).defs) ∨ pair.2 ∈ entry.imports.flatMap (·.2) := by
    rcases bound.2.2.2 with hlocal | ⟨edge, edgeIn, inEdge, _⟩
    · exact Or.inl hlocal
    · exact Or.inr (List.mem_flatMap.mpr ⟨edge, edgeIn, inEdge⟩)
  exact ⟨a, sourceCell, (agree pair.2 admission).trans sourceCell⟩

#print axioms declared_rows_of_source
#print axioms entry_cells

end CoreJS.TerminalEntryCells
