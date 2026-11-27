import EsmSplit.CoreJS.IndexedUnitsAgree

/-! Source and actual indexed plan scopes agree after sorting original unit
identities for hoist initialization. This says nothing about eager order. -/

namespace CoreJS.IndexedSortedScopes

/-- Valid full-plan coverage and actual executable footprints already imply
that projecting away metadata retains every executable source ID exactly once. -/
theorem executable_coverage {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} (valid : EsmSplit.Valid B S P)
    (footprints : IndexedExportCells.Footprints body B) :
    (SparseProjectedOrder.executableIds body P).Perm (body.map Prod.fst) := by
  obtain ⟨_, _, _, _, _, _, _, permutes, _⟩ := valid
  have unique : (SparseProjectedOrder.executableIds body P).Nodup := by
    exact permutes.1.filter _
  apply (List.perm_ext_iff_of_nodup unique footprints.uniqueIds).2
  intro id
  constructor
  · intro member
    have selected := (List.mem_filter.mp member).2
    have existsRow : ∃ stmt, body.lookup id = some stmt := by
      apply Option.isSome_iff_exists.mp
      simpa [SparseProjectedOrder.executableIds, indexedNumbered] using selected
    obtain ⟨stmt, found⟩ := existsRow
    exact List.mem_map.mpr ⟨(id, stmt), lookup_mem found, rfl⟩
  · intro member
    obtain ⟨⟨ordinal, stmt⟩, inBody, same⟩ := List.mem_map.mp member
    simp only at same
    subst ordinal
    have found := IndexedExportCells.body_lookup footprints inBody
    have within := (footprints.statement id stmt found).1
    have inPlan := permutes.2.2 id (List.mem_range.mpr within)
    apply List.mem_filter.mpr
    exact ⟨inPlan, by simp [indexedNumbered, found]⟩

/-- The same real source and plan use identical declaration allocation
order and region extent, even if their eager execution orders differ. -/
theorem indexed_tops_regions {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} (valid : EsmSplit.Valid B S P)
    (footprints : IndexedExportCells.Footprints body B)
    {sourceProgram : Program} {source : Module} (sourceBody : source.body = body)
    (singleton : sourceProgram.modules = [source]) (sourceImports : source.imports = [])
    (sourceFind : sourceProgram.find sourceProgram.entry = some source)
    (ordered : body.Pairwise (fun a b => a.1 ≤ b.1)) :
    sourceProgram.tops = (splitProgramIndexed body B P).tops ∧
      sourceProgram.regions = (splitProgramIndexed body B P).regions := by
  have covered := executable_coverage valid footprints
  have sourceOrdered : source.body.Pairwise (fun a b => a.1 ≤ b.1) := by rw [sourceBody]; exact ordered
  constructor
  · rw [IndexedEntryLink.import_free_source_tops sourceFind sourceImports sourceOrdered, sourceBody,
      SparseProjectedOrder.split_indexed_tops_of_perm footprints.uniqueIds covered ordered]
  · rw [IndexedEntryLink.singleton_source_regions singleton, sourceBody,
      SparseProjectedOrder.split_indexed_regions_of_perm footprints.uniqueIds covered]

/-- A valid indexed plan with exactly the source executable IDs has the same
identity-sorted unit scopes as the import-free original. The actual plan may
order its modules differently, including a cycle through the entry. -/
theorem indexed_sorted_scopes {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} (valid : EsmSplit.Valid B S P)
    (footprints : IndexedExportCells.Footprints body B) (free : IndexedUnitsAgree.FreeFootprints body B)
    (complete : IndexedPlanCells.DefinitionsPresent body B)
    {sourceProgram : Program} {source : Module} (sourceBody : source.body = body)
    (sourceImports : source.imports = []) (sourceFind : sourceProgram.find sourceProgram.entry = some source)
    (ordered : body.Pairwise (fun a b => a.1 ≤ b.1)) :
    ∀ frames, FramesOf sourceProgram.tops frames → ∃ before after,
      sourceProgram.scopes frames sourceProgram.loaded = .ok before ∧
      (splitProgramIndexed body B P).scopes frames (splitProgramIndexed body B P).loaded = .ok after ∧
      SameUnits ((unitsOf before).mergeSort fun a b => decide (a.id ≤ b.id))
        ((unitsOf after).mergeSort fun a b => decide (a.id ≤ b.id)) := by
  intro frames frameNames
  have covered := executable_coverage valid footprints
  have sourceOrdered : source.body.Pairwise (fun a b => a.1 ≤ b.1) := by rw [sourceBody]; exact ordered
  have sourceTops : sourceProgram.tops = body :=
    (IndexedEntryLink.import_free_source_tops sourceFind sourceImports sourceOrdered).trans sourceBody
  rw [sourceTops] at frameNames
  obtain ⟨targetLinked, targetScopes, targetKeys, targetAgree⟩ :=
    IndexedUnitsAgree.indexed_units_agree valid footprints free complete frameNames sourceBody
  let sourceEnv : Env := { vars := source.scope frames, this := .undef }
  let sourceLinked : List (Module × Env) := [(source, sourceEnv)]
  have sourceScopes : sourceProgram.scopes frames sourceProgram.loaded = .ok sourceLinked :=
    IndexedEntryLink.import_free_entry_scopes sourceFind sourceImports frames
  have sourceKeys : (unitsOf sourceLinked).map (fun u => (u.1, u.2.1)) = body := by
    rw [unitsOf_keys]
    simp [sourceLinked, sourceBody]
  have sourceEnv_eq : ∀ u ∈ unitsOf sourceLinked, u.2.2 = sourceEnv := by
    intro u member
    obtain ⟨p, hp, _, env⟩ := mem_unitsOf member
    have same : p = (source, sourceEnv) := by simpa [sourceLinked] using hp
    subst p
    exact env
  have targetTops : (splitProgramIndexed body B P).tops = body :=
    SparseProjectedOrder.split_indexed_tops_of_perm footprints.uniqueIds covered ordered
  have sortedSame : SameUnits
      ((unitsOf sourceLinked).mergeSort fun a b => decide (a.id ≤ b.id))
      ((unitsOf targetLinked).mergeSort fun a b => decide (a.id ≤ b.id)) := by
    refine sameUnits_of ?_ fun u hu u' hu' _ sameStmt => ?_
    · rw [List.map_mergeSort (f := fun u : LinkedUnit => (u.1, u.2.1))
          (s := fun a b : Nat × Stmt => decide (a.1 ≤ b.1)) (fun _ _ _ _ => rfl),
        sourceKeys,
        List.map_mergeSort (f := fun u : LinkedUnit => (u.1, u.2.1))
          (s := fun a b : Nat × Stmt => decide (a.1 ≤ b.1)) (fun _ _ _ _ => rfl),
        targetKeys, List.mergeSort_of_pairwise (ordered.imp fun h => by simpa using h)]
      exact targetTops.symm
    · rw [sourceEnv_eq u ((List.mergeSort_perm _ _).mem_iff.1 hu), sameStmt]
      exact targetAgree u' ((List.mergeSort_perm _ _).mem_iff.1 hu')
  exact ⟨sourceLinked, targetLinked, sourceScopes, targetScopes, sortedSame⟩

#print axioms executable_coverage
#print axioms indexed_tops_regions
#print axioms indexed_sorted_scopes

end CoreJS.IndexedSortedScopes
