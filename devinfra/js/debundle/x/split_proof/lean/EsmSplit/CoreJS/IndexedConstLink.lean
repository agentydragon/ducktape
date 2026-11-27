import EsmSplit.CoreJS.IndexedUnitsAgree
import EsmSplit.CoreJS.SingleModule

/-! Successful source allocation and live exports for sparse original IDs. -/

namespace CoreJS.IndexedConstLink

private theorem start_le_max_fold (values : List Nat) (start : Nat) :
    start ≤ values.foldl max start := by
  induction values generalizing start with
  | nil => exact Nat.le_refl _
  | cons value rest ih =>
    rw [List.foldl_cons]
    exact Nat.le_trans (Nat.le_max_left _ _) (ih (max start value))

private theorem mem_le_max_fold (values : List Nat) (start value : Nat)
    (member : value ∈ values) : value ≤ values.foldl max start := by
  induction values generalizing start with
  | nil => simp at member
  | cons head rest ih =>
    rw [List.foldl_cons]
    rcases List.mem_cons.mp member with same | later
    · subst head
      exact Nat.le_trans (Nat.le_max_right _ _) (start_le_max_fold rest (max start value))
    · exact ih (max start head) later

/-- Declaration allocation succeeds in every world even when original IDs have gaps. -/
theorem source_decl_allocation {body : List (Nat × Stmt)} {sourceProgram : Program} {source : Module}
    (sourceBody : source.body = body) (singleton : sourceProgram.modules = [source])
    (decls : ∀ p ∈ body, ∃ kind name init, p.2 = .decl kind name init) (w : World) :
    ∃ frames ready, declareTops body (initialState w sourceProgram.regions) = .ok frames ready := by
  have extent : sourceProgram.regions = (body.map fun row => row.1 + 1).foldl max 0 := by
    rw [IndexedEntryLink.singleton_source_regions singleton, sourceBody]
  have bound : ∀ p ∈ body, p.1 + 1 < (initialState w sourceProgram.regions).cells.size := by
    intro p hp
    have inExtent : p.1 + 1 ∈ body.map (fun row => row.1 + 1) := List.mem_map.2 ⟨p, hp, rfl⟩
    have le := mem_le_max_fold _ 0 _ inExtent
    simp only [initialState, Array.size_replicate, extent]
    exact Nat.lt_succ_of_le le
  obtain ⟨frames, ready, allocation, _⟩ := declareDeclTops_ok body decls (initialState w sourceProgram.regions) bound
  exact ⟨frames, ready, allocation⟩

/-- The original declaration pass succeeds in every world and returns the
per-row binding facts for the actual sparse identities. -/
theorem source_decl_bindings {body : List (Nat × Stmt)} {sourceProgram : Program} {source : Module}
    (sourceBody : source.body = body) (singleton : sourceProgram.modules = [source])
    (decls : ∀ p ∈ body, ∃ kind name init, p.2 = .decl kind name init) (w : World) :
    ∃ frames ready, declareTops body (initialState w sourceProgram.regions) = .ok frames ready ∧
      IndexedAllocatedCells.TopBindingAllocation ready body frames := by
  obtain ⟨frames, ready, allocated⟩ := source_decl_allocation sourceBody singleton decls w
  exact ⟨frames, ready, allocated, IndexedAllocatedCells.declareTops_bindings body allocated⟩

/-- Const allocation is a specialization of declaration allocation. -/
theorem source_allocation {body : List (Nat × Stmt)} {sourceProgram : Program} {source : Module}
    (sourceBody : source.body = body) (singleton : sourceProgram.modules = [source])
    (consts : ∀ p ∈ body, ∃ name init, p.2 = .decl .const name init) (w : World) :
    ∃ frames ready, declareTops body (initialState w sourceProgram.regions) = .ok frames ready := by
  have decls : ∀ p ∈ body, ∃ kind name init, p.2 = .decl kind name init := by
    intro p member
    obtain ⟨name, value, shape⟩ := consts p member
    exact ⟨.const, name, value, shape⟩
  obtain ⟨frames, ready, allocated, _⟩ := source_decl_bindings sourceBody singleton decls w
  exact ⟨frames, ready, allocated⟩

/-- Declaration-only programs link in every world to the same public cell table. -/
theorem declarations_same_exports_of_order_kept {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} (valid : EsmSplit.Valid B S P)
    (footprints : IndexedExportCells.Footprints body B) (complete : IndexedPlanCells.DefinitionsPresent body B)
    {sourceProgram : Program} {source : Module} (sourceBody : source.body = body)
    (sourceImports : source.imports = []) (sourceFind : sourceProgram.find sourceProgram.entry = some source)
    (singleton : sourceProgram.modules = [source])
    (ordered : body.Pairwise (fun a b => a.1 ≤ b.1)) (kept : SparseProjectedOrder.OrderKept body P)
    (sourceDecls : ∀ p ∈ body, ∃ kind name init, p.2 = .decl kind name init)
    {row : EsmSplit.LocalExportUnit} (sourceRow : InteriorExportNames.SourceRow source row)
    (rowFootprint : row.FootprintMatches (B.stmt row.ordinal))
    {owner : EsmSplit.Module} (ownerIn : owner ∈ P.mods) (rowWithin : row.ordinal < B.length)
    (planEntry : P.find P.entry = some owner)
    (authored : S.claimants B row.ordinal = [owner.name]) (unitsUnique : owner.stmts.Nodup)
    (glue : owner.exports = [])
    (onlyRow : ∀ id ∈ owner.stmts, id ≠ row.ordinal → (B.stmt id).exports = [])
    (sourceDefs : ∀ pair ∈ row.names, ∃ id, id < B.length ∧ pair.2 ∈ (B.stmt id).defs) :
    LinkedBehavior.SameExports sourceProgram (splitProgramIndexed body B P) := by
  intro w
  obtain ⟨frames, ready, allocated⟩ := source_decl_allocation sourceBody singleton sourceDecls w
  have regions : (splitProgramIndexed body B P).regions = sourceProgram.regions := by
    rw [SparseProjectedOrder.split_indexed_regions_eq_source kept,
      IndexedEntryLink.singleton_source_regions singleton, sourceBody]
  obtain ⟨targetLinked, table, sourceLink, targetLink, _, sourceTable, targetTable, _⟩ :=
    IndexedEntryLink.row_actual_links_from_allocation valid footprints complete sourceBody sourceImports sourceFind
      ordered sourceDecls (SparseProjectedOrder.split_indexed_tops_eq_source kept ordered) regions allocated
      sourceRow rowFootprint ownerIn rowWithin planEntry authored unitsUnique glue onlyRow sourceDefs
  refine ⟨table, table, ?_, ?_, List.Perm.refl _⟩
  · rw [LinkedBehavior.exports, sourceLink]
    change (EntryExports.resolve sourceProgram.entry _).toOption = some table
    rw [sourceTable]
    rfl
  · rw [LinkedBehavior.exports, targetLink]
    change (EntryExports.resolve (splitProgramIndexed body B P).entry targetLinked).toOption = some table
    rw [targetTable]
    rfl

/-- All worlds link to the very same ordered public cell table. -/
theorem same_exports_of_order_kept {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
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
    LinkedBehavior.SameExports sourceProgram (splitProgramIndexed body B P) := by
  have decls : ∀ p ∈ body, ∃ kind name init, p.2 = .decl kind name init := by
    intro p member
    obtain ⟨name, init, shape⟩ := sourceConsts p member
    exact ⟨.const, name, init, shape⟩
  exact declarations_same_exports_of_order_kept valid footprints complete sourceBody sourceImports sourceFind
    singleton ordered kept decls sourceRow rowFootprint ownerIn rowWithin planEntry authored unitsUnique
    glue onlyRow sourceDefs

#print axioms source_decl_bindings
#print axioms source_decl_allocation
#print axioms source_allocation
#print axioms declarations_same_exports_of_order_kept
#print axioms same_exports_of_order_kept

end CoreJS.IndexedConstLink
