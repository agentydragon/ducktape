import EsmSplit.CoreJS.IndexedConstLink
import EsmSplit.CoreJS.LiteralSwapCheck

/-! Exact schedule steps for const declarations in the existing indexed
constructor. The source, allocation, and authored plan determine the linked
units. The adjacent step is checked over original rows before linking. -/

namespace CoreJS.IndexedReorder

open LiteralSwapCheck

/-- A checked schedule changes the execution order while preserving the
original-ID allocation, source scopes, and complete bounded result. -/
theorem run_of_schedule {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (valid : EsmSplit.Valid B S P) {original : Program} {source : Module}
    (single : original.modules = [source]) (imports : source.imports = [])
    (found : original.find original.entry = some source)
    (footprints : IndexedExportCells.Footprints source.body B)
    (free : IndexedUnitsAgree.FreeFootprints source.body B)
    (complete : IndexedPlanCells.DefinitionsPresent source.body B)
    (ordered : source.body.Pairwise (fun a b => a.1 ≤ b.1))
    (consts : ∀ p ∈ source.body, ∃ name init, p.2 = .decl .const name init)
    {scheduled : List (Nat × Stmt)} (steps : SourceSchedule source.body scheduled)
    (ids : SparseProjectedOrder.executableIds source.body P = scheduled.map Prod.fst)
    (w : World) (fuel : Nat) :
    runProgram w fuel original = runProgram w fuel (splitProgramIndexed source.body B P) := by
  let output := splitProgramIndexed source.body B P
  have unique := footprints.uniqueIds
  have covered : (SparseProjectedOrder.executableIds source.body P).Perm (source.body.map Prod.fst) := by
    rw [ids]
    exact steps.perm.map Prod.fst
  have tops : output.tops = source.body :=
    SparseProjectedOrder.split_indexed_tops_of_perm unique covered ordered
  have regions : output.regions = original.regions := by
    rw [SparseProjectedOrder.split_indexed_regions_of_perm unique covered,
      IndexedEntryLink.singleton_source_regions single]
  obtain ⟨frames, ready, allocated⟩ := IndexedConstLink.source_allocation rfl single consts w
  have frameNames := declareTops_names _ allocated
  let env : Env := { vars := source.scope frames, this := .undef }
  let before : LinkedBehavior.CheckedLink original w :=
    ⟨[(source, env)], ready, IndexedEntryLink.link_after_const_allocation
      (IndexedEntryLink.import_free_source_tops found imports ordered)
      unique allocated (IndexedEntryLink.import_free_entry_scopes found imports frames)
      (IndexedEntryLink.source_const_init consts)⟩
  obtain ⟨linked, scopes, keys, agree⟩ :=
    IndexedUnitsAgree.indexed_units_agree valid footprints free complete frameNames (source := source) rfl
  let after : LinkedBehavior.CheckedLink output w :=
    ⟨linked, ready, by
      rw [regions]
      exact IndexedEntryLink.link_after_const_allocation tops unique allocated scopes
        (IndexedEntryLink.indexed_initFunctions_consts scopes consts)⟩
  have cells : ∀ id x e, (id, .decl .const x e) ∈ source.body →
      ∃ a, env.vars.lookup x = some a := by
    intro id x e member
    have originalRow := IndexedExportCells.body_lookup footprints member
    have defined := ((footprints.statement id _ originalRow).2 x).2 (by simp [Stmt.bindings, Stmt.varNames, varNamesOf, lexDecls])
    obtain ⟨a, binding, _⟩ := IndexedExportCells.source_and_module_cell footprints frameNames
      valid.1 (source := source) (m := source) rfl originalRow member defined
    exact ⟨a, binding⟩
  have moves := steps.linked env cells (fun _ _ _ hx hy => declareTops_scope_injective allocated source hx hy)
  have same : SameUnits (inScope env scheduled) (unitsOf linked) := by
    refine sameUnits_of ?_ fun u hu u' hu' _ sameStmt => ?_
    · rw [keys, SparseProjectedOrder.split_indexed_loaded_of_ids unique steps.perm ids]
      simp [inScope, List.map_map, Function.comp_def]
    · obtain ⟨⟨id, stmt⟩, _, rfl⟩ := List.mem_map.1 hu
      rw [sameStmt]
      exact agree u' hu'
  rw [LinkedBehavior.run_after_link fuel before, LinkedBehavior.run_after_link fuel after]
  have originalUnits : unitsOf before.linked = inScope env source.body := by simp [before, unitsOf, inScope]
  rw [originalUnits]
  exact (ExactLiteralMove.reorder_exact fuel moves ready).trans
    (congrFun (runUnits_same fuel same) ready)

/-- Inline export locals must come from original exporting declarations
actually owned by the authored entry. These are executable IDs. -/
def InlineExportsOwned (source : Module) (B : EsmSplit.Bundle) (entry : EsmSplit.Module) : Prop :=
  ∀ pair ∈ source.exports, ∃ id ∈ entry.stmts, id < B.length ∧
    pair.2 ∈ (B.stmt id).defs ∧ (pair.1, some pair.2) ∈ (B.stmt id).exports

instance (source : Module) (B : EsmSplit.Bundle) (entry : EsmSplit.Module) :
    Decidable (InlineExportsOwned source B entry) := by
  unfold InlineExportsOwned
  infer_instance

/-- Resolve authored inline exports from the actual source and candidate scopes.
Frame names suffice for this step, so a caller can use the same table proof
for different supported declaration kinds. -/
theorem inline_tables {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (valid : EsmSplit.Valid B S P) {original : Program} {source : Module}
    (found : original.find original.entry = some source)
    (footprints : IndexedExportCells.Footprints source.body B)
    (complete : IndexedPlanCells.DefinitionsPresent source.body B)
    {frames : List (Nat × List (Name × Addr))} (frameNames : FramesOf source.body frames)
    {linked : List (Module × Env)}
    (scopes : (splitProgramIndexed source.body B P).scopes frames
      (splitProgramIndexed source.body B P).loaded = .ok linked)
    {entry : EsmSplit.Module} (planEntry : P.find P.entry = some entry)
    (own : InlineExportsOwned source B entry)
    (table : (moduleOfIndexed source.body B P entry).exports = source.exports) :
    ∃ exports, EntryExports.resolve original.entry
        [(source, { vars := source.scope frames, this := .undef })] = .ok exports ∧
      EntryExports.resolve (splitProgramIndexed source.body B P).entry linked = .ok exports := by
  let output := splitProgramIndexed source.body B P
  let target := moduleOfIndexed source.body B P entry
  let env : Env := { vars := source.scope frames, this := .undef }
  have targetFind : output.find output.entry = some target := by
    change (splitProgramIndexed source.body B P).find P.entry = some target
    rw [SparseProjectedOrder.split_indexed_find, planEntry]
    rfl
  obtain ⟨cells, _, targetLink⟩ := IndexedEntryLink.scopes_find output frames output.entry
    output.loaded linked target scopes (IndexedEntryLink.loaded_entry_find targetFind)
  have sourceLink : ([(source, env)] : List (Module × Env)).find?
      (fun (m, _) => m.name == original.entry) = some (source, env) := by
    have name : source.name = original.entry := by
      have selected := List.find?_some found
      simpa [Program.find] using selected
    simp [name]
  have cellAgreement : ∀ pair ∈ source.exports, ∃ a, env.vars.lookup pair.2 = some a ∧
      (target.scope frames ++ cells).lookup pair.2 = some a := by
    intro pair member
    obtain ⟨id, owned, within, declared, _⟩ := own pair member
    obtain ⟨stmt, originalRow⟩ := complete id within pair.2 declared
    have targetRow : (id, stmt) ∈ target.body :=
      (SparseProjectedOrder.moduleOfIndexed_body_lookup source.body B P entry id stmt).2
        ⟨owned, originalRow⟩
    obtain ⟨a, sourceCell, targetCell⟩ := IndexedExportCells.source_and_module_cell
      footprints frameNames valid.1 (source := source) rfl originalRow targetRow declared
    exact ⟨a, sourceCell, by rw [List.lookup_append, targetCell]; rfl⟩
  exact IndexedEntryLink.resolved_entries_equal sourceLink targetLink table cellAgreement

/-- Locally owned inline exports resolve to the original live cells under a
covered plan. Both tables are computed by the actual linker. -/
theorem same_inline_exports {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (valid : EsmSplit.Valid B S P) {original : Program} {source : Module}
    (single : original.modules = [source]) (imports : source.imports = [])
    (found : original.find original.entry = some source)
    (footprints : IndexedExportCells.Footprints source.body B)
    (complete : IndexedPlanCells.DefinitionsPresent source.body B)
    (ordered : source.body.Pairwise (fun a b => a.1 ≤ b.1))
    (consts : ∀ p ∈ source.body, ∃ name init, p.2 = .decl .const name init)
    (covered : (SparseProjectedOrder.executableIds source.body P).Perm (source.body.map Prod.fst))
    {entry : EsmSplit.Module} (planEntry : P.find P.entry = some entry)
    (own : InlineExportsOwned source B entry)
    (table : (moduleOfIndexed source.body B P entry).exports = source.exports) :
    LinkedBehavior.SameExports original (splitProgramIndexed source.body B P) := by
  intro w
  let output := splitProgramIndexed source.body B P
  have unique := footprints.uniqueIds
  have tops : output.tops = source.body :=
    SparseProjectedOrder.split_indexed_tops_of_perm unique covered ordered
  have regions : output.regions = original.regions := by
    rw [SparseProjectedOrder.split_indexed_regions_of_perm unique covered,
      IndexedEntryLink.singleton_source_regions single]
  obtain ⟨frames, ready, allocated⟩ := IndexedConstLink.source_allocation rfl single consts w
  have frameNames := declareTops_names _ allocated
  let env : Env := { vars := source.scope frames, this := .undef }
  let before : LinkedBehavior.CheckedLink original w :=
    ⟨[(source, env)], ready, IndexedEntryLink.link_after_const_allocation
      (IndexedEntryLink.import_free_source_tops found imports ordered)
      unique allocated (IndexedEntryLink.import_free_entry_scopes found imports frames)
      (IndexedEntryLink.source_const_init consts)⟩
  obtain ⟨linked, scopes, init⟩ :=
    IndexedEntryLink.indexed_const_scopes_and_init valid footprints complete frameNames (source := source) rfl consts
  let after : LinkedBehavior.CheckedLink output w :=
    ⟨linked, ready, by
      rw [regions]
      exact IndexedEntryLink.link_after_const_allocation tops unique allocated scopes init⟩
  obtain ⟨exports, sourceTable, targetTable⟩ :=
    inline_tables valid found footprints complete frameNames scopes planEntry own table
  refine ⟨exports, exports, ?_, ?_, .refl _⟩
  · rw [LinkedBehavior.exports, before.equation]
    change (EntryExports.resolve original.entry _).toOption = _
    rw [sourceTable]
    rfl
  · change LinkedBehavior.exports w output = _
    rw [LinkedBehavior.exports, after.equation]
    change (EntryExports.resolve output.entry _).toOption = _
    rw [targetTable]
    rfl

#print axioms inline_tables
#print axioms same_inline_exports
#print axioms run_of_schedule

end CoreJS.IndexedReorder
