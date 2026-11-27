import EsmSplit.CoreJS.IndexedConstLink
import EsmSplit.CoreJS.IndexedSortedScopes
import EsmSplit.CoreJS.LiteralSwapCheck

/-! Exact schedule steps for const declarations in the existing indexed
constructor. The source, allocation, and authored plan determine the linked
units. The adjacent step is checked over original rows before linking. -/

namespace CoreJS.IndexedReorder

open LiteralSwapCheck

/-- The common indexed linker construction for a checked executable order.
Both primitive and allocation-only schedule theorems use this evidence, so
they reason about the same source allocation and generated module scopes. -/
structure ScheduleLinks {B : EsmSplit.Bundle} {P : EsmSplit.Plan}
    (original : Program) (source : Module) (scheduled : List (Nat × Stmt)) (w : World) where
  frames : List (Nat × List (Name × Addr))
  ready : State w
  allocated : declareTops source.body (initialState w original.regions) = .ok frames ready
  env : Env
  envMatches : env = { vars := source.scope frames, this := .undef }
  before : LinkedBehavior.CheckedLink original w
  beforeReady : before.ready = ready
  after : LinkedBehavior.CheckedLink (splitProgramIndexed source.body B P) w
  afterReady : after.ready = ready
  same : SameUnits (inScope env scheduled) (unitsOf after.linked)
  sourceUnits : unitsOf before.linked = inScope env source.body

/-- Allocate the exact source IDs, link the actual indexed candidate, and
relate its loaded units to the requested schedule. -/
theorem checkedScheduleLinks {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (valid : EsmSplit.Valid B S P) {original : Program} {source : Module}
    (single : original.modules = [source]) (imports : source.imports = [])
    (found : original.find original.entry = some source)
    (footprints : IndexedExportCells.Footprints source.body B)
    (free : IndexedUnitsAgree.FreeFootprints source.body B)
    (complete : IndexedPlanCells.DefinitionsPresent source.body B)
    (ordered : source.body.Pairwise (fun a b => a.1 ≤ b.1))
    (consts : ∀ p ∈ source.body, ∃ name init, p.2 = .decl .const name init)
    {scheduled : List (Nat × Stmt)} (permutation : scheduled.Perm source.body)
    (ids : SparseProjectedOrder.executableIds source.body P = scheduled.map Prod.fst)
    (w : World) : Nonempty (ScheduleLinks (B := B) (P := P) original source scheduled w) := by
  let output := splitProgramIndexed source.body B P
  have unique := footprints.uniqueIds
  have covered : (SparseProjectedOrder.executableIds source.body P).Perm (source.body.map Prod.fst) := by
    rw [ids]
    exact permutation.map Prod.fst
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
  have same : SameUnits (inScope env scheduled) (unitsOf linked) := by
    refine sameUnits_of ?_ fun u hu u' hu' _ sameStmt => ?_
    · rw [keys, SparseProjectedOrder.split_indexed_loaded_of_ids unique permutation ids]
      simp [inScope, List.map_map, Function.comp_def]
    · obtain ⟨⟨id, stmt⟩, _, rfl⟩ := List.mem_map.1 hu
      rw [sameStmt]
      exact agree u' hu'
  have sourceUnits : unitsOf before.linked = inScope env source.body := by
    simp [before, unitsOf, inScope]
  exact ⟨⟨frames, ready, allocated, env, rfl, before, rfl, after, rfl, same, sourceUnits⟩⟩

/-- Any theorem about successful executions of the real linked units can use
this common construction to prove normal-completion equivalence. -/
theorem normal_of_unit_schedule {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (valid : EsmSplit.Valid B S P) {original : Program} {source : Module}
    (single : original.modules = [source]) (imports : source.imports = [])
    (found : original.find original.entry = some source)
    (footprints : IndexedExportCells.Footprints source.body B)
    (free : IndexedUnitsAgree.FreeFootprints source.body B)
    (complete : IndexedPlanCells.DefinitionsPresent source.body B)
    (ordered : source.body.Pairwise (fun a b => a.1 ≤ b.1))
    (consts : ∀ p ∈ source.body, ∃ name init, p.2 = .decl .const name init)
    {scheduled : List (Nat × Stmt)} (permutation : scheduled.Perm source.body)
    (ids : SparseProjectedOrder.executableIds source.body P = scheduled.map Prod.fst)
    (unitSchedule : ∀ {w} (links : ScheduleLinks (B := B) (P := P) original source scheduled w)
      (fuel : Nat) (final : State w),
      runUnits fuel (inScope links.env source.body) links.ready = .ok () final ↔
        runUnits fuel (inScope links.env scheduled) links.ready = .ok () final) :
    LinkedBehavior.ScheduleEquivalent .normalCompletion (fun _ => True)
      original (splitProgramIndexed source.body B P) := by
  change LinkedBehavior.NormalEquivalent (fun _ => True) original (splitProgramIndexed source.body B P)
  intro w _ final
  constructor
  · rintro ⟨fuel, hrun, hnotFuel⟩
    obtain ⟨links⟩ := checkedScheduleLinks valid single imports found footprints free complete ordered consts
      permutation ids w
    have sourceRun : runUnits fuel (inScope links.env source.body) links.ready = .ok () final := by
      rw [← links.beforeReady, ← links.sourceUnits, ← LinkedBehavior.run_after_link fuel links.before]
      exact hrun
    have targetUnits := (unitSchedule links fuel final).mp sourceRun
    have targetRun : runProgram w fuel (splitProgramIndexed source.body B P) = .ok () final := by
      rw [LinkedBehavior.run_after_link fuel links.after, links.afterReady,
        ← congrFun (runUnits_same fuel links.same) links.ready]
      exact targetUnits
    exact ⟨fuel, targetRun, by simp⟩
  · rintro ⟨fuel, hrun, hnotFuel⟩
    obtain ⟨links⟩ := checkedScheduleLinks valid single imports found footprints free complete ordered consts
      permutation ids w
    have candidateUnits : runUnits fuel (unitsOf links.after.linked) links.ready = .ok () final := by
      rw [← links.afterReady, ← LinkedBehavior.run_after_link fuel links.after]
      exact hrun
    have targetUnits := (congrFun (runUnits_same fuel links.same) links.ready).trans candidateUnits
    have sourceRun : runProgram w fuel original = .ok () final := by
      rw [LinkedBehavior.run_after_link fuel links.before, links.beforeReady, links.sourceUnits]
      exact (unitSchedule links fuel final).mpr targetUnits
    exact ⟨fuel, sourceRun, by simp⟩

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
  obtain ⟨links⟩ := checkedScheduleLinks valid single imports found footprints free complete ordered consts
    steps.perm ids w
  have cells : ∀ id x e, (id, .decl .const x e) ∈ source.body →
      ∃ a, links.env.vars.lookup x = some a := by
    intro id x e member
    have originalRow := IndexedExportCells.body_lookup footprints member
    have defined := ((footprints.statement id _ originalRow).2 x).2 (by simp [Stmt.bindings, Stmt.varNames, varNamesOf, lexDecls])
    obtain ⟨a, binding, _⟩ := IndexedExportCells.source_and_module_cell footprints
      (declareTops_names _ links.allocated)
      valid.1 (source := source) (m := source) rfl originalRow member defined
    exact ⟨a, by simpa [links.envMatches] using binding⟩
  have moves := steps.linked links.env cells
    (fun _ _ _ hx hy => declareTops_scope_injective links.allocated source
      (by simpa [links.envMatches] using hx) (by simpa [links.envMatches] using hy))
  rw [LinkedBehavior.run_after_link fuel links.before, LinkedBehavior.run_after_link fuel links.after,
    links.beforeReady, links.afterReady, links.sourceUnits]
  exact (ExactLiteralMove.reorder_exact fuel moves links.ready).trans
    (congrFun (runUnits_same fuel links.same) links.ready)

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

/-- Inline exports keep their original live cells for any source made only of
top-level declarations. Allocation and link hoists use the declaration kind,
so initialized and uninitialized lexical `let` rows retain their mutability
and TDZ transitions. The schedule remains exactly the source order. -/
theorem same_inline_exports_of_declarations {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (valid : EsmSplit.Valid B S P) {original : Program} {source : Module}
    (single : original.modules = [source]) (imports : source.imports = [])
    (found : original.find original.entry = some source)
    (footprints : IndexedExportCells.Footprints source.body B)
    (complete : IndexedPlanCells.DefinitionsPresent source.body B)
    (ordered : source.body.Pairwise (fun a b => a.1 ≤ b.1))
    (decls : ∀ p ∈ source.body, ∃ kind name init, p.2 = .decl kind name init)
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
  obtain ⟨frames, ready, allocated⟩ :=
    IndexedConstLink.source_decl_allocation rfl single decls w
  have frameNames := declareTops_names _ allocated
  let env : Env := { vars := source.scope frames, this := .undef }
  let before : LinkedBehavior.CheckedLink original w :=
    ⟨[(source, env)], ready, IndexedEntryLink.link_after_const_allocation
      (IndexedEntryLink.import_free_source_tops found imports ordered)
      unique allocated (IndexedEntryLink.import_free_entry_scopes found imports frames)
      (IndexedEntryLink.source_decl_init decls)⟩
  obtain ⟨linked, scopes⟩ := IndexedEntryLink.indexed_scopes_success
    valid footprints complete frameNames (source := source) rfl
  have init := IndexedEntryLink.indexed_initFunctions_declarations (W := w) scopes decls
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

/-- An order-preserving indexed plan shares the original allocation and hoisted
state. The linked units agree on their free cells, including a hoisted helper's
captures and its callers' imports. Resolve the complete public table from the
same frames; this does not require the source rows to be plain declarations. -/
theorem inline_links_of_order_kept {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (valid : EsmSplit.Valid B S P) {original : Program} {source : Module}
    (single : original.modules = [source]) (imports : source.imports = [])
    (found : original.find original.entry = some source)
    (footprints : IndexedExportCells.Footprints source.body B)
    (free : IndexedUnitsAgree.FreeFootprints source.body B)
    (complete : IndexedPlanCells.DefinitionsPresent source.body B)
    (ordered : source.body.Pairwise (fun a b => a.1 ≤ b.1))
    (kept : SparseProjectedOrder.OrderKept source.body P)
    {entry : EsmSplit.Module} (planEntry : P.find P.entry = some entry)
    (own : InlineExportsOwned source B entry)
    (table : (moduleOfIndexed source.body B P entry).exports = source.exports)
    {w : World} (before : LinkedBehavior.CheckedLink original w) :
    ∃ after : LinkedBehavior.CheckedLink (splitProgramIndexed source.body B P) w,
      before.ready = after.ready ∧
      SameUnits (unitsOf before.linked) (unitsOf after.linked) ∧
      ∃ exports, EntryExports.resolve original.entry before.linked = .ok exports ∧
        EntryExports.resolve (splitProgramIndexed source.body B P).entry after.linked = .ok exports := by
  obtain ⟨tops, regions⟩ :=
    IndexedSortedScopes.indexed_tops_regions valid footprints rfl single imports found ordered
  have scopes := IndexedUnitsAgree.indexed_scopes_agree valid footprints free complete rfl imports found
    ordered (SparseProjectedOrder.split_indexed_loaded_eq_source kept)
  obtain ⟨_, frames, _, after, allocated, beforeScopes, afterScopes, ready, same, _⟩ :=
    OrderedRegion.matching_link_with_frames regions tops scopes before
  have frameNames := declareTops_names _ allocated
  rw [IndexedEntryLink.import_free_source_tops found imports ordered] at frameNames
  have originalLinked := Except.ok.inj
    (beforeScopes.symm.trans (IndexedEntryLink.import_free_entry_scopes found imports frames))
  refine ⟨after, ready, same, ?_⟩
  rw [originalLinked]
  exact inline_tables valid found footprints complete frameNames afterScopes planEntry own table

end CoreJS.IndexedReorder
