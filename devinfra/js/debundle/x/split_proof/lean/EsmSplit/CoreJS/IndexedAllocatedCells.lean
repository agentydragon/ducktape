import EsmSplit.CoreJS.IndexedConstructorCells

/-! Allocation facts for indexed source scopes. `FramesOf` alone records names;
these results use the actual successful `declareTops` allocation. -/

namespace CoreJS.IndexedAllocatedCells

variable {W : World}

/-- A completed allocation sequence retains every cell it returned. -/
theorem newCells_present (bs : List (Name × Binding)) {s s' : State W} {frame : List (Name × Addr)}
    (h : newCells bs s = .ok frame s') : ∀ p ∈ frame, ∃ b, s'.cellAt p.2 = some b :=
  (newCells_allocation bs h).2.2.present

/-- Allocating top-level frames preserves cells that existed before allocation. -/
theorem declareTops_preserves_cells : ∀ (tops : List (Nat × Stmt)) {s s' : State W}
    {frames : List (Nat × List (Name × Addr))},
    declareTops tops s = .ok frames s' → ∀ a b, s.cellAt a = some b → s'.cellAt a = some b
  | [], s, s', frames, h => by unfold declareTops at h; cases h; exact fun _ _ v => v
  | (id, stmt) :: rest, s, s', frames, h => by
    unfold declareTops at h
    obtain ⟨frame, t1, hf, h⟩ := bind_ok h
    obtain ⟨tail, t2, ht, h⟩ := bind_ok h
    cases h
    obtain ⟨u, hu, rfl⟩ := inRegion_ok_state hf
    have keep := newCells_keeps stmt.bindings (Extends.refl { s with region := id + 1 })
    rw [hu] at keep
    intro a b present
    apply declareTops_preserves_cells rest ht a b
    exact keep.cells a b present

/-- Every address in a successfully allocated top-level frame is present in the
resulting CoreJS state, including after the remaining frames are allocated. -/
theorem declareTops_present : ∀ (tops : List (Nat × Stmt)) {s s' : State W}
    {frames : List (Nat × List (Name × Addr))},
    declareTops tops s = .ok frames s' → ∀ q ∈ frames, ∀ p ∈ q.2, ∃ b, s'.cellAt p.2 = some b
  | [], s, s', frames, h => by unfold declareTops at h; cases h; simp
  | (id, stmt) :: rest, s, s', frames, h => by
    unfold declareTops at h
    obtain ⟨frame, t1, hf, h⟩ := bind_ok h
    obtain ⟨tail, t2, ht, h⟩ := bind_ok h
    cases h
    intro q hq p hp
    rcases List.mem_cons.1 hq with rfl | hq
    · obtain ⟨u, hu, rfl⟩ := inRegion_ok_state hf
      obtain ⟨b, present⟩ := newCells_present stmt.bindings hu p hp
      exact ⟨b, declareTops_preserves_cells rest ht p.2 b present⟩
    · exact declareTops_present rest ht q hq p hp

/-- A present scope lookup backed by successful allocation names an actual
cell in the allocation result, not just an address in a name-only frame. -/
theorem scope_cell_present {tops : List (Nat × Stmt)} {s s' : State W}
    {frames : List (Nat × List (Name × Addr))} (allocated : declareTops tops s = .ok frames s')
    {m : Module} {name : Name} {a : Addr} (found : (m.scope frames).lookup name = some a) :
    ∃ b, s'.cellAt a = some b := by
  obtain ⟨⟨id, stmt⟩, _, member⟩ := List.mem_flatMap.1 (lookup_mem found)
  cases hf : frames.lookup id with
  | none => simp [hf] at member
  | some frame =>
    have inFrame : (name, a) ∈ frame := by simpa only [hf, Option.getD_some] using member
    exact declareTops_present tops allocated (id, frame) (lookup_mem hf) (name, a) inFrame

/-- An authored interior export row retains its original owner and ordered
aliases, and every public local resolves in the source and constructed owner
to the same address allocated by the successful source declaration pass. The
result state here is the state immediately after `declareTops`; later linking
and initialization still require their own preservation argument. -/
theorem row_allocated_cells {body : List (Nat × Stmt)} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} (valid : EsmSplit.Valid B S P)
    (footprints : IndexedExportCells.Footprints body B) (complete : IndexedPlanCells.DefinitionsPresent body B)
    {s s' : State W} {frames : List (Nat × List (Name × Addr))}
    (allocated : declareTops body s = .ok frames s')
    {source : Module} (sourceBody : source.body = body) {row : EsmSplit.LocalExportUnit}
    (sourceRow : InteriorExportNames.SourceRow source row)
    (rowFootprint : row.FootprintMatches (B.stmt row.ordinal))
    {owner : EsmSplit.Module} (ownerIn : owner ∈ P.mods) (rowWithin : row.ordinal < B.length)
    (authored : S.claimants B row.ordinal = [owner.name]) (unitsUnique : owner.stmts.Nodup)
    (glue : owner.exports = [])
    (onlyRow : ∀ id ∈ owner.stmts, id ≠ row.ordinal → (B.stmt id).exports = [])
    (sourceDefs : ∀ pair ∈ row.names, ∃ id, id < B.length ∧ pair.2 ∈ (B.stmt id).defs) :
    ∃ cells, row.ordinal ∈ owner.stmts ∧
      (moduleOfIndexed body B P owner).exports = source.exports ∧
      (splitProgramIndexed body B P).imported frames (moduleOfIndexed body B P owner).imports = .ok cells ∧
      ∀ pair ∈ row.names, ∃ a b, (source.scope frames).lookup pair.2 = some a ∧
        ((moduleOfIndexed body B P owner).scope frames ++ cells).lookup pair.2 = some a ∧
        s'.cellAt a = some b := by
  obtain ⟨cells, owned, exports, imported, agree⟩ :=
    InteriorPlanExports.row_scope_cells valid (IndexedConstructorCells.projection body B P) footprints complete
      (declareTops_names body allocated) sourceBody sourceRow rowFootprint ownerIn rowWithin authored
      unitsUnique glue onlyRow sourceDefs
  refine ⟨cells, owned, exports, imported, ?_⟩
  intro pair member
  obtain ⟨a, original, generated⟩ := agree pair member
  obtain ⟨b, present⟩ := scope_cell_present allocated original
  exact ⟨a, b, original, generated, present⟩

#print axioms scope_cell_present
#print axioms row_allocated_cells

end CoreJS.IndexedAllocatedCells
