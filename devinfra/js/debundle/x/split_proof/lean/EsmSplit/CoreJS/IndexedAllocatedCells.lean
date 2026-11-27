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

/-- Per-row binding facts from the real top-level declaration pass, in the
same order as the original statement identities. -/
inductive TopBindingAllocation (s : State W) : List (Nat × Stmt) → List (Nat × List (Name × Addr)) → Prop
  | nil : TopBindingAllocation s [] []
  | cons {id : Nat} {stmt : Stmt} {frame : List (Name × Addr)} {rest frames}
      (row : BindingAllocation s stmt.bindings frame) (tail : TopBindingAllocation s rest frames) :
      TopBindingAllocation s ((id, stmt) :: rest) ((id, frame) :: frames)

/-- A later state that preserves cells preserves each recorded binding. -/
private theorem bindingAllocation_preserved {s t : State W} {bs : List (Name × Binding)}
    {frame : List (Name × Addr)} (h : BindingAllocation s bs frame)
    (keep : ∀ a b, s.cellAt a = some b → t.cellAt a = some b) : BindingAllocation t bs frame := by
  induction h with
  | nil => exact .nil
  | cons value tail ih => exact .cons (keep _ _ value) ih

/-- Successful `declareTops` preserves each allocated binding through every
later row, even when statement identities are sparse or names repeat. -/
theorem declareTops_bindings : ∀ (tops : List (Nat × Stmt)) {s s' : State W}
    {frames : List (Nat × List (Name × Addr))},
    declareTops tops s = .ok frames s' → TopBindingAllocation s' tops frames
  | [], s, s', frames, h => by unfold declareTops at h; cases h; exact .nil
  | (id, stmt) :: rest, s, s', frames, h => by
    unfold declareTops at h
    obtain ⟨frame, t1, hf, h⟩ := bind_ok h
    obtain ⟨tail, t2, ht, h⟩ := bind_ok h
    cases h
    obtain ⟨u, hu, rfl⟩ := inRegion_ok_state hf
    have row := (newCells_allocation stmt.bindings hu).2.2
    have kept := bindingAllocation_preserved row (fun a b present =>
        declareTops_preserves_cells rest ht a b present)
    exact .cons kept (declareTops_bindings rest ht)

/-- Every recorded row address remains present in the final state. -/
theorem TopBindingAllocation.present {s : State W} {tops : List (Nat × Stmt)}
    {frames : List (Nat × List (Name × Addr))} (facts : TopBindingAllocation s tops frames) :
    ∀ q ∈ frames, ∀ p ∈ q.2, ∃ b, s.cellAt p.2 = some b := by
  induction facts with
  | nil => simp
  | @cons id stmt frame rest frames row tail ih =>
    intro q hq p hp
    rcases List.mem_cons.1 hq with rfl | hq
    · exact row.present p hp
    · exact ih q hq p hp

/-- Every address in a successfully allocated top-level frame remains present. -/
theorem declareTops_present (tops : List (Nat × Stmt)) {s s' : State W}
    {frames : List (Nat × List (Name × Addr))} (h : declareTops tops s = .ok frames s') :
    ∀ q ∈ frames, ∀ p ∈ q.2, ∃ b, s'.cellAt p.2 = some b :=
  (declareTops_bindings tops h).present

/-- A source row and one of its actual declared bindings retain their
corresponding frame and binding. Duplicate IDs and names are not collapsed. -/
theorem TopBindingAllocation.binding_present {s : State W} {tops : List (Nat × Stmt)}
    {frames : List (Nat × List (Name × Addr))} (facts : TopBindingAllocation s tops frames)
    {id : Nat} {stmt : Stmt} {name : Name} {binding : Binding}
    (row : (id, stmt) ∈ tops) (declared : (name, binding) ∈ stmt.bindings) :
    ∃ frame addr, (id, frame) ∈ frames ∧ (name, addr) ∈ frame ∧ s.cellAt addr = some binding := by
  induction facts with
  | nil => simp at row
  | @cons currentId currentStmt frame rest frames head tail ih =>
    rcases List.mem_cons.1 row with same | later
    · cases same
      obtain ⟨addr, member, value⟩ := head.binding_present declared
      exact ⟨frame, addr, List.mem_cons_self, member, value⟩
    · obtain ⟨foundFrame, addr, found, member, value⟩ := ih later
      exact ⟨foundFrame, addr, List.mem_cons_of_mem _ found, member, value⟩

/-- Every source binding has a real final address in its original identity's
region after the complete allocation pass, regardless of ID gaps. -/
theorem declareTops_binding {tops : List (Nat × Stmt)} {s s' : State W}
    {frames : List (Nat × List (Name × Addr))} (allocated : declareTops tops s = .ok frames s')
    {id : Nat} {stmt : Stmt} {name : Name} {binding : Binding}
    (row : (id, stmt) ∈ tops) (declared : (name, binding) ∈ stmt.bindings) :
    ∃ frame addr, (id, frame) ∈ frames ∧ (name, addr) ∈ frame ∧
      s'.cellAt addr = some binding ∧ addr.region = id + 1 := by
  obtain ⟨frame, addr, inFrames, inFrame, value⟩ :=
    (declareTops_bindings tops allocated).binding_present row declared
  exact ⟨frame, addr, inFrames, inFrame, value, declareTops_regions tops allocated (id, frame) inFrames
    (name, addr) inFrame⟩

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

#print axioms declareTops_binding
#print axioms declareTops_bindings
#print axioms declareTops_present
#print axioms scope_cell_present
#print axioms row_allocated_cells

end CoreJS.IndexedAllocatedCells
