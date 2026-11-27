import EsmSplit.CoreJS.ExportRows
import EsmSplit.CoreJS.IndexedEntryLink
import EsmSplit.PlanGlue

/-! Link the actual indexed constructor's complete public surface from
source-indexed local-export rows and the authored plan. -/

namespace CoreJS.ExportRows

/-- Dense executable IDs use the same CoreJS module as the original terminal
constructor; metadata remains at its full-bundle ordinal. -/
theorem indexed_module_dense (ss : List Stmt) (B : EsmSplit.Bundle) (P : EsmSplit.Plan)
    (md : EsmSplit.Module) :
    moduleOfIndexed ((List.range ss.length).zip ss) B P md = moduleOf ss B P md := by
  have same : indexedNumbered ((List.range ss.length).zip ss) =
      (fun id => (ss[id]?).map (id, ·)) := by
    funext id
    simp [indexedNumbered, lookup_zip_range]
  simp [moduleOfIndexed, moduleOf, same]

/-- The same list-indexed graph is exactly the existing dense graph. -/
theorem indexed_program_dense (ss : List Stmt) (B : EsmSplit.Bundle) (P : EsmSplit.Plan) :
    splitProgramIndexed ((List.range ss.length).zip ss) B P = splitProgram ss B P := by
  simp [splitProgramIndexed, splitProgram, indexed_module_dense]

/-- A successful lookup identifies both the original row and its ordinal. -/
theorem rowAt_some {rows : List EsmSplit.LocalExportUnit} {id : Nat} {row : EsmSplit.LocalExportUnit}
    (found : rowAt rows id = some row) : row ∈ rows ∧ row.ordinal = id := by
  have member := lookup_mem found
  obtain ⟨other, inRows, same⟩ := List.mem_map.1 member
  cases same
  exact ⟨inRows, rfl⟩

/-- Each source row selected in the authored entry is an actual plan unit and
its planner footprint is the one for its original ordinal. -/
theorem selected_row {B : EsmSplit.Bundle} {owner : EsmSplit.Module}
    {rows : List EsmSplit.LocalExportUnit} (unitMatches : ∀ id ∈ owner.stmts, UnitMatches B rows id)
    (orderedRows : owner.stmts.filterMap (rowAt rows) = rows)
    {row : EsmSplit.LocalExportUnit} (member : row ∈ rows) :
    row.ordinal ∈ owner.stmts ∧ row.FootprintMatches (B.stmt row.ordinal) := by
  have selected : row ∈ owner.stmts.filterMap (rowAt rows) := by rw [orderedRows]; exact member
  obtain ⟨id, owned, found⟩ := List.mem_filterMap.1 selected
  have same := (rowAt_some found).2
  subst id
  have matched := unitMatches row.ordinal owned
  simp only [UnitMatches, found] at matched
  exact ⟨owned, matched⟩

/-- Every public local has the original allocated cell in the actual entry
scope, with each metadata row retaining its authored owner. All table equality
comes from the planner's row fold and exact source coverage. -/
theorem rows_scope_cells {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} (valid : EsmSplit.Valid B S P)
    (footprints : IndexedExportCells.Footprints body B) (complete : IndexedPlanCells.DefinitionsPresent body B)
    {s s' : State W} {frames : List (Nat × List (Name × Addr))}
    (allocated : declareTops body s = .ok frames s')
    {source : Module} (sourceBody : source.body = body) {rows : List EsmSplit.LocalExportUnit}
    (sourceRows : SourceRows source rows) {owner : EsmSplit.Module} (ownerIn : owner ∈ P.mods)
    (unitMatches : ∀ id ∈ owner.stmts, UnitMatches B rows id)
    (orderedRows : owner.stmts.filterMap (rowAt rows) = rows) (noGlue : owner.exports = [])
    (sourceDefs : ∀ pair ∈ rows.flatMap (·.names), ∃ id, id < B.length ∧ pair.2 ∈ (B.stmt id).defs) :
    ∃ cells, (moduleOfIndexed body B P owner).exports = source.exports ∧
      (splitProgramIndexed body B P).imported frames (moduleOfIndexed body B P owner).imports = .ok cells ∧
      (∀ row ∈ rows, row.ordinal ∈ owner.stmts ∧ S.claimants B row.ordinal = [owner.name]) ∧
      ∀ pair ∈ source.exports, ∃ a b, (source.scope frames).lookup pair.2 = some a ∧
        ((moduleOfIndexed body B P owner).scope frames ++ cells).lookup pair.2 = some a ∧
        s'.cellAt a = some b := by
  have projection := IndexedConstructorCells.projection body B P
  obtain ⟨cells, imported, agree⟩ := IndexedPlanCells.module_scope_cells valid projection footprints complete
    (declareTops_names body allocated) sourceBody ownerIn
  refine ⟨cells, ?_, imported, ?_, ?_⟩
  · rw [projection.exports, exact_entry_exports unitMatches orderedRows noGlue, sourceRows.exports]
  · intro row member
    obtain ⟨owned, _⟩ := selected_row unitMatches orderedRows member
    have within := valid.2.2.1.2.2.2.2.2.1 owner ownerIn row.ordinal owned
    exact ⟨owned, (valid.2.2.1.2.2.2.2.1 owner ownerIn row.ordinal (List.mem_range.2 within)).1 owned⟩
  · intro pair member
    have inRows : pair ∈ rows.flatMap (·.names) := by rw [← sourceRows.exports]; exact member
    obtain ⟨row, rowIn, pairIn⟩ := List.mem_flatMap.1 inRows
    obtain ⟨owned, rowFootprint⟩ := selected_row unitMatches orderedRows rowIn
    obtain ⟨id, within, declared⟩ := sourceDefs pair inRows
    obtain ⟨declarationOwner, declarationOwnerIn, declOwned⟩ := InteriorPlanExports.declaration_owner valid within
    have binding := InteriorExportNames.row_pair_binding valid ownerIn declarationOwnerIn owned rowFootprint
      declOwned pairIn declared
    have bound : (∃ j ∈ owner.stmts, pair.2 ∈ (B.stmt j).defs) ∨ pair.2 ∈ owner.imports.flatMap (·.2) := by
      rcases binding.2.2.2 with hlocal | ⟨edge, edgeIn, inEdge, _⟩
      · exact Or.inl hlocal
      · exact Or.inr (List.mem_flatMap.2 ⟨edge, edgeIn, inEdge⟩)
    obtain ⟨stmt, inSource⟩ := complete id within pair.2 declared
    have sourceUnit : (id, stmt) ∈ source.body := by rw [sourceBody]; exact lookup_mem inSource
    obtain ⟨a, original, _⟩ := IndexedExportCells.source_and_module_cell footprints
      (declareTops_names body allocated) valid.1 sourceBody inSource sourceUnit declared
    obtain ⟨b, present⟩ := IndexedAllocatedCells.scope_cell_present allocated original
    exact ⟨a, b, original, (agree pair.2 bound).trans original, present⟩

/-- The actual source and indexed plan scopes resolve the same complete,
ordered public-name/address table. The hypotheses inspect the source rows and
authored plan, not the result of resolving either table. -/
theorem rows_resolved_from_plan {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} (valid : EsmSplit.Valid B S P)
    (footprints : IndexedExportCells.Footprints body B) (complete : IndexedPlanCells.DefinitionsPresent body B)
    {s s' : State W} {frames : List (Nat × List (Name × Addr))}
    (allocated : declareTops body s = .ok frames s')
    {sourceProgram : Program} {source : Module} (sourceBody : source.body = body)
    (sourceImports : source.imports = []) (sourceFind : sourceProgram.find sourceProgram.entry = some source)
    {rows : List EsmSplit.LocalExportUnit} (sourceRows : SourceRows source rows)
    {owner : EsmSplit.Module} (planEntry : P.find P.entry = some owner)
    (unitMatches : ∀ id ∈ owner.stmts, UnitMatches B rows id)
    (orderedRows : owner.stmts.filterMap (rowAt rows) = rows) (noGlue : owner.exports = [])
    (sourceDefs : ∀ pair ∈ rows.flatMap (·.names), ∃ id, id < B.length ∧ pair.2 ∈ (B.stmt id).defs) :
    ∃ targetLinked table,
      (splitProgramIndexed body B P).scopes frames (splitProgramIndexed body B P).loaded = .ok targetLinked ∧
      (∀ row ∈ rows, row.ordinal ∈ owner.stmts ∧ S.claimants B row.ordinal = [owner.name]) ∧
      EntryExports.resolve sourceProgram.entry [(source, { vars := source.scope frames, this := .undef })] = .ok table ∧
      EntryExports.resolve (splitProgramIndexed body B P).entry targetLinked = .ok table ∧
      ∀ pair ∈ source.exports, ∃ a b, (source.scope frames).lookup pair.2 = some a ∧ s'.cellAt a = some b := by
  have ownerIn : owner ∈ P.mods := List.mem_of_find?_eq_some planEntry
  obtain ⟨cells, published, imported, ownership, agree⟩ :=
    rows_scope_cells valid footprints complete allocated sourceBody sourceRows ownerIn unitMatches orderedRows noGlue sourceDefs
  obtain ⟨targetLinked, targetScopes⟩ := IndexedEntryLink.indexed_scopes_success valid footprints complete
    (declareTops_names body allocated) sourceBody
  have targetFind : (splitProgramIndexed body B P).find (splitProgramIndexed body B P).entry =
      some (moduleOfIndexed body B P owner) := by
    change (splitProgramIndexed body B P).find P.entry = some (moduleOfIndexed body B P owner)
    rw [SparseProjectedOrder.split_indexed_find, planEntry]
    rfl
  have sourceScopes := IndexedEntryLink.import_free_entry_scopes sourceFind sourceImports frames
  have sourceEntry := IndexedEntryLink.loaded_entry_find sourceFind
  obtain ⟨sourceCells, sourceImported, sourceFound⟩ := IndexedEntryLink.scopes_find sourceProgram frames
    sourceProgram.entry sourceProgram.loaded [(source, { vars := source.scope frames, this := .undef })]
    source sourceScopes sourceEntry
  rw [sourceImports] at sourceImported
  cases sourceImported
  obtain ⟨targetCells, targetImported, targetFound⟩ := IndexedEntryLink.scopes_find
    (splitProgramIndexed body B P) frames (splitProgramIndexed body B P).entry
    (splitProgramIndexed body B P).loaded targetLinked (moduleOfIndexed body B P owner) targetScopes
    (IndexedEntryLink.loaded_entry_find targetFind)
  have sameCells : targetCells = cells := by rw [imported] at targetImported; cases targetImported; rfl
  subst targetCells
  have envAgree : ∀ pair ∈ source.exports, ∃ a,
      ({ vars := source.scope frames ++ [], this := .undef } : Env).vars.lookup pair.2 = some a ∧
      ({ vars := (moduleOfIndexed body B P owner).scope frames ++ cells, this := .undef } : Env).vars.lookup pair.2 = some a := by
    intro pair member
    obtain ⟨a, _, original, target, _⟩ := agree pair member
    exact ⟨a, by simpa using original, target⟩
  obtain ⟨table, original, target⟩ := IndexedEntryLink.resolved_entries_equal sourceFound targetFound published envAgree
  refine ⟨targetLinked, table, targetScopes, ownership, ?_, target, ?_⟩
  · simpa using original
  · intro pair member
    obtain ⟨a, b, original, _, present⟩ := agree pair member
    exact ⟨a, b, original, present⟩

/-- The actual planner's consumed-name check supplies the common no-glue
premise for terminal and interior rows. Source-row provenance, complete unit
footprints, and the original order of metadata rows remain explicit premises;
no equality of the resulting export tables is assumed. `SourceRows` does not
by itself detect an omitted empty metadata row; the caller must establish
complete source-unit provenance at its input boundary. -/
theorem rows_resolved_from_mkPlan {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    (valid : EsmSplit.Valid B S (EsmSplit.mkPlan B S))
    (footprints : IndexedExportCells.Footprints body B) (complete : IndexedPlanCells.DefinitionsPresent body B)
    {s s' : State W} {frames : List (Nat × List (Name × Addr))}
    (allocated : declareTops body s = .ok frames s')
    {sourceProgram : Program} {source : Module} (sourceBody : source.body = body)
    (sourceImports : source.imports = []) (sourceFind : sourceProgram.find sourceProgram.entry = some source)
    {rows : List EsmSplit.LocalExportUnit} (sourceRows : SourceRows source rows)
    {owner : EsmSplit.Module}
    (planEntry : (EsmSplit.mkPlan B S).find (EsmSplit.mkPlan B S).entry = some owner)
    (unitMatches : ∀ id ∈ owner.stmts, UnitMatches B rows id)
    (orderedRows : owner.stmts.filterMap (rowAt rows) = rows)
    (covered : EsmSplit.PlanGlue.EntryCovered B S owner)
    (sourceDefs : ∀ pair ∈ rows.flatMap (·.names), ∃ id, id < B.length ∧ pair.2 ∈ (B.stmt id).defs) :
    ∃ targetLinked table,
      (splitProgramIndexed body B (EsmSplit.mkPlan B S)).scopes frames
        (splitProgramIndexed body B (EsmSplit.mkPlan B S)).loaded = .ok targetLinked ∧
      (∀ row ∈ rows, row.ordinal ∈ owner.stmts ∧ S.claimants B row.ordinal = [owner.name]) ∧
      EntryExports.resolve sourceProgram.entry [(source, { vars := source.scope frames, this := .undef })] = .ok table ∧
      EntryExports.resolve (splitProgramIndexed body B (EsmSplit.mkPlan B S)).entry targetLinked = .ok table ∧
      ∀ pair ∈ source.exports, ∃ a b, (source.scope frames).lookup pair.2 = some a ∧ s'.cellAt a = some b :=
  rows_resolved_from_plan valid footprints complete allocated sourceBody sourceImports sourceFind sourceRows
    planEntry unitMatches orderedRows (EsmSplit.PlanGlue.mkPlan_no_extra_glue planEntry covered) sourceDefs

#print axioms rowAt_some
#print axioms selected_row
#print axioms rows_scope_cells
#print axioms rows_resolved_from_plan
#print axioms rows_resolved_from_mkPlan
#print axioms indexed_module_dense
#print axioms indexed_program_dense

end CoreJS.ExportRows
