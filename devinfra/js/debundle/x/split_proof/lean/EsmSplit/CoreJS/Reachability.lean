import EsmSplit.CoreJS.OpaqueFrame

/-!
# What an opaque request can reach

Objects and bindings occupy separate stores, even when their numeric addresses coincide. The graph follows every
stored pointer, including accessors and closure captures. Its roots are the supplied bindings and previously handed
objects. These roots suffice for binding requests and own keys; operations that introduce built-in objects need
additional roots. No assertion about callback execution or prototype-search fuel is made here.
-/

namespace CoreJS

variable {W : World}

inductive Edge (s : State W) : HeapNode → HeapNode → Prop where
  | obj {a o node} : s.objAt a = some o → node ∈ o.nodes → Edge s (.obj a) node
  | cell {a b node} : s.cellAt a = some b → node ∈ b.nodes → Edge s (.cell a) node

/-- Least reachability closure, rooted only in the capabilities this request family actually uses. -/
inductive Reachable (s : State W) (cells : List (Option Addr)) : HeapNode → Prop where
  | handed {a} : a ∈ s.world.handed → Reachable s cells (.obj a)
  | supplied {a} : some a ∈ cells → Reachable s cells (.cell a)
  | edge {a b} : Reachable s cells a → Edge s a b → Reachable s cells b

/-- A closed envelope of reachable nodes. Keeping this witness fixed makes mutation proofs compositional. -/
structure Access (cells : List (Option Addr)) (A : HeapNode → Prop) (s : State W) : Prop where
  handed : ∀ a ∈ s.world.handed, A (.obj a)
  supplied : ∀ a, some a ∈ cells → A (.cell a)
  closed : ∀ a b, A a → Edge s a b → A b

/-- Enumerate the allocated records by their real addresses, so protected sources can be skipped. -/
private def rowsClosed (rows : Array (Array α)) (node : Addr → HeapNode) (edges : α → List HeapNode)
    (allowed : HeapNode → Bool) : Bool :=
  rows.zipIdx.all fun (row, region) => row.zipIdx.all fun (item, index) =>
    !allowed (node ⟨region, index⟩) || (edges item).all allowed

private theorem rowsClosed_edges {rows : Array (Array α)} {node edges allowed}
    (ok : rowsClosed rows node edges allowed = true) {a : Addr} {item : α}
    (found : (rows[a.region]?).bind (·[a.index]?) = some item) (src : allowed (node a) = true) :
    ∀ n ∈ edges item, allowed n = true := by
  obtain ⟨row, hr, hi⟩ := Option.bind_eq_some_iff.mp found
  have allRows := Array.all_eq_true_iff_forall_mem.mp ok
  have allItems := allRows (row, a.region) (Array.mem_zipIdx_iff_getElem?.2 hr)
  have entry := Array.all_eq_true_iff_forall_mem.mp allItems (item, a.index) (Array.mem_zipIdx_iff_getElem?.2 hi)
  simpa [src, List.all_eq_true] using entry

private theorem rowsClosed_of_edges {rows : Array (Array α)} {node edges allowed}
    (closed : ∀ (a : Addr) item, (rows[a.region]?).bind (·[a.index]?) = some item →
      allowed (node a) = true → ∀ n ∈ edges item, allowed n = true) :
    rowsClosed rows node edges allowed = true := by
  apply Array.all_eq_true_iff_forall_mem.mpr
  intro (row, region) hrow
  apply Array.all_eq_true_iff_forall_mem.mpr
  intro (item, index) hitem
  have hr : rows[region]? = some row := Array.mem_zipIdx_iff_getElem?.1 hrow
  have hi : row[index]? = some item := Array.mem_zipIdx_iff_getElem?.1 hitem
  cases h : allowed (node ⟨region, index⟩) with
  | false => simp [h]
  | true =>
    simp only [h, Bool.not_true, Bool.false_or]
    exact List.all_eq_true.mpr (closed ⟨region, index⟩ item (by simp [hr, hi]) h)

/-- Check the actual handed and supplied capabilities and all outgoing edges in a concrete heap.
The world's private state is never inspected, so the same check works on a linked state for arbitrary worlds. -/
def accessOk (s : State W) (cells : List (Option Addr)) (allowed : HeapNode → Bool) : Bool :=
  s.world.handed.all (fun a => allowed (.obj a)) &&
    cells.all (fun cell => cell.all (fun a => allowed (.cell a))) &&
    rowsClosed s.heap .obj Obj.nodes allowed && rowsClosed s.cells .cell Binding.nodes allowed

theorem accessOk_sound {s : State W} {cells allowed} (ok : accessOk s cells allowed = true) :
    Access cells (fun n => allowed n = true) s := by
  simp only [accessOk, Bool.and_eq_true] at ok
  obtain ⟨⟨⟨handed, supplied⟩, objects⟩, bindings⟩ := ok
  refine ⟨Array.all_eq_true_iff_forall_mem.mp handed, ?_, ?_⟩
  · intro a ha; exact List.all_eq_true.mp supplied (some a) ha
  · intro src dst hsrc edge
    cases edge with
    | obj ho hm => exact rowsClosed_edges objects ho hsrc _ hm
    | cell hc hm => exact rowsClosed_edges bindings hc hsrc _ hm

theorem accessOk_iff {s : State W} {cells allowed} :
    accessOk s cells allowed = true ↔ Access cells (fun n => allowed n = true) s := by
  refine ⟨accessOk_sound, fun hs => ?_⟩
  simp only [accessOk, Bool.and_eq_true]
  refine ⟨⟨⟨Array.all_eq_true_iff_forall_mem.mpr hs.handed, List.all_eq_true.mpr ?_⟩, ?_⟩, ?_⟩
  · intro cell hc; cases cell with
    | none => rfl
    | some a => exact hs.supplied a hc
  · exact rowsClosed_of_edges fun _ _ ho ha _ hm => hs.closed _ _ ha (.obj ho hm)
  · exact rowsClosed_of_edges fun _ _ hc ha _ hm => hs.closed _ _ ha (.cell hc hm)

/-- The envelope excludes the changed region and every binding an allocation initializer reads. -/
structure Away (r : Nat) (R : Addr → Prop) (A : HeapNode → Prop) : Prop where
  region : ∀ node, A node → node.addr.region ≠ r
  reads : ∀ a, A (.cell a) → ¬R a

def SafeVal (A : HeapNode → Prop) (v : Val) : Prop := ∀ node ∈ v.nodes, A node

theorem safeVal_obj {A : HeapNode → Prop} {a : Addr} : SafeVal A (.obj a) ↔ A (.obj a) := by
  simp [SafeVal, Val.nodes]

theorem Access.reachable {cells : List (Option Addr)} {A : HeapNode → Prop} {s : State W}
    (h : Access cells A s) {node : HeapNode} (hr : Reachable s cells node) : A node := by
  induction hr with
  | handed ha => exact h.handed _ ha
  | supplied ha => exact h.supplied _ ha
  | edge _ he ih => exact h.closed _ _ ih he

theorem access_reachable (s : State W) (cells : List (Option Addr)) : Access cells (Reachable s cells) s :=
  ⟨fun _ h => .handed h, fun _ h => .supplied h, fun _ _ h e => .edge h e⟩

theorem Access.value {cells : List (Option Addr)} {A : HeapNode → Prop} {s : State W}
    (h : Access cells A s) {a : Addr} {b : Binding} {v : Val} (ha : A (.cell a))
    (hb : s.cellAt a = some b) (hv : b.value = some v) : SafeVal A v := by
  intro node hn
  apply h.closed (.cell a) node ha
  apply Edge.cell hb
  simpa only [Binding.nodes, hv, Option.toList_some, List.flatMap_cons, List.flatMap_nil, List.append_nil] using hn

/-- Replacing an unreachable region does not introduce new reachable nodes. -/
theorem Access.put {cells : List (Option Addr)} {A : HeapNode → Prop} {s : State W} {r : Nat} {R : Addr → Prop}
    (h : Access cells A s) (away : Away r R A) (H : Array Obj) (C : Array Binding) :
    Access cells A (s.put r H C) := by
  refine ⟨h.handed, h.supplied, fun a b ha he => h.closed a b ha ?_⟩
  cases he with
  | obj ho hn =>
    have hr := away.region _ ha
    simp only [HeapNode.addr] at hr
    rw [State.objAt_put, ite_eq_right hr] at ho
    exact .obj ho hn
  | cell hc hn =>
    have hr := away.region _ ha
    simp only [HeapNode.addr] at hr
    rw [State.cellAt_put, ite_eq_right hr] at hc
    exact .cell hc hn

/-- Updating only the world's private state or its handle table does not change graph edges. -/
theorem Access.world {cells : List (Option Addr)} {A : HeapNode → Prop} {s : State W}
    (h : Access cells A s) (host : Host W) (hh : ∀ a ∈ host.handed, A (.obj a)) :
    Access cells A { s with world := host } := by
  refine ⟨hh, h.supplied, fun a b ha he => h.closed a b ha ?_⟩
  cases he with
  | obj ho hn => exact .obj ho hn
  | cell hc hn => exact .cell hc hn

/-- The actual state update used by `setCell`. It cannot create a binding at a missing address. -/
def State.assign (s : State W) (a : Addr) (v : Val) : State W :=
  { s with cells := s.cells.modify a.region (·.modify a.index fun b => { b with value := some v }) }

theorem State.cellAt_assign_self (s : State W) (a : Addr) (v : Val) :
    (s.assign a v).cellAt a = (s.cellAt a).map (fun b => { b with value := some v }) := by
  simp only [State.assign, State.cellAt, Array.getElem?_modify, ↓reduceIte]
  cases s.cells[a.region]? <;> simp [Array.getElem?_modify]

theorem State.cellAt_assign_other (s : State W) (a : Addr) (v : Val) (c : Addr) (hne : c ≠ a) :
    (s.assign a v).cellAt c = s.cellAt c := cellAt_modify s a _ c hne

/-- Writing a reachable value into a reachable binding keeps the same closed envelope. -/
theorem Access.assign {cells : List (Option Addr)} {A : HeapNode → Prop} {s : State W}
    (h : Access cells A s) (a : Addr) (v : Val) (hv : SafeVal A v) : Access cells A (s.assign a v) := by
  refine ⟨h.handed, h.supplied, fun node target hn he => ?_⟩
  cases he with
  | obj ho hm => exact h.closed _ _ hn (.obj ho hm)
  | @cell c binding target hb hm =>
    by_cases hc : c = a
    · subst c
      rw [State.cellAt_assign_self] at hb
      cases hcell : s.cellAt a with
      | none => simp [hcell] at hb
      | some b =>
        simp only [hcell, Option.map_some, Option.some.injEq] at hb
        subst hb
        exact hv target (by simpa [Binding.nodes] using hm)
    · rw [State.cellAt_assign_other _ _ _ _ hc] at hb
      exact h.closed _ _ hn (.cell hb hm)

/-- The store update frames every other region and each separately protected read binding. -/
theorem State.assign_preserves {r : Nat} {R : Addr → Prop} {A : HeapNode → Prop}
    (away : Away r R A) {a : Addr} (ha : A (.cell a)) (s : State W) (v : Val) :
    Preserves r R s (s.assign a v) := by
  have har : a.region ≠ r := away.region _ ha
  refine ⟨rfl, ?_, fun c hc => ?_, rfl⟩
  · simp [State.assign, Array.getElem?_modify, har]
  · apply State.cellAt_assign_other
    intro hca
    exact away.reads a ha (hca ▸ hc)

/-- An update outside a replaced region commutes with that replacement. -/
theorem State.assign_put {r : Nat} (s : State W) (a : Addr) (v : Val) (ha : a.region ≠ r)
    (H : Array Obj) (C : Array Binding) :
    (s.put r H C).assign a v = (s.assign a v).put r H C := by
  have hh : (s.cells.set! r C)[a.region]? = s.cells[a.region]? := by
    simp [Ne.symm ha]
  have he : (s.cells.set! r C).modify a.region (·.modify a.index fun b => { b with value := some v }) =
      (s.cells.modify a.region (·.modify a.index fun b => { b with value := some v })).set! r C := by
    rw [modify_eq_set! _ _ _ #[], modify_eq_set! _ _ _ #[], hh, set!_comm _ (Ne.symm ha)]
  simp only [State.assign, State.put]
  rw [he]

end CoreJS
