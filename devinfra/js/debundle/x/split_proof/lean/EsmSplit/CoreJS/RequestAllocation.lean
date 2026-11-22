import EsmSplit.CoreJS.RequestFrame

/-! # Opaque object creation

Allocation extends a closed capability envelope by the newly issued object address.
Only the default-prototype case introduces a built-in root. -/

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
    simp only []
    rw [M.bind_apply]
    cases ha : alloc o t with
    | ok a u =>
      have har : a.region ≠ r := (alloc_region ha).trans hframe.region ▸ hr
      let B := withObject A a
      have hb : Away r R B := by
        constructor
        · intro node hn; rcases hn with hn | rfl
          · exact away.region _ hn
          · exact har
        · intro b hb; rcases hb with hb | hb
          · exact away.reads _ hb
          · cases hb
      refine ⟨B, fun _ hn => Or.inl hn, hb, ?_⟩
      have hu := access_alloc hp.2 safe ha
      have hv : SafeVal B (.obj a) := safeVal_obj.2 (Or.inr rfl)
      have hue := (alloc_ok ha).2.region
      exact ((reachFrame_toW r R cells B _ hv).bind fun w _ =>
        reachFrame_pure r R cells B (Told.value w) trivial).accessible u hu (hue.trans hframe.region ▸ hr)
    | fuel | throw | unknown =>
      unfold alloc at ha
      split at ha <;> cases ha <;> exact ⟨A, fun _ h => h, away, hp.2⟩

end CoreJS
