import EsmSplit.CoreJS.Move

/-!
# Distinct names resolve to distinct linked cells

`FramesOf` records names, not cell allocation. The results below instead use a successful `declareTops` trace.
Within a frame every `newCell` allocates a fresh address. Frames selected by different statement identities have
different regions. These facts hold from an arbitrary initial state, including nonempty stores.
-/

namespace CoreJS

variable {W : World}

/-- Every address in a frame identifies just one entry, even if its input had repeated names. -/
def DistinctCells (frame : List (Name × Addr)) : Prop :=
  ∀ p ∈ frame, ∀ q ∈ frame, p.2 = q.2 → p = q

theorem newCell_value {b : Binding} {s s' : State W} {a : Addr} (h : newCell b s = .ok a s') :
    s'.cellAt a = some b := by
  unfold newCell at h
  split at h
  · rename_i bs hbs
    cases h
    rw [cellAt_push hbs]
    simp
  · cases h

/-- Ordered allocation facts for each input binding and returned address. -/
inductive BindingAllocation (s : State W) : List (Name × Binding) → List (Name × Addr) → Prop
  | nil : BindingAllocation s [] []
  | cons {name : Name} {binding : Binding} {addr : Addr} {rest frame}
      (value : s.cellAt addr = some binding) (tail : BindingAllocation s rest frame) :
      BindingAllocation s ((name, binding) :: rest) ((name, addr) :: frame)

/-- Every recorded allocation names a cell containing its original binding. -/
theorem BindingAllocation.present {s : State W} {bs : List (Name × Binding)} {frame : List (Name × Addr)}
    (h : BindingAllocation s bs frame) : ∀ p ∈ frame, ∃ b, s.cellAt p.2 = some b := by
  induction h with
  | nil => simp
  | @cons name binding addr rest frame value tail ih =>
    intro p hp
    rcases List.mem_cons.1 hp with rfl | hp
    · exact ⟨binding, value⟩
    · exact ih p hp

/-- Each input binding has a corresponding returned address containing that
binding. Repeated names remain separate list entries. -/
theorem BindingAllocation.binding_present {s : State W} {bs : List (Name × Binding)} {frame : List (Name × Addr)}
    (h : BindingAllocation s bs frame) {name : Name} {binding : Binding} (member : (name, binding) ∈ bs) :
    ∃ addr, (name, addr) ∈ frame ∧ s.cellAt addr = some binding := by
  induction h with
  | nil => simp at member
  | @cons x b a rest frame value tail ih =>
    rcases List.mem_cons.1 member with eq | member
    · cases eq
      exact ⟨a, List.mem_cons_self, value⟩
    · obtain ⟨addr, found, cell⟩ := ih member
      exact ⟨addr, List.mem_cons_of_mem _ found, cell⟩

/-- A successful allocation extends the old state, preserves the order and names
of the input bindings, and records each new binding at its fresh address. This
works for nonempty regions and arbitrary binding values and mutability. -/
theorem newCells_allocation : ∀ (bs : List (Name × Binding)) {s s' : State W}
    {frame : List (Name × Addr)}, newCells bs s = .ok frame s' →
      Extends s s' ∧ (∀ p ∈ frame, s.cellAt p.2 = none) ∧
      BindingAllocation s' bs frame
  | [], s, s', frame, h => by
    unfold newCells at h
    cases h
    exact ⟨Extends.refl s, by simp, .nil⟩
  | (x, b) :: rest, s, s', frame, h => by
    unfold newCells at h
    obtain ⟨a, t1, hc, h⟩ := bind_ok h
    obtain ⟨tail, t2, ht, h⟩ := bind_ok h
    cases h
    obtain ⟨extendsTail, freshTail, valuesTail⟩ := newCells_allocation rest ht
    have extendsHead := (newCell_ok hc).2
    refine ⟨extendsHead.trans extendsTail, ?_, ?_⟩
    · intro p hp
      rcases List.mem_cons.1 hp with rfl | hp
      · exact (newCell_ok hc).1
      · exact extendsHead.freshCell (freshTail p hp)
    · exact .cons (extendsTail.cells a b (newCell_value hc)) valuesTail

/-- Every returned cell was absent before the allocation sequence began. -/
theorem newCells_fresh (bs : List (Name × Binding)) {s s' : State W} {frame : List (Name × Addr)}
    (h : newCells bs s = .ok frame s') : ∀ p ∈ frame, s.cellAt p.2 = none :=
  (newCells_allocation bs h).2.1

theorem newCells_distinct : ∀ (bs : List (Name × Binding)) {s s' : State W} {frame : List (Name × Addr)},
    newCells bs s = .ok frame s' → DistinctCells frame
  | [], s, s', frame, h => by unfold newCells at h; cases h; simp [DistinctCells]
  | (x, b) :: rest, s, s', frame, h => by
    unfold newCells at h
    obtain ⟨c, t1, hc, h⟩ := bind_ok h
    obtain ⟨tail, t2, ht, h⟩ := bind_ok h
    cases h
    intro p hp q hq he
    rcases List.mem_cons.1 hp with rfl | hp
    · rcases List.mem_cons.1 hq with rfl | hq
      · rfl
      · have hf := newCells_fresh rest ht q hq
        rw [← he, newCell_value hc] at hf
        cases hf
    · rcases List.mem_cons.1 hq with rfl | hq
      · have hf := newCells_fresh rest ht p hp
        rw [he, newCell_value hc] at hf
        cases hf
      · exact newCells_distinct rest ht p hp q hq he

/-- The within-frame invariant follows from actual allocation, not merely from a list of declared names. -/
theorem declareTops_distinct : ∀ (tops : List (Nat × Stmt)) {s s' : State W} {frames : List (Nat × List (Name × Addr))},
    declareTops tops s = .ok frames s' → ∀ q ∈ frames, DistinctCells q.2
  | [], s, s', frames, h => by unfold declareTops at h; cases h; simp
  | (id, stmt) :: rest, s, s', frames, h => by
    unfold declareTops at h
    obtain ⟨frame, t1, hf, h⟩ := bind_ok h
    obtain ⟨tail, t2, ht, h⟩ := bind_ok h
    cases h
    intro q hq
    rcases List.mem_cons.1 hq with rfl | hq
    · obtain ⟨u, u', hu⟩ := inRegion_ok hf
      exact newCells_distinct _ hu
    · exact declareTops_distinct rest ht q hq

private theorem scope_mem_frame {md : Module} {frames : List (Nat × List (Name × Addr))} {x : Name} {a : Addr}
    (h : (x, a) ∈ md.scope frames) : ∃ id frame, frames.lookup id = some frame ∧ (x, a) ∈ frame := by
  obtain ⟨⟨id, stmt⟩, _, hm⟩ := List.mem_flatMap.1 h
  cases hf : frames.lookup id with
  | none => simp [hf] at hm
  | some frame => exact ⟨id, frame, hf, by simpa only [hf, Option.getD_some] using hm⟩

/-- A successful linker allocation makes the local scope injective in its cell addresses. No empty-store or
unique-name premise is needed: repeated names can shadow, but one allocated cell cannot acquire two names. -/
theorem declareTops_scope_injective {tops : List (Nat × Stmt)} {s s' : State W}
    {frames : List (Nat × List (Name × Addr))} (h : declareTops tops s = .ok frames s') (md : Module)
    {x y : Name} {a : Addr} (hx : (md.scope frames).lookup x = some a) (hy : (md.scope frames).lookup y = some a) :
    x = y := by
  obtain ⟨i, fi, hfi, hxi⟩ := scope_mem_frame (lookup_mem hx)
  obtain ⟨j, fj, hfj, hyj⟩ := scope_mem_frame (lookup_mem hy)
  have hi := declareTops_regions _ h (i, fi) (lookup_mem hfi) (x, a) hxi
  have hj := declareTops_regions _ h (j, fj) (lookup_mem hfj) (y, a) hyj
  have hij : i = j := Nat.add_right_cancel (hi.symm.trans hj)
  subst j
  have hframes : fi = fj := Option.some.inj (hfi.symm.trans hfj)
  subst fj
  exact congrArg Prod.fst (declareTops_distinct _ h (i, fi) (lookup_mem hfi) (x, a) hxi (y, a) hyj rfl)

/-- The bundle environment used by the reordering theorem inherits the allocation guarantee. -/
theorem declareTops_bundleEnv_injective {ss : List Stmt} {s s' : State W}
    {frames : List (Nat × List (Name × Addr))}
    (h : declareTops ((List.range ss.length).zip ss) s = .ok frames s')
    {x y : Name} {a : Addr}
    (hx : (bundleEnv ss frames).vars.lookup x = some a) (hy : (bundleEnv ss frames).vars.lookup y = some a) : x = y := by
  apply declareTops_scope_injective h (bundleModule ss)
  · simpa only [bundleEnv, List.append_nil] using hx
  · simpa only [bundleEnv, List.append_nil] using hy

/-- Distinct names in a linked bundle cannot satisfy the same address predicate. -/
theorem declareTops_bundleEnv_disjoint {ss : List Stmt} {s s' : State W}
    {frames : List (Nat × List (Name × Addr))}
    (h : declareTops ((List.range ss.length).zip ss) s = .ok frames s') {x y : Name} (hne : x ≠ y) :
    ∀ a, (bundleEnv ss frames).vars.lookup x = some a → (bundleEnv ss frames).vars.lookup y ≠ some a :=
  fun _ hx hy => hne (declareTops_bundleEnv_injective h hx hy)

namespace LinkedCellsExamples

private def multipleNames : Stmt := .opaque 0 [("x", .let_), ("y", .let_)] [] [] .world

private def initial (W : World) : State W where
  heap := #[#[], #[]]
  cells := #[#[], #[{ value := some (.bool false), mutable := true }]]
  region := 9
  out := #[]
  world := { state := W.init, handed := #[] }

private def allocated (W : World) : State W :=
  { initial W with
    cells := #[#[], #[{ value := some (.bool false), mutable := true },
      { value := none, mutable := true }, { value := none, mutable := true }]], region := 0 }

/-- Two names of the same statement get different indices after the preexisting cell. -/
theorem nonempty_allocation (W : World) :
    declareTops [(0, multipleNames)] (initial W) =
      .ok [(0, [("x", ⟨1, 1⟩), ("y", ⟨1, 2⟩)])] (allocated W) := rfl

private def aliased : List (Nat × List (Name × Addr)) := [(0, [("x", ⟨1, 0⟩), ("y", ⟨1, 0⟩)])]

/-- Name alignment alone admits a frame that aliases two distinct bindings. -/
theorem names_allow_alias : FramesOf [(0, multipleNames)] aliased := rfl

/-- Such a frame cannot be the result of any successful allocation, regardless of the initial state. -/
theorem allocation_rejects_alias (W : World) :
    ¬ ∃ s s' : State W, declareTops [(0, multipleNames)] s = .ok aliased s' := by
  rintro ⟨s, s', h⟩
  have he : ("x" : Name) = "y" := declareTops_scope_injective h (bundleModule [multipleNames])
    (a := ⟨1, 0⟩) rfl rfl
  exact (by decide : ("x" : Name) ≠ "y") he

end LinkedCellsExamples

#print axioms BindingAllocation.present
#print axioms BindingAllocation.binding_present
#print axioms newCells_allocation
#print axioms newCells_distinct
#print axioms declareTops_bundleEnv_injective
#print axioms LinkedCellsExamples.allocation_rejects_alias

end CoreJS
