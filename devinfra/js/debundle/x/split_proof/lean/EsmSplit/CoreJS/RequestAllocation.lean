import EsmSplit.CoreJS.RequestFrame

/-! # Opaque object and callback allocation

Allocation extends a closed capability envelope by the newly issued object address.
An intrinsic prototype must already belong to the envelope. -/

namespace CoreJS

variable {W : World}

theorem frames_alloc (r R) (o : Obj) : Frames r R (alloc o : M W Addr) := by
  constructor
  · intro s H C hr
    simp only [alloc, State.put]
    have hh : (s.heap.set! r H)[s.region]? = s.heap[s.region]? := by simp [Ne.symm hr]
    rw [hh]
    cases s.heap[s.region]? <;> simp only [Res.put, State.put, set!_comm _ (Ne.symm hr)]
  · intro s hr
    unfold alloc
    split
    · exact ⟨by simp [hr], rfl, fun _ _ => rfl, rfl⟩
    · exact Preserves.refl r R s

private def withObject (A : HeapNode → Prop) (a : Addr) (node : HeapNode) : Prop :=
  A node ∨ node = .obj a

private theorem access_alloc {cells A} {s t : State W} {o a} (hs : Access cells A s)
    (safe : ∀ node ∈ o.nodes, A node) (run : alloc o s = .ok a t) :
    Access cells (withObject A a) t := by
  unfold alloc at run
  split at run
  · rename_i objs hobjs
    cases run
    refine ⟨fun b hb => Or.inl (hs.handed b hb), fun b hb => Or.inl (hs.supplied b hb), ?_⟩
    intro node target hn he
    apply Or.inl
    cases he with
    | @obj b obj target ho hm =>
      rw [objAt_push hobjs] at ho
      split at ho
      · cases ho; exact safe _ hm
      · rename_i hne
        rcases hn with hn | hn
        · exact hs.closed _ _ hn (.obj ho hm)
        · cases hn; exact False.elim (hne rfl)
    | cell hc hm =>
      rcases hn with hn | hn
      · exact hs.closed _ _ hn (.cell hc hm)
      · cases hn
  · cases run

private def parentOf (proto : Option WVal) : M W (Option Addr) :=
  match proto with
  | none => pure (some objectProto)
  | some p => do return some (← requireObj "object prototype" (← fromW p))

private theorem frames_requireObj (r R) (v : Val) : Frames r R (requireObj "object prototype" v : M W Addr) := by
  cases v <;> first | exact frames_throwVal r R _ | exact frames_pure r R _ | exact frames_unknown r R _

private theorem frames_parent (r R) (proto : Option WVal) : Frames r R (parentOf proto : M W _) := by
  cases proto with
  | none => exact frames_pure r R _
  | some p => exact (frames_fromW r R p).bind' fun v => (frames_requireObj r R v).bind' fun _ => frames_pure r R _

private theorem parent_safe (r R cells A) (proto : Option WVal) (root : proto = none → A (.obj objectProto)) :
    ReachFrame r R cells A (fun parent => ∀ a ∈ parent, A (.obj a)) (parentOf (W := W) proto) := by
  cases proto with
  | none => exact reachFrame_pure r R cells A _ (by simpa using root rfl)
  | some p =>
    apply (reachFrame_fromW r R cells A p).bind
    intro v hv
    have req : ReachFrame r R cells A (fun a => A (.obj a)) (requireObj "object prototype" v : M W Addr) := by
      apply ReachFrame.of_frames (frames_requireObj r R v)
      intro s hs hr
      cases v <;> first | exact hs | exact ⟨safeVal_obj.1 hv, hs⟩ | exact ⟨by simp [SafeVal, Val.nodes], hs⟩
    exact req.bind fun a ha => reachFrame_pure r R cells A _ (by simpa using ha)

private theorem perform_newObject_eq (call : Call W) (construct : Val → List Val → M W Val)
    (code cells proto) : perform call construct code cells (.newObject proto) = (do
      let parent ← parentOf proto
      let a ← alloc { proto := parent, props := [] }
      let w ← toW (.obj a)
      pure (Told.value w)) := by
  have mapSome (m : M W Addr) : some <$> m = (m >>= fun a => pure (some a)) := rfl
  cases proto <;> simp only [perform, parentOf, mapSome, M.bind_assoc, M.pure_bind]

/-- All outcomes of a world object allocation frame a protected region and its read bindings. -/
theorem frames_newObject (r R) (call : Call W) (construct : Val → List Val → M W Val)
    (code cells proto) : Frames r R (perform call construct code cells (.newObject proto)) := by
  rw [perform_newObject_eq]
  exact (frames_parent r R proto).bind' fun parent =>
    (frames_alloc r R { proto := parent, props := [] }).bind' fun a =>
      (frames_toW r R (.obj a)).bind' fun w => frames_pure r R (Told.value w)

/-- The continuation may allocate further objects and extend the envelope again. -/
private theorem access_alloc_bind {r R cells A} (away : Away r R A) (o : Obj)
    (safe : ∀ node ∈ o.nodes, A node) (s : State W) (hs : Access cells A s) (hr : s.region ≠ r)
    (k : Addr → M W α) (P : α → Prop)
    (next : ∀ a t, Away r R (withObject A a) → Access cells (withObject A a) t → t.region ≠ r →
      ∃ B, (∀ node, withObject A a node → B node) ∧ Away r R B ∧ (k a t).Accessible cells B P) :
    ∃ B, (∀ node, A node → B node) ∧ Away r R B ∧ ((alloc o >>= k) s).Accessible cells B P := by
  rw [M.bind_apply]
  cases ha : alloc o s with
  | ok a u =>
    have har : a.region ≠ r := alloc_region ha ▸ hr
    have hb : Away r R (withObject A a) := by
      constructor
      · intro node hn; rcases hn with hn | rfl
        · exact away.region _ hn
        · exact har
      · intro b hb; rcases hb with hb | hb
        · exact away.reads _ hb
        · cases hb
    obtain ⟨B, incl, hB, rest⟩ := next a u hb (access_alloc hs safe ha) ((alloc_ok ha).2.region ▸ hr)
    exact ⟨B, fun _ hn => incl _ (Or.inl hn), hB, rest⟩
  | fuel | throw | unknown =>
    unfold alloc at ha
    split at ha <;> cases ha <;> exact ⟨A, fun _ h => h, away, hs⟩

/-- Publishing a fresh object whose outgoing edges are already accessible extends
the capability envelope by that object's address. -/
private theorem access_alloc_publish {r R cells A} (away : Away r R A) (o : Obj)
    (safe : ∀ node ∈ o.nodes, A node) (s : State W) (hs : Access cells A s) (hr : s.region ≠ r) :
    ∃ B, (∀ node, A node → B node) ∧ Away r R B ∧
      ((do
        let a ← alloc o
        let w ← toW (.obj a)
        pure (Told.value w)) s).Accessible cells B (fun _ => True) := by
  apply access_alloc_bind away o safe s hs hr
  intro a u hb hu hr'
  refine ⟨_, fun _ h => h, hb, ?_⟩
  have hv : SafeVal (withObject A a) (.obj a) := safeVal_obj.2 (Or.inr rfl)
  exact ((reachFrame_toW r R cells _ _ hv).bind fun w _ =>
    reachFrame_pure r R cells _ (Told.value w) trivial).accessible u hu hr'

/-- After the actual request, the accessible envelope can grow while still excluding the protected region
and read bindings. A supplied prototype must come through the handle table. Default construction needs
the intrinsic Object.prototype inside the initial envelope. -/
theorem access_newObject {r R cells A} (away : Away r R A) (call : Call W)
    (construct : Val → List Val → M W Val) (code proto) (root : proto = none → A (.obj objectProto))
    (s : State W) (hs : Access cells A s) (hr : s.region ≠ r) :
    ∃ B, (∀ node, A node → B node) ∧ Away r R B ∧
      (perform call construct code cells (.newObject proto) s).Accessible cells B (fun _ => True) := by
  rw [perform_newObject_eq]
  have hp := (parent_safe r R cells A proto root).accessible s hs hr
  have hframe := (frames_parent r R proto).preserves s hr
  rw [M.bind_apply]
  cases h : parentOf proto s with
  | fuel => exact ⟨A, fun _ h => h, away, trivial⟩
  | throw | unknown =>
    exact ⟨A, fun _ h => h, away, by simpa only [h, Res.Accessible] using hp⟩
  | ok parent t =>
    simp only [h, Res.Accessible] at hp
    simp only [h, Res.Preserves] at hframe
    let o : Obj := { proto := parent, props := [] }
    have safe : ∀ node ∈ o.nodes, A node := by
      cases parent <;> simp_all [o, Obj.nodes]
    exact access_alloc_publish away o safe t hp.2 (hframe.region ▸ hr)

/-- Allocating and handing the world a nonconstructible callback does not touch a protected region. -/
theorem frames_newCallback (r R) (call : Call W) (construct : Val → List Val → M W Val)
    (code cells part name length) : Frames r R (perform call construct code cells (.newFunction part name length false)) := by
  simp only [perform, makeOpaque, Bool.false_eq_true, ↓reduceIte, M.bind_assoc, M.pure_bind]
  exact (frames_alloc r R _).bind' fun a =>
    (frames_toW r R (.obj a)).bind' fun w => frames_pure r R (Told.value w)

private theorem opaqueCapturesAccessible {cells A} {s : State W} (hs : Access cells A s)
    (code part node) (h : node ∈ (Code.opaque code part cells).nodes) : A node := by
  obtain ⟨slot, hslot, eq⟩ := List.mem_filterMap.mp h
  cases slot with
  | none => simp at eq
  | some a =>
    simp at eq
    subst node
    exact hs.supplied a hslot

/-- The new callback exposes its prototype and precisely the supplied captures.
Missing bindings give the world no edge to a cell. -/
theorem access_newCallback {r R cells A} (away : Away r R A) (call : Call W)
    (construct : Val → List Val → M W Val) (code part name length) (root : A (.obj functionProto))
    (s : State W) (hs : Access cells A s) (hr : s.region ≠ r) :
    ∃ B, (∀ node, A node → B node) ∧ Away r R B ∧
      (perform call construct code cells (.newFunction part name length false) s).Accessible cells B (fun _ => True) := by
  simp only [perform, makeOpaque, Bool.false_eq_true, ↓reduceIte, M.bind_assoc, M.pure_bind]
  apply access_alloc_publish away _ _ s hs hr
  intro node hn
  simp [Obj.nodes, Property.nodes, Val.nodes] at hn
  exact hn.elim (fun h => h ▸ root) (opaqueCapturesAccessible hs code part node)

private theorem frames_modifyObj {r R} (a : Addr) (ha : a.region ≠ r) (f : Obj → Obj) :
    Frames r R (modifyObj a f : M W Unit) := by
  constructor
  · intro s H C hr
    have hh : (s.heap.set! r H)[a.region]? = s.heap[a.region]? := by simp [Ne.symm ha]
    simp only [modifyObj, Res.put, State.put]
    rw [modify_eq_set! _ _ _ #[], modify_eq_set! _ _ _ #[], hh, set!_comm _ (Ne.symm ha)]
  · intro s hr
    exact ⟨by simp [Array.getElem?_modify, ha], rfl, fun _ _ => rfl, rfl⟩

/-- Edges retained from the old record, and new edges into the accessible envelope,
are safe even when the update completes a cycle. -/
private theorem reachFrame_modifyObj {r R cells A} (away : Away r R A) (a : Addr) (ha : A (.obj a))
    (f : Obj → Obj) (edges : ∀ o n, n ∈ (f o).nodes → n ∈ o.nodes ∨ A n) :
    ReachFrame r R cells A (fun _ => True) (modifyObj a f : M W Unit) := by
  apply ReachFrame.of_frames (frames_modifyObj a (away.region _ ha) f)
  intro s hs hr
  refine ⟨trivial, hs.handed, hs.supplied, ?_⟩
  intro node target hn he
  cases he with
  | cell hc hm => exact hs.closed _ _ hn (.cell hc hm)
  | @obj b obj target ho hm =>
    by_cases hba : b = a
    · subst b
      have own : ({ s with heap := s.heap.modify a.region (·.modify a.index f) } : State W).objAt a =
          (s.objAt a).map f := by
        simp only [State.objAt, Array.getElem?_modify, ↓reduceIte]
        cases s.heap[a.region]? <;> simp [Array.getElem?_modify]
      rw [own] at ho
      cases old : s.objAt a with
      | none => simp [old] at ho
      | some o =>
        simp only [old, Option.map_some, Option.some.injEq] at ho
        subst obj
        exact (edges o target hm).elim (fun h => hs.closed _ _ hn (.obj old h)) id
    · rw [objAt_modify _ _ _ _ hba] at ho
      exact hs.closed _ _ hn (.obj ho hm)

/-- Constructible functions allocate their own prototype object and set both links
before publishing the function handle. -/
theorem frames_newConstructor (r R) (call : Call W) (construct : Val → List Val → M W Val)
    (code cells part name length) : Frames r R (perform call construct code cells (.newFunction part name length true)) := by
  simp only [perform, makeOpaque, ↓reduceIte, addPrototype, M.bind_assoc, M.pure_bind]
  apply (frames_alloc r R _).bind
  intro a ⟨s, t, hr, run⟩
  exact (frames_alloc r R _).bind' fun p =>
    (frames_modifyObj a (alloc_region run ▸ hr) _).bind' fun _ =>
    (frames_toW r R (.obj a)).bind' fun w => frames_pure r R (Told.value w)

/-- A constructible callback and its new prototype can point to each other.
The reachable envelope includes both fresh addresses, every supplied captured cell,
and both intrinsic prototypes. -/
theorem access_newConstructor {r R cells A} (away : Away r R A) (call : Call W)
    (construct : Val → List Val → M W Val) (code part name length)
    (functionRoot : A (.obj functionProto)) (objectRoot : A (.obj objectProto))
    (s : State W) (hs : Access cells A s) (hr : s.region ≠ r) :
    ∃ B, (∀ node, A node → B node) ∧ Away r R B ∧
      (perform call construct code cells (.newFunction part name length true) s).Accessible cells B (fun _ => True) := by
  simp only [perform, makeOpaque, ↓reduceIte, addPrototype, M.bind_assoc, M.pure_bind]
  apply access_alloc_bind away _ _ s hs hr
  · intro a t ha ht hr'
    apply access_alloc_bind ha _ _ t ht hr'
    · intro p u hp hu hr''
      let B := withObject (withObject A a) p
      have fa : B (.obj a) := Or.inl (Or.inr rfl)
      have fp : B (.obj p) := Or.inr rfl
      refine ⟨B, fun _ h => h, hp, ?_⟩
      apply (ReachFrame.bind (reachFrame_modifyObj hp a fa _ ?_) ?_).accessible u hu hr''
      · intro o node hn
        simp [Obj.nodes, Property.nodes, Val.nodes] at hn ⊢
        rcases hn with h | h | h | h
        · exact Or.inl (Or.inl h)
        · exact Or.inl (Or.inr (Or.inl h))
        · subst node; exact Or.inr fp
        · exact Or.inl (Or.inr (Or.inr h))
      · intro _ _
        exact (reachFrame_toW r R cells B _ (safeVal_obj.2 fa)).bind fun w _ =>
          reachFrame_pure r R cells B (Told.value w) trivial
    · intro node hn
      simp [Obj.nodes, Property.nodes, Val.nodes] at hn
      rcases hn with rfl | rfl
      · exact Or.inl objectRoot
      · exact Or.inr rfl
  · intro node hn
    simp [Obj.nodes, Property.nodes, Val.nodes] at hn
    exact hn.elim (fun h => h ▸ functionRoot) (opaqueCapturesAccessible hs code part node)

end CoreJS
