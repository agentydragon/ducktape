import EsmSplit.CoreJS.IndexedAllocatedCells
import EsmSplit.CoreJS.EntryExports
import EsmSplit.CoreJS.ProjectedOrder
import EsmSplit.CoreJS.SingleModule

/-! Structural facts for the actual CoreJS scope and entry-linking operations.
No source/candidate execution comparison is used. -/

namespace CoreJS.IndexedEntryLink

variable {W : World}

/-- The linker's identity-sorted declaration units perform no function
hoists, independently of declaration kind or initializer. -/
theorem sorted_initFunctions_declarations (linked : List (Module × Env))
    (allDecl : ∀ u ∈ unitsOf linked, ∃ kind name init, u.stmt = .decl kind name init) :
    (initFunctions ((unitsOf linked).mergeSort fun a b => decide (a.id ≤ b.id)) : M W Unit) = pure () := by
  apply initFunctions_declarations
  intro u member
  exact allDecl u ((List.mergeSort_perm _ _).mem_iff.1 member)

/-- Actual indexed scopes contain only original source statements, so
mixed declaration kinds have no link-time function hoists. -/
theorem indexed_initFunctions_declarations {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {P : EsmSplit.Plan}
    {frames : List (Nat × List (Name × Addr))} {linked : List (Module × Env)}
    (scopeResult : (splitProgramIndexed body B P).scopes frames (splitProgramIndexed body B P).loaded = .ok linked)
    (decls : ∀ p ∈ body, ∃ kind name init, p.2 = .decl kind name init) :
    (initFunctions ((unitsOf linked).mergeSort fun a b => decide (a.id ≤ b.id)) : M W Unit) = pure () := by
  apply sorted_initFunctions_declarations
  intro unit member
  obtain ⟨pair, pairIn, inBody, _⟩ := mem_unitsOf member
  have modules := ProjectedOrder.scopes_modules (splitProgramIndexed body B P) frames
    (splitProgramIndexed body B P).loaded linked scopeResult
  have loaded : pair.1 ∈ (splitProgramIndexed body B P).loaded := by
    rw [← modules]
    exact List.mem_map.mpr ⟨pair, pairIn, rfl⟩
  have flat : (unit.1, unit.2.1) ∈ (splitProgramIndexed body B P).loaded.flatMap (·.body) :=
    List.mem_flatMap.mpr ⟨pair.1, loaded, inBody⟩
  have found := SparseProjectedOrder.loaded_row_lookup flat
  have inSource : body.lookup unit.1 = some unit.2.1 := by simpa [indexedNumbered] using found
  exact decls (unit.1, unit.2.1) (lookup_mem inSource)

/-- Existing const-only callers use the shared declaration proof. -/
theorem indexed_initFunctions_consts {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {P : EsmSplit.Plan}
    {frames : List (Nat × List (Name × Addr))} {linked : List (Module × Env)}
    (scopeResult : (splitProgramIndexed body B P).scopes frames (splitProgramIndexed body B P).loaded = .ok linked)
    (sourceConsts : ∀ p ∈ body, ∃ name init, p.2 = .decl .const name init) :
    (initFunctions ((unitsOf linked).mergeSort fun a b => decide (a.id ≤ b.id)) : M W Unit) = pure () := by
  apply indexed_initFunctions_declarations scopeResult
  intro p member
  obtain ⟨name, init, shape⟩ := sourceConsts p member
  exact ⟨.const, name, init, shape⟩

theorem initFunctions_consts (units : List LinkedUnit)
    (allConst : ∀ u ∈ units, ∃ name init, u.stmt = .decl .const name init) :
    (initFunctions units : M W Unit) = pure () := by
  apply initFunctions_declarations
  intro u member
  obtain ⟨name, init, shape⟩ := allConst u member
  exact ⟨.const, name, init, shape⟩

/-- Const-only source scopes use the same declaration-hoist argument. -/
theorem sorted_initFunctions_consts (linked : List (Module × Env))
    (allConst : ∀ u ∈ unitsOf linked, ∃ name init, u.stmt = .decl .const name init) :
    (initFunctions ((unitsOf linked).mergeSort fun a b => decide (a.id ≤ b.id)) : M W Unit) = pure () := by
  apply sorted_initFunctions_declarations
  intro u member
  obtain ⟨name, init, shape⟩ := allConst u member
  exact ⟨.const, name, init, shape⟩

#print axioms indexed_initFunctions_declarations
#print axioms indexed_initFunctions_consts

/-- The indexed constructor's real linker constructs scopes for every loaded
module from source/plan validity and source frames, without testing a candidate
execution. -/
theorem indexed_scopes_success {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} (valid : EsmSplit.Valid B S P)
    (footprints : IndexedExportCells.Footprints body B) (complete : IndexedPlanCells.DefinitionsPresent body B)
    {frames : List (Nat × List (Name × Addr))} (frameNames : FramesOf body frames)
    {source : Module} (sourceBody : source.body = body) :
    ∃ linked, (splitProgramIndexed body B P).scopes frames (splitProgramIndexed body B P).loaded = .ok linked := by
  obtain ⟨linked, success, _, _⟩ := scopes_ok (splitProgramIndexed body B P) frames
    (fun _ _ => True) (splitProgramIndexed body B P).loaded (by
      intro m member
      obtain ⟨path, _, found⟩ := List.mem_filterMap.1 member
      rw [SparseProjectedOrder.split_indexed_find] at found
      obtain ⟨md, hfind, rfl⟩ := Option.map_eq_some_iff.1 found
      have inPlan : md ∈ P.mods := List.mem_of_find?_eq_some hfind
      obtain ⟨cells, imported, _⟩ := IndexedPlanCells.module_scope_cells valid
        (IndexedConstructorCells.projection body B P) footprints complete frameNames sourceBody inPlan
      exact ⟨cells, imported, trivial⟩)
  exact ⟨linked, success⟩

/-- For const-only source units, the actual constructed scopes link without
function-hoist state changes. This holds for arbitrary statement initializers;
it concerns linker initialization rather than statement execution. -/
theorem indexed_const_scopes_and_init {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} (valid : EsmSplit.Valid B S P)
    (footprints : IndexedExportCells.Footprints body B) (complete : IndexedPlanCells.DefinitionsPresent body B)
    {frames : List (Nat × List (Name × Addr))} (frameNames : FramesOf body frames)
    {source : Module} (sourceBody : source.body = body)
    (sourceConsts : ∀ p ∈ body, ∃ name init, p.2 = .decl .const name init) :
    ∃ linked, (splitProgramIndexed body B P).scopes frames (splitProgramIndexed body B P).loaded = .ok linked ∧
      (initFunctions ((unitsOf linked).mergeSort fun a b => decide (a.id ≤ b.id)) : M W Unit) = pure () := by
  obtain ⟨linked, linkedScopes⟩ := indexed_scopes_success valid footprints complete frameNames sourceBody
  exact ⟨linked, linkedScopes, indexed_initFunctions_consts linkedScopes sourceConsts⟩

#print axioms indexed_const_scopes_and_init
#print axioms indexed_scopes_success

/-- The depth-first module schedule always includes the requested entry,
even when its dependencies contain cycles. -/
theorem entry_in_evalOrder (program : Program) : program.entry ∈ program.evalOrder := by
  unfold Program.evalOrder Program.visit
  rw [Nat.add_one]
  simp only [EsmSplit.visitImports, List.contains_nil, Bool.false_eq_true, ↓reduceIte,
    List.mem_append, List.mem_singleton]
  exact Or.inr trivial

/-- If the program names its entry module, selecting that name from the actual
loaded modules returns exactly that module. -/
theorem loaded_entry_find {program : Program} {entryModule : Module}
    (found : program.find program.entry = some entryModule) :
    program.loaded.find? (fun m => m.name == program.entry) = some entryModule := by
  have nameEq : entryModule.name = program.entry := by
    have selected := List.find?_some found
    simpa [Program.find] using selected
  have entryMember : entryModule ∈ program.loaded := by
    apply List.mem_filterMap.mpr
    exact ⟨program.entry, entry_in_evalOrder program, found⟩
  have existsSelected : (program.loaded.find? (fun m => m.name == program.entry)).isSome = true := by
    apply List.find?_isSome.2
    exact ⟨entryModule, entryMember, by simp [nameEq]⟩
  cases selected : program.loaded.find? (fun m => m.name == program.entry) with
  | none => simp [selected] at existsSelected
  | some actual =>
    have actualName : actual.name = program.entry := by
      have matchName := List.find?_some selected
      simpa using matchName
    have actualMember := List.mem_of_find?_eq_some selected
    obtain ⟨path, _, actualFound⟩ := List.mem_filterMap.1 actualMember
    have actualPath : actual.name = path := by
      have matchPath := List.find?_some actualFound
      simpa [Program.find] using matchPath
    have sameFind : program.find program.entry = some actual := by
      rw [← actualName, actualPath]
      exact actualFound
    have same : actual = entryModule := Option.some.inj (sameFind.symm.trans found)
    simp [same]

/-- An import-free source entry is the only loaded source module, independent
of unrelated modules that might occur in the program. -/
theorem import_free_entry_loaded {program : Program} {entryModule : Module}
    (found : program.find program.entry = some entryModule) (noImports : entryModule.imports = []) :
    program.loaded = [entryModule] := by
  have order : program.evalOrder = [program.entry] := by
    unfold Program.evalOrder Program.visit
    rw [Nat.add_one]
    simp [EsmSplit.visitImports, found, noImports]
  rw [Program.loaded, order]
  simp [found]

/-- The source's actual scope constructor succeeds for an import-free entry. -/
theorem import_free_entry_scopes {program : Program} {entryModule : Module}
    (found : program.find program.entry = some entryModule) (noImports : entryModule.imports = [])
    (frames : List (Nat × List (Name × Addr))) :
    program.scopes frames program.loaded =
      .ok [(entryModule, { vars := entryModule.scope frames, this := .undef })] := by
  rw [import_free_entry_loaded found noImports]
  simp [Program.scopes, Program.imported, noImports]
  rfl

/-- The actual import-free source allocates the original rows in their
original identity order. The ordering premise is on the source AST body. -/
theorem import_free_source_tops {program : Program} {source : Module}
    (found : program.find program.entry = some source) (noImports : source.imports = [])
    (ordered : source.body.Pairwise (fun a b => a.1 ≤ b.1)) : program.tops = source.body := by
  rw [Program.tops, import_free_entry_loaded found noImports]
  simp only [List.flatMap_cons, List.flatMap_nil, List.append_nil]
  exact List.mergeSort_of_pairwise (ordered.imp fun h => by simpa using h)

/-- A singleton source program's initial region count is determined by its
original executable identities, including gaps for metadata. -/
theorem singleton_source_regions {program : Program} {source : Module}
    (singleton : program.modules = [source]) :
    program.regions = (source.body.map fun row => row.1 + 1).foldl max 0 := by
  simp [Program.regions, singleton]

#print axioms import_free_source_tops
#print axioms singleton_source_regions
#print axioms import_free_entry_loaded
#print axioms import_free_entry_scopes
#print axioms entry_in_evalOrder
#print axioms loaded_entry_find

/-- An import-free source with declaration-only body has no link-time function
hoists in its actual singleton scope list. -/
theorem source_decl_init {source : Module} {frames : List (Nat × List (Name × Addr))}
    (sourceDecls : ∀ p ∈ source.body, ∃ kind name init, p.2 = .decl kind name init) :
    (initFunctions ((unitsOf [(source, { vars := source.scope frames, this := .undef })]).mergeSort
      fun a b => decide (a.id ≤ b.id)) : M W Unit) = pure () := by
  apply sorted_initFunctions_declarations
  intro unit member
  obtain ⟨pair, pairIn, inBody, _⟩ := mem_unitsOf member
  have pairEq : pair = (source, { vars := source.scope frames, this := .undef }) := by simpa using pairIn
  subst pair
  exact sourceDecls (unit.1, unit.2.1) inBody

/-- Const-only source initialization uses the shared declaration proof. -/
theorem source_const_init {source : Module} {frames : List (Nat × List (Name × Addr))}
    (sourceConsts : ∀ p ∈ source.body, ∃ name init, p.2 = .decl .const name init) :
    (initFunctions ((unitsOf [(source, { vars := source.scope frames, this := .undef })]).mergeSort
      fun a b => decide (a.id ≤ b.id)) : M W Unit) = pure () := by
  apply source_decl_init
  intro p member
  obtain ⟨name, init, shape⟩ := sourceConsts p member
  exact ⟨.const, name, init, shape⟩

#print axioms source_decl_init
#print axioms source_const_init

/-- The actual linker returns the constructed scope list in the post-allocation
state when identity checking succeeds and the const-unit hoist phase is inert.
All premises describe one program and its allocation, not a compared pair. -/
theorem link_after_const_allocation {program : Program} {body : List (Nat × Stmt)}
    {start ready : State W} {frames : List (Nat × List (Name × Addr))} {linked : List (Module × Env)}
    (tops : program.tops = body) (unique : (body.map Prod.fst).Nodup)
    (allocated : declareTops body start = .ok frames ready)
    (scopes : program.scopes frames program.loaded = .ok linked)
    (init : (initFunctions ((unitsOf linked).mergeSort fun a b => decide (a.id ≤ b.id)) : M W Unit) = pure ()) :
    link program start = .ok linked ready := by
  have checked : (checkIdentities body : M W Unit) start = .ok () start := by
    simp [checkIdentities, unique, pure]
  unfold link linkUsingScopes
  rw [tops]
  simp only [M.bind_apply, checked, allocated, scopes, lift, pure]
  change ((initFunctions ((unitsOf linked).mergeSort fun a b => decide (a.1 ≤ b.1)) >>= fun _ => pure linked) : M W _)
    ready = .ok linked ready
  rw [init]
  rfl

#print axioms link_after_const_allocation

/-- A failure of the shared declaration pass propagates through the actual
linker unchanged. This includes throws, unknown results, and fuel exhaustion. -/
theorem link_allocation_failure {program : Program} {body : List (Nat × Stmt)} {start : State W}
    (tops : program.tops = body) (unique : (body.map Prod.fst).Nodup) :
    match declareTops body start with
    | .ok _ _ => True
    | .throw v state => link program start = .throw v state
    | .unknown why state => link program start = .unknown why state
    | .fuel => link program start = .fuel := by
  have checked : (checkIdentities body : M W Unit) start = .ok () start := by
    simp [checkIdentities, unique, pure]
  unfold link linkUsingScopes
  rw [tops]
  simp only [M.bind_apply, checked]
  cases allocated : declareTops body start <;> simp [allocated, M.bind_apply]

#print axioms link_allocation_failure

/-- Selecting an entry from the actual list returned by `Program.scopes`
selects the same first named module as the loaded-module list and pairs it
with exactly the scope and imports produced by the CoreJS linker. -/
theorem scopes_find (program : Program) (frames : List (Nat × List (Name × Addr))) (entry : String) :
    ∀ (modules : List Module) (linked : List (Module × Env)) (target : Module),
      program.scopes frames modules = .ok linked →
      modules.find? (fun m => m.name == entry) = some target →
      ∃ cells, program.imported frames target.imports = .ok cells ∧
        linked.find? (fun (m, _) => m.name == entry) =
          some (target, { vars := target.scope frames ++ cells, this := .undef })
  | [], linked, target, scopeResult, found => by simp at found
  | m :: rest, linked, target, scopeResult, found => by
    change ((do
      let imported ← program.imported frames m.imports
      let tail ← program.scopes frames rest
      pure ((m, { vars := m.scope frames ++ imported, this := .undef }) :: tail)) : Except Abort _) = .ok linked at scopeResult
    cases imported : program.imported frames m.imports with
    | error why => rw [imported] at scopeResult; cases scopeResult
    | ok cells =>
      cases remaining : program.scopes frames rest with
      | error why => rw [imported, remaining] at scopeResult; cases scopeResult
      | ok tail =>
        rw [imported, remaining] at scopeResult
        cases scopeResult
        cases hit : (m.name == entry) with
        | true =>
          have equal : m = target := by simpa [List.find?_cons, hit] using found
          subst target
          exact ⟨cells, imported, by simp [hit]⟩
        | false =>
          have later : rest.find? (fun m => m.name == entry) = some target := by
            simpa [List.find?_cons, hit] using found
          obtain ⟨targetCells, targetImports, targetFound⟩ := scopes_find program frames entry rest tail target remaining later
          exact ⟨targetCells, targetImports, by simpa [List.find?_cons, hit] using targetFound⟩

/-- A public/local pair resolved in a concrete CoreJS environment. -/
private def entryCell (env : Env) : Name × Name → Except String (Name × Addr) := fun (publicName, localName) => do
  let some a := env.vars.lookup localName | throw s!"unbound entry export {publicName}"
  return (publicName, a)

/-- An ordered list of aliases resolves to one identical public-address table
when each local has the same present address in the two environments. -/
theorem export_pairs_equal (before after : Env) : ∀ names : List (Name × Name),
    (∀ p ∈ names, ∃ a, before.vars.lookup p.2 = some a ∧ after.vars.lookup p.2 = some a) →
    ∃ table, names.mapM (entryCell before) = .ok table ∧ names.mapM (entryCell after) = .ok table
  | [], _ => ⟨[], rfl, rfl⟩
  | (publicName, localName) :: rest, agree => by
    obtain ⟨a, original, candidate⟩ := agree (publicName, localName) (by simp)
    obtain ⟨table, hb, ha⟩ := export_pairs_equal before after rest
      (fun p member => agree p (List.mem_cons_of_mem _ member))
    refine ⟨(publicName, a) :: table, ?_, ?_⟩
    · simp [entryCell, original, hb]; rfl
    · simp [entryCell, candidate, ha]; rfl

/-- EntryExports.resolve uses that ordered alias table for the actual selected
linked entries, including when the source and candidate entry names differ. -/
theorem resolved_entries_equal {sourceName candidateName : String} {source candidate : Module}
    {sourceEnv candidateEnv : Env} {sourceLinked candidateLinked : List (Module × Env)}
    (sourceFound : sourceLinked.find? (fun (m, _) => m.name == sourceName) = some (source, sourceEnv))
    (candidateFound : candidateLinked.find? (fun (m, _) => m.name == candidateName) = some (candidate, candidateEnv))
    (exports : candidate.exports = source.exports)
    (agree : ∀ p ∈ source.exports, ∃ a, sourceEnv.vars.lookup p.2 = some a ∧ candidateEnv.vars.lookup p.2 = some a) :
    ∃ table, EntryExports.resolve sourceName sourceLinked = .ok table ∧
      EntryExports.resolve candidateName candidateLinked = .ok table := by
  obtain ⟨table, before, after⟩ := export_pairs_equal sourceEnv candidateEnv source.exports agree
  refine ⟨table, ?_, ?_⟩
  · simp only [EntryExports.resolve, sourceFound]
    exact before
  · simp only [EntryExports.resolve, candidateFound, exports]
    exact after

/-- When actual CoreJS scope construction selects the source and authored
entry modules, resolving their ordered public aliases gives the same table of
addresses. Every such address is allocated in the source declaration result.
The source-scope and loaded-entry premises are structural linker obligations;
they do not compare executions or entry tables. Candidate scopes are derived
from source/plan validity. -/
theorem row_resolved_entries {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} (valid : EsmSplit.Valid B S P)
    (footprints : IndexedExportCells.Footprints body B) (complete : IndexedPlanCells.DefinitionsPresent body B)
    {s s' : State W} {frames : List (Nat × List (Name × Addr))}
    (allocated : declareTops body s = .ok frames s')
    {sourceProgram : Program} {source : Module} (sourceBody : source.body = body)
    (sourceImports : source.imports = []) {row : EsmSplit.LocalExportUnit}
    (sourceRow : InteriorExportNames.SourceRow source row)
    (rowFootprint : row.FootprintMatches (B.stmt row.ordinal))
    {owner : EsmSplit.Module} (ownerIn : owner ∈ P.mods) (rowWithin : row.ordinal < B.length)
    (authored : S.claimants B row.ordinal = [owner.name]) (unitsUnique : owner.stmts.Nodup)
    (glue : owner.exports = [])
    (onlyRow : ∀ id ∈ owner.stmts, id ≠ row.ordinal → (B.stmt id).exports = [])
    (sourceDefs : ∀ pair ∈ row.names, ∃ id, id < B.length ∧ pair.2 ∈ (B.stmt id).defs)
    {sourceLinked : List (Module × Env)}
    (sourceScopes : sourceProgram.scopes frames sourceProgram.loaded = .ok sourceLinked)
    (sourceEntry : sourceProgram.loaded.find? (fun m => m.name == sourceProgram.entry) = some source)
    (targetEntry : (splitProgramIndexed body B P).loaded.find?
      (fun m => m.name == (splitProgramIndexed body B P).entry) = some (moduleOfIndexed body B P owner)) :
    ∃ targetLinked table,
      (splitProgramIndexed body B P).scopes frames (splitProgramIndexed body B P).loaded = .ok targetLinked ∧
      row.ordinal ∈ owner.stmts ∧
      EntryExports.resolve sourceProgram.entry sourceLinked = .ok table ∧
      EntryExports.resolve (splitProgramIndexed body B P).entry targetLinked = .ok table ∧
      ∀ pair ∈ row.names, ∃ a b, (source.scope frames).lookup pair.2 = some a ∧ s'.cellAt a = some b := by
  obtain ⟨targetLinked, targetScopes⟩ := indexed_scopes_success valid footprints complete
    (declareTops_names body allocated) sourceBody
  obtain ⟨cells, owned, published, imported, agree⟩ :=
    IndexedAllocatedCells.row_allocated_cells valid footprints complete allocated sourceBody sourceRow rowFootprint
      ownerIn rowWithin authored unitsUnique glue onlyRow sourceDefs
  obtain ⟨sourceCells, sourceImported, sourceFound⟩ :=
    scopes_find sourceProgram frames sourceProgram.entry sourceProgram.loaded sourceLinked source sourceScopes sourceEntry
  have noSourceCells : sourceCells = [] := by
    rw [sourceImports] at sourceImported
    cases sourceImported
    rfl
  subst sourceCells
  obtain ⟨targetCells, targetImported, targetFound⟩ :=
    scopes_find (splitProgramIndexed body B P) frames (splitProgramIndexed body B P).entry
      (splitProgramIndexed body B P).loaded targetLinked (moduleOfIndexed body B P owner) targetScopes targetEntry
  have sameCells : targetCells = cells := by rw [imported] at targetImported; cases targetImported; rfl
  subst targetCells
  have envAgree : ∀ pair ∈ source.exports, ∃ a,
      ({ vars := source.scope frames ++ [], this := .undef } : Env).vars.lookup pair.2 = some a ∧
      ({ vars := (moduleOfIndexed body B P owner).scope frames ++ cells, this := .undef } : Env).vars.lookup pair.2 = some a := by
    intro pair member
    have inRow : pair ∈ row.names := by rw [← sourceRow.exports]; exact member
    obtain ⟨a, _, original, target, _⟩ := agree pair inRow
    exact ⟨a, by simpa using original, target⟩
  obtain ⟨table, original, target⟩ := resolved_entries_equal sourceFound targetFound published envAgree
  refine ⟨targetLinked, table, targetScopes, owned, original, target, ?_⟩
  intro pair member
  obtain ⟨a, b, original, _, present⟩ := agree pair member
  exact ⟨a, b, original, present⟩

/-- A source import-free entry and the authored plan's entry lookup are
sufficient to resolve the actual ordered entry tables; no loaded-list or
scope-success results are supplied by the caller. -/
theorem row_resolved_from_plan {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} (valid : EsmSplit.Valid B S P)
    (footprints : IndexedExportCells.Footprints body B) (complete : IndexedPlanCells.DefinitionsPresent body B)
    {s s' : State W} {frames : List (Nat × List (Name × Addr))}
    (allocated : declareTops body s = .ok frames s')
    {sourceProgram : Program} {source : Module} (sourceBody : source.body = body)
    (sourceImports : source.imports = []) (sourceFind : sourceProgram.find sourceProgram.entry = some source)
    {row : EsmSplit.LocalExportUnit} (sourceRow : InteriorExportNames.SourceRow source row)
    (rowFootprint : row.FootprintMatches (B.stmt row.ordinal))
    {owner : EsmSplit.Module} (ownerIn : owner ∈ P.mods) (rowWithin : row.ordinal < B.length)
    (planEntry : P.find P.entry = some owner)
    (authored : S.claimants B row.ordinal = [owner.name]) (unitsUnique : owner.stmts.Nodup)
    (glue : owner.exports = [])
    (onlyRow : ∀ id ∈ owner.stmts, id ≠ row.ordinal → (B.stmt id).exports = [])
    (sourceDefs : ∀ pair ∈ row.names, ∃ id, id < B.length ∧ pair.2 ∈ (B.stmt id).defs) :
    ∃ targetLinked table,
      (splitProgramIndexed body B P).scopes frames (splitProgramIndexed body B P).loaded = .ok targetLinked ∧
      row.ordinal ∈ owner.stmts ∧
      EntryExports.resolve sourceProgram.entry [(source, { vars := source.scope frames, this := .undef })] = .ok table ∧
      EntryExports.resolve (splitProgramIndexed body B P).entry targetLinked = .ok table ∧
      ∀ pair ∈ row.names, ∃ a b, (source.scope frames).lookup pair.2 = some a ∧ s'.cellAt a = some b := by
  have targetFind : (splitProgramIndexed body B P).find (splitProgramIndexed body B P).entry =
      some (moduleOfIndexed body B P owner) := by
    change (splitProgramIndexed body B P).find P.entry = some (moduleOfIndexed body B P owner)
    rw [SparseProjectedOrder.split_indexed_find, planEntry]
    rfl
  exact row_resolved_entries valid footprints complete allocated sourceBody sourceImports sourceRow rowFootprint
    ownerIn rowWithin authored unitsUnique glue onlyRow sourceDefs
    (import_free_entry_scopes sourceFind sourceImports frames) (loaded_entry_find sourceFind) (loaded_entry_find targetFind)

/-- Under structural equality of the candidate's top-level identities and
region count, a successful source allocation produces actual source and split
links with the same ready state and the same ordered entry export table. This
step admits declaration-only executable source rows; allocation failure and execution
outcomes require separate composition. -/
theorem row_actual_links_from_allocation {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} (valid : EsmSplit.Valid B S P)
    (footprints : IndexedExportCells.Footprints body B) (complete : IndexedPlanCells.DefinitionsPresent body B)
    {sourceProgram : Program} {source : Module} (sourceBody : source.body = body)
    (sourceImports : source.imports = []) (sourceFind : sourceProgram.find sourceProgram.entry = some source)
    (ordered : body.Pairwise (fun a b => a.1 ≤ b.1))
    (sourceDecls : ∀ p ∈ body, ∃ kind name init, p.2 = .decl kind name init)
    (candidateTops : (splitProgramIndexed body B P).tops = body)
    (candidateRegions : (splitProgramIndexed body B P).regions = sourceProgram.regions)
    {ready : State W} {frames : List (Nat × List (Name × Addr))}
    (allocated : declareTops body (initialState W sourceProgram.regions) = .ok frames ready)
    {row : EsmSplit.LocalExportUnit} (sourceRow : InteriorExportNames.SourceRow source row)
    (rowFootprint : row.FootprintMatches (B.stmt row.ordinal))
    {owner : EsmSplit.Module} (ownerIn : owner ∈ P.mods) (rowWithin : row.ordinal < B.length)
    (planEntry : P.find P.entry = some owner)
    (authored : S.claimants B row.ordinal = [owner.name]) (unitsUnique : owner.stmts.Nodup)
    (glue : owner.exports = [])
    (onlyRow : ∀ id ∈ owner.stmts, id ≠ row.ordinal → (B.stmt id).exports = [])
    (sourceDefs : ∀ pair ∈ row.names, ∃ id, id < B.length ∧ pair.2 ∈ (B.stmt id).defs) :
    ∃ targetLinked table,
      link sourceProgram (initialState W sourceProgram.regions) =
        .ok [(source, { vars := source.scope frames, this := .undef })] ready ∧
      link (splitProgramIndexed body B P) (initialState W (splitProgramIndexed body B P).regions) =
        .ok targetLinked ready ∧ row.ordinal ∈ owner.stmts ∧
      EntryExports.resolve sourceProgram.entry [(source, { vars := source.scope frames, this := .undef })] = .ok table ∧
      EntryExports.resolve (splitProgramIndexed body B P).entry targetLinked = .ok table ∧
      ∀ pair ∈ row.names, ∃ a b, (source.scope frames).lookup pair.2 = some a ∧ ready.cellAt a = some b := by
  have sourceOrdered : source.body.Pairwise (fun a b => a.1 ≤ b.1) := by rw [sourceBody]; exact ordered
  have sourceTops : sourceProgram.tops = body := (import_free_source_tops sourceFind sourceImports sourceOrdered).trans sourceBody
  have sourceShape : ∀ p ∈ source.body, ∃ kind name init, p.2 = .decl kind name init := by
    rw [sourceBody]
    exact sourceDecls
  have sourceLink := link_after_const_allocation sourceTops footprints.uniqueIds allocated
    (import_free_entry_scopes sourceFind sourceImports frames) (source_decl_init sourceShape)
  obtain ⟨targetLinked, table, targetScopes, owned, before, after, present⟩ :=
    row_resolved_from_plan valid footprints complete allocated sourceBody sourceImports sourceFind sourceRow
      rowFootprint ownerIn rowWithin planEntry authored unitsUnique glue onlyRow sourceDefs
  have targetAllocation : declareTops body (initialState W (splitProgramIndexed body B P).regions) = .ok frames ready := by
    rw [candidateRegions]
    exact allocated
  have targetInit := indexed_initFunctions_declarations (W := W) targetScopes sourceDecls
  have targetLink := link_after_const_allocation candidateTops footprints.uniqueIds targetAllocation targetScopes targetInit
  exact ⟨targetLinked, table, sourceLink, targetLink, owned, before, after, present⟩

/-- The public entry tables agree after successful linking; all modeled
link-time failures and their states agree exactly. -/
def EntryLinkOutcomesAgree (source candidate : Program) (w : World) : Prop :=
  match link source (initialState w source.regions), link candidate (initialState w candidate.regions) with
  | .ok sourceLinked sourceState, .ok candidateLinked candidateState =>
      sourceState = candidateState ∧ ∃ table,
        EntryExports.resolve source.entry sourceLinked = .ok table ∧
        EntryExports.resolve candidate.entry candidateLinked = .ok table
  | .throw x s, .throw y t => x = y ∧ s = t
  | .unknown x s, .unknown y t => x = y ∧ s = t
  | .fuel, .fuel => True
  | _, _ => False

/-- For every modeled world, actual source and candidate link outcomes agree,
including allocation failures. On successful links their exact ordered public
entry-address tables agree. Structural candidate tops/regions are source/plan
proof obligations, not comparisons of executions. -/
theorem row_link_outcomes_from_plan {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} (valid : EsmSplit.Valid B S P)
    (footprints : IndexedExportCells.Footprints body B) (complete : IndexedPlanCells.DefinitionsPresent body B)
    {sourceProgram : Program} {source : Module} (sourceBody : source.body = body)
    (sourceImports : source.imports = []) (sourceFind : sourceProgram.find sourceProgram.entry = some source)
    (ordered : body.Pairwise (fun a b => a.1 ≤ b.1))
    (sourceDecls : ∀ p ∈ body, ∃ kind name init, p.2 = .decl kind name init)
    (candidateTops : (splitProgramIndexed body B P).tops = body)
    (candidateRegions : (splitProgramIndexed body B P).regions = sourceProgram.regions)
    {row : EsmSplit.LocalExportUnit} (sourceRow : InteriorExportNames.SourceRow source row)
    (rowFootprint : row.FootprintMatches (B.stmt row.ordinal))
    {owner : EsmSplit.Module} (ownerIn : owner ∈ P.mods) (rowWithin : row.ordinal < B.length)
    (planEntry : P.find P.entry = some owner)
    (authored : S.claimants B row.ordinal = [owner.name]) (unitsUnique : owner.stmts.Nodup)
    (glue : owner.exports = [])
    (onlyRow : ∀ id ∈ owner.stmts, id ≠ row.ordinal → (B.stmt id).exports = [])
    (sourceDefs : ∀ pair ∈ row.names, ∃ id, id < B.length ∧ pair.2 ∈ (B.stmt id).defs) :
    ∀ w : World, EntryLinkOutcomesAgree sourceProgram (splitProgramIndexed body B P) w := by
  intro w
  have sourceOrdered : source.body.Pairwise (fun a b => a.1 ≤ b.1) := by rw [sourceBody]; exact ordered
  have sourceTops : sourceProgram.tops = body := (import_free_source_tops sourceFind sourceImports sourceOrdered).trans sourceBody
  cases allocation : declareTops body (initialState w sourceProgram.regions) with
  | ok frames ready =>
    obtain ⟨targetLinked, table, sourceLink, targetLink, _, sourceTable, targetTable, _⟩ :=
      row_actual_links_from_allocation valid footprints complete sourceBody sourceImports sourceFind ordered
        sourceDecls candidateTops candidateRegions allocation sourceRow rowFootprint ownerIn rowWithin planEntry
        authored unitsUnique glue onlyRow sourceDefs
    unfold EntryLinkOutcomesAgree
    rw [sourceLink, targetLink]
    exact ⟨rfl, table, sourceTable, targetTable⟩
  | throw value state =>
    have sourceFail := link_allocation_failure (W := w) (start := initialState w sourceProgram.regions) sourceTops footprints.uniqueIds
    have targetFail := link_allocation_failure (W := w) (start := initialState w sourceProgram.regions) candidateTops footprints.uniqueIds
    rw [allocation] at sourceFail targetFail
    unfold EntryLinkOutcomesAgree
    rw [candidateRegions, sourceFail, targetFail]
    exact ⟨rfl, rfl⟩
  | unknown why state =>
    have sourceFail := link_allocation_failure (W := w) (start := initialState w sourceProgram.regions) sourceTops footprints.uniqueIds
    have targetFail := link_allocation_failure (W := w) (start := initialState w sourceProgram.regions) candidateTops footprints.uniqueIds
    rw [allocation] at sourceFail targetFail
    unfold EntryLinkOutcomesAgree
    rw [candidateRegions, sourceFail, targetFail]
    exact ⟨rfl, rfl⟩
  | fuel =>
    have sourceFail := link_allocation_failure (W := w) (start := initialState w sourceProgram.regions) sourceTops footprints.uniqueIds
    have targetFail := link_allocation_failure (W := w) (start := initialState w sourceProgram.regions) candidateTops footprints.uniqueIds
    rw [allocation] at sourceFail targetFail
    unfold EntryLinkOutcomesAgree
    rw [candidateRegions, sourceFail, targetFail]
    trivial

#print axioms row_link_outcomes_from_plan
#print axioms row_actual_links_from_allocation
#print axioms row_resolved_from_plan
#print axioms row_resolved_entries
#print axioms export_pairs_equal
#print axioms resolved_entries_equal
#print axioms scopes_find
#print axioms initFunctions_consts
#print axioms sorted_initFunctions_consts

end CoreJS.IndexedEntryLink
