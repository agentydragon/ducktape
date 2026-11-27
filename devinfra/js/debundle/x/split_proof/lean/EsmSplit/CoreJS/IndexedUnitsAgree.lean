import EsmSplit.CoreJS.IndexedEntryLink

/-! Original-ID statement scope agreement for the actual indexed constructor.
The source-side free-name footprint is stated over CoreJS ASTs. -/

namespace CoreJS.IndexedUnitsAgree

/-- A source statement's free names are among its original declaration and
its planner-visible eager or lazy uses. -/
def FreeFootprints (body : List (Nat × Stmt)) (B : EsmSplit.Bundle) : Prop :=
  ∀ id stmt, body.lookup id = some stmt → ∀ name ∈ stmt.free,
    name ∈ (B.stmt id).defs ∨ name ∈ (B.stmt id).eager ++ (B.stmt id).lazy

/-- Every actual indexed import resolves to the corresponding original scope
address; names absent from the plan imports are absent from the import frame. -/
theorem imported_exact {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} (valid : EsmSplit.Valid B S P)
    (footprints : IndexedExportCells.Footprints body B) (complete : IndexedPlanCells.DefinitionsPresent body B)
    {frames : List (Nat × List (Name × Addr))} (frameNames : FramesOf body frames)
    {source : Module} (sourceBody : source.body = body) {md : EsmSplit.Module} (moduleIn : md ∈ P.mods) :
    ∃ cells, (splitProgramIndexed body B P).imported frames (moduleOfIndexed body B P md).imports = .ok cells ∧
      ∀ name, cells.lookup name = if name ∈ md.imports.flatMap (·.2) then (source.scope frames).lookup name else none := by
  have projection := IndexedConstructorCells.projection body B P
  rw [projection.imports]
  apply imported_ok (splitProgramIndexed body B P) frames ((source.scope frames).lookup ·)
    (fun src u => EsmSplit.exportNameFor (P.namedExports B src) u) md.imports
  intro edge edgeIn
  refine ⟨?_, ?_⟩
  · rw [SparseProjectedOrder.split_indexed_find, Option.isSome_map]
    exact ((valid.2.2.2.1 md moduleIn).1 edge edgeIn).2.1
  · intro name nameIn
    obtain ⟨_, _, a, _, _, _, _, cell, original⟩ :=
      IndexedPlanCells.import_cell valid projection footprints complete frameNames sourceBody moduleIn edgeIn nameIn
    exact ⟨a, cell, original⟩

/-- Each original statement, when placed in its authored module, has the same
CoreJS scope on every free name as the source, including unbound globals. -/
theorem module_statement_agree {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} (valid : EsmSplit.Valid B S P)
    (footprints : IndexedExportCells.Footprints body B) (freeFootprints : FreeFootprints body B)
    (complete : IndexedPlanCells.DefinitionsPresent body B)
    {frames : List (Nat × List (Name × Addr))} (frameNames : FramesOf body frames)
    {source : Module} (sourceBody : source.body = body) {md : EsmSplit.Module} (moduleIn : md ∈ P.mods)
    {id : Nat} {stmt : Stmt} (owned : id ∈ md.stmts) (original : body.lookup id = some stmt) :
    ∃ cells, (splitProgramIndexed body B P).imported frames (moduleOfIndexed body B P md).imports = .ok cells ∧
      Agree stmt.free { vars := source.scope frames, this := .undef }
        { vars := (moduleOfIndexed body B P md).scope frames ++ cells, this := .undef } := by
  have projection := IndexedConstructorCells.projection body B P
  obtain ⟨cells, imported, lookup⟩ := imported_exact valid footprints complete frameNames sourceBody moduleIn
  obtain ⟨otherCells, importedAgain, boundAgree⟩ :=
    IndexedPlanCells.module_scope_cells valid projection footprints complete frameNames sourceBody moduleIn
  have sameCells : otherCells = cells := by rw [imported] at importedAgain; cases importedAgain; rfl
  subst otherCells
  refine ⟨cells, imported, ⟨rfl, rfl, ?_⟩⟩
  intro name freeName
  by_cases bound : (∃ j ∈ md.stmts, name ∈ (B.stmt j).defs) ∨ name ∈ md.imports.flatMap (·.2)
  · exact (boundAgree name bound).symm
  · have noDecl : ∀ j ∈ md.stmts, name ∉ (B.stmt j).defs := fun j hj hn => bound (Or.inl ⟨j, hj, hn⟩)
    have noImport : name ∉ md.imports.flatMap (·.2) := fun h => bound (Or.inr h)
    have noModule : ((moduleOfIndexed body B P md).scope frames).lookup name = none := by
      apply IndexedExportCells.scope_lookup_none footprints frameNames
      intro p hp hn
      exact noDecl p.1 ((projection.body md p.1 p.2).1 hp).1 hn
    have noSource : (source.scope frames).lookup name = none := by
      apply IndexedExportCells.scope_lookup_none footprints frameNames
      intro p hp hdecl
      have inSource : p ∈ body := by simpa [sourceBody] using hp
      have found := IndexedExportCells.body_lookup footprints inSource
      have within := (footprints.statement p.1 p.2 found).1
      have mentioned : name ∈ (B.stmt id).eager ++ (B.stmt id).lazy :=
        (freeFootprints id stmt original name freeName).resolve_left (noDecl id owned)
      have hygienic := valid.2.2.2.2.2.1 md moduleIn id owned name mentioned
      have noLocal : md.declares B name = false := Bool.eq_false_iff.2 fun h => by
        obtain ⟨j, hj, hn⟩ := List.any_eq_true.1 h
        exact noDecl j hj (List.contains_iff_mem.1 hn)
      have noEdge : md.imports.find? (·.2.contains name) = none := List.find?_eq_none.2 fun e he hn =>
        noImport (List.mem_flatMap.2 ⟨e, he, List.contains_iff_mem.1 hn⟩)
      simp only [EsmSplit.Plan.resolve, noLocal, noEdge, Bool.false_eq_true, ↓reduceIte] at hygienic
      obtain ⟨owner, ownerIn, hasId⟩ := InteriorPlanExports.declaration_owner valid within
      have ownerDeclares : owner.declares B name = true :=
        List.any_eq_true.2 ⟨p.1, hasId, List.contains_iff_mem.2 hdecl⟩
      have someOwner : (P.mods.find? (·.declares B name)).isSome = true :=
        List.find?_isSome.2 ⟨owner, ownerIn, ownerDeclares⟩
      unfold EsmSplit.Plan.declaredIn at hygienic
      cases foundOwner : P.mods.find? (·.declares B name) with
      | none => rw [foundOwner] at someOwner; cases someOwner
      | some _ => rw [foundOwner] at hygienic; cases hygienic
    change (source.scope frames).lookup name =
      ((moduleOfIndexed body B P md).scope frames ++ cells).lookup name
    rw [List.lookup_append, noModule, Option.none_or, lookup]
    simp [noImport, noSource]

/-- The actual indexed linker constructs all loaded scopes, retains their
original statement identities, and agrees with the source environment on
every linked statement's free names. -/
theorem indexed_units_agree {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} (valid : EsmSplit.Valid B S P)
    (footprints : IndexedExportCells.Footprints body B) (freeFootprints : FreeFootprints body B)
    (complete : IndexedPlanCells.DefinitionsPresent body B)
    {frames : List (Nat × List (Name × Addr))} (frameNames : FramesOf body frames)
    {source : Module} (sourceBody : source.body = body) :
    ∃ linked, (splitProgramIndexed body B P).scopes frames (splitProgramIndexed body B P).loaded = .ok linked ∧
      (unitsOf linked).map (fun u => (u.1, u.2.1)) =
        (splitProgramIndexed body B P).loaded.flatMap (·.body) ∧
      ∀ u ∈ unitsOf linked, Agree u.stmt.free { vars := source.scope frames, this := .undef } u.env := by
  let program := splitProgramIndexed body B P
  let emit := moduleOfIndexed body B P
  have projection := IndexedConstructorCells.projection body B P
  obtain ⟨linked, success, modules, each⟩ := scopes_ok program frames
    (fun m cells => ∃ md ∈ P.mods, m = emit md ∧ ∀ id stmt,
      id ∈ md.stmts → body.lookup id = some stmt →
      Agree stmt.free { vars := source.scope frames, this := .undef }
        { vars := (emit md).scope frames ++ cells, this := .undef }) program.loaded (by
      intro m member
      obtain ⟨path, _, found⟩ := List.mem_filterMap.1 member
      rw [projection.find] at found
      obtain ⟨md, hfind, rfl⟩ := Option.map_eq_some_iff.1 found
      have mdIn : md ∈ P.mods := List.mem_of_find?_eq_some hfind
      obtain ⟨cells, imported, _⟩ := imported_exact valid footprints complete frameNames sourceBody mdIn
      refine ⟨cells, imported, md, mdIn, rfl, ?_⟩
      intro id stmt owned original
      obtain ⟨other, otherImported, agree⟩ :=
        module_statement_agree valid footprints freeFootprints complete frameNames sourceBody mdIn owned original
      have same : other = cells := by rw [imported] at otherImported; cases otherImported; rfl
      subst other
      exact agree)
  refine ⟨linked, success, ?_, ?_⟩
  · rw [unitsOf_keys]
    have flat : linked.flatMap (·.1.body) = (linked.map Prod.fst).flatMap (·.body) := by rw [List.flatMap_map]
    rw [flat, modules]
  · intro unit member
    obtain ⟨pair, pairIn, inBody, env⟩ := mem_unitsOf member
    obtain ⟨cells, ⟨md, mdIn, moduleEq, agree⟩, envEq⟩ := each pair pairIn
    change Agree unit.2.1.free { vars := source.scope frames, this := .undef } unit.2.2
    rw [env, envEq, moduleEq]
    rw [moduleEq] at inBody
    have original := (projection.body md unit.1 unit.2.1).1 inBody
    exact agree unit.1 unit.2.1 original.1 original.2

/-- Source/plan facts and original-row order give the existing CoreJS
`ScopesAgree` interface. `rows` is a structural construction fact about the
indexed program's loaded original rows, suitable for the sparse order theorem. -/
theorem indexed_scopes_agree {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} (valid : EsmSplit.Valid B S P)
    (footprints : IndexedExportCells.Footprints body B) (freeFootprints : FreeFootprints body B)
    (complete : IndexedPlanCells.DefinitionsPresent body B)
    {sourceProgram : Program} {source : Module} (sourceBody : source.body = body)
    (sourceImports : source.imports = []) (sourceFind : sourceProgram.find sourceProgram.entry = some source)
    (ordered : body.Pairwise (fun a b => a.1 ≤ b.1))
    (rows : (splitProgramIndexed body B P).loaded.flatMap (·.body) = body) :
    ScopesAgree sourceProgram (splitProgramIndexed body B P) := by
  intro frames frameNames
  have sourceOrdered : source.body.Pairwise (fun a b => a.1 ≤ b.1) := by rw [sourceBody]; exact ordered
  have sourceTops : sourceProgram.tops = body :=
    (IndexedEntryLink.import_free_source_tops sourceFind sourceImports sourceOrdered).trans sourceBody
  rw [sourceTops] at frameNames
  obtain ⟨targetLinked, targetScopes, targetKeys, targetAgree⟩ :=
    indexed_units_agree valid footprints freeFootprints complete frameNames sourceBody
  have sourceKeys : (unitsOf [(source, { vars := source.scope frames, this := .undef })]).map
      (fun u => (u.1, u.2.1)) = body := by
    rw [unitsOf_keys]
    simp [sourceBody]
  refine ⟨[(source, { vars := source.scope frames, this := .undef })], targetLinked,
    IndexedEntryLink.import_free_entry_scopes sourceFind sourceImports frames, targetScopes, ?_, ?_⟩
  · refine sameUnits_of ?_ fun u hu u' hu' _ sameStmt => ?_
    · rw [sourceKeys, targetKeys, rows]
    · obtain ⟨p, hp, _, env⟩ := mem_unitsOf hu
      rw [List.mem_singleton] at hp
      subst hp
      rw [env, sameStmt]
      exact targetAgree u' hu'
  · have sorted := ordered
    rw [← sourceKeys] at sorted
    exact (List.pairwise_map (R := fun a b => a.1 ≤ b.1) (f := fun u : LinkedUnit => (u.1, u.2.1))).1 sorted

/-- The existing CoreJS execution theorem applies to indexed construction:
for every world and fuel, the source and candidate have identical outcomes,
provided the original source/plan conditions and structural row/region facts
hold. No pair of executions is inspected to establish these premises. -/
theorem indexed_run_same {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} (valid : EsmSplit.Valid B S P)
    (footprints : IndexedExportCells.Footprints body B) (freeFootprints : FreeFootprints body B)
    (complete : IndexedPlanCells.DefinitionsPresent body B)
    {sourceProgram : Program} {source : Module} (sourceBody : source.body = body)
    (sourceImports : source.imports = []) (sourceFind : sourceProgram.find sourceProgram.entry = some source)
    (ordered : body.Pairwise (fun a b => a.1 ≤ b.1))
    (rows : (splitProgramIndexed body B P).loaded.flatMap (·.body) = body)
    (candidateTops : (splitProgramIndexed body B P).tops = body)
    (candidateRegions : (splitProgramIndexed body B P).regions = sourceProgram.regions) :
    ∀ (w : World) (fuel : Nat), runProgram w fuel sourceProgram = runProgram w fuel (splitProgramIndexed body B P) := by
  have sourceOrdered : source.body.Pairwise (fun a b => a.1 ≤ b.1) := by rw [sourceBody]; exact ordered
  have sourceTops : sourceProgram.tops = body :=
    (IndexedEntryLink.import_free_source_tops sourceFind sourceImports sourceOrdered).trans sourceBody
  have scopes := indexed_scopes_agree valid footprints freeFootprints complete sourceBody sourceImports sourceFind ordered rows
  intro w fuel
  exact run_same (W := w) candidateRegions.symm (sourceTops.trans candidateTops.symm) scopes fuel

/-- The sparse schedule and singleton source determine the constructor's
rows, allocation order, and region count. They are not execution checks. -/
theorem indexed_run_same_of_order_kept {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} (valid : EsmSplit.Valid B S P)
    (footprints : IndexedExportCells.Footprints body B) (freeFootprints : FreeFootprints body B)
    (complete : IndexedPlanCells.DefinitionsPresent body B)
    {sourceProgram : Program} {source : Module} (sourceBody : source.body = body)
    (sourceImports : source.imports = []) (sourceFind : sourceProgram.find sourceProgram.entry = some source)
    (singleton : sourceProgram.modules = [source])
    (ordered : body.Pairwise (fun a b => a.1 ≤ b.1)) (kept : SparseProjectedOrder.OrderKept body P) :
    ∀ (w : World) (fuel : Nat), runProgram w fuel sourceProgram = runProgram w fuel (splitProgramIndexed body B P) := by
  apply indexed_run_same valid footprints freeFootprints complete sourceBody sourceImports sourceFind ordered
    (SparseProjectedOrder.split_indexed_loaded_eq_source kept)
    (SparseProjectedOrder.split_indexed_tops_eq_source kept ordered)
  rw [SparseProjectedOrder.split_indexed_regions_eq_source kept,
    IndexedEntryLink.singleton_source_regions singleton, sourceBody]

/-- For the same sparse schedule, the actual link outcomes retain the ordered
public name-to-address table, including an authored local export row. -/
theorem row_link_outcomes_of_order_kept {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} (valid : EsmSplit.Valid B S P)
    (footprints : IndexedExportCells.Footprints body B) (complete : IndexedPlanCells.DefinitionsPresent body B)
    {sourceProgram : Program} {source : Module} (sourceBody : source.body = body)
    (sourceImports : source.imports = []) (sourceFind : sourceProgram.find sourceProgram.entry = some source)
    (singleton : sourceProgram.modules = [source])
    (ordered : body.Pairwise (fun a b => a.1 ≤ b.1)) (kept : SparseProjectedOrder.OrderKept body P)
    (sourceConsts : ∀ p ∈ body, ∃ name init, p.2 = .decl .const name init)
    {row : EsmSplit.LocalExportUnit} (sourceRow : InteriorExportNames.SourceRow source row)
    (rowFootprint : row.FootprintMatches (B.stmt row.ordinal))
    {owner : EsmSplit.Module} (ownerIn : owner ∈ P.mods) (rowWithin : row.ordinal < B.length)
    (planEntry : P.find P.entry = some owner)
    (authored : S.claimants B row.ordinal = [owner.name]) (unitsUnique : owner.stmts.Nodup)
    (glue : owner.exports = [])
    (onlyRow : ∀ id ∈ owner.stmts, id ≠ row.ordinal → (B.stmt id).exports = [])
    (sourceDefs : ∀ pair ∈ row.names, ∃ id, id < B.length ∧ pair.2 ∈ (B.stmt id).defs) :
    ∀ w : World, IndexedEntryLink.EntryLinkOutcomesAgree sourceProgram (splitProgramIndexed body B P) w := by
  have decls : ∀ p ∈ body, ∃ kind name init, p.2 = .decl kind name init := by
    intro p member
    obtain ⟨name, init, shape⟩ := sourceConsts p member
    exact ⟨.const, name, init, shape⟩
  apply IndexedEntryLink.row_link_outcomes_from_plan valid footprints complete sourceBody sourceImports sourceFind
    ordered decls (SparseProjectedOrder.split_indexed_tops_eq_source kept ordered) ?_
    sourceRow rowFootprint ownerIn rowWithin planEntry authored unitsUnique glue onlyRow sourceDefs
  rw [SparseProjectedOrder.split_indexed_regions_eq_source kept,
    IndexedEntryLink.singleton_source_regions singleton, sourceBody]

#print axioms indexed_run_same_of_order_kept
#print axioms row_link_outcomes_of_order_kept
#print axioms indexed_run_same
#print axioms indexed_scopes_agree
#print axioms indexed_units_agree
#print axioms imported_exact
#print axioms module_statement_agree

end CoreJS.IndexedUnitsAgree
