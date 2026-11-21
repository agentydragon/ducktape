import Experiments.CoreJS.Reachability

/-!
# Reachability frames for binding requests and own keys

The proofs apply to actual `perform` requests. Their premises describe the stored pointer graph, not the effect of
running a request. All stateful outcomes preserve the graph invariant; exceptions retain safe thrown values.
-/

namespace CoreJS

variable {W : World} {α β : Type}

def Res.Accessible (result : Res W α) (cells : List (Option Addr)) (A : HeapNode → Prop) (P : α → Prop) : Prop :=
  match result with
  | .ok a s => P a ∧ Access cells A s
  | .throw v s => SafeVal A v ∧ Access cells A s
  | .unknown _ s => Access cells A s
  | .fuel => True

/-- A frame on states with a closed capability envelope, with a successful-result postcondition for composition. -/
structure ReachFrame (r : Nat) (R : Addr → Prop) (cells : List (Option Addr)) (A : HeapNode → Prop)
    (P : α → Prop) (m : M W α) : Prop where
  replace : ∀ s H C, Access cells A s → s.region ≠ r → m (s.put r H C) = (m s).put r H C
  preserves : ∀ s, Access cells A s → s.region ≠ r → (m s).Preserves r R s
  accessible : ∀ s, Access cells A s → s.region ≠ r → (m s).Accessible cells A P

theorem ReachFrame.of_frames {r R cells A P} {m : M W α} (h : Frames r R m)
    (hs : ∀ s, Access cells A s → s.region ≠ r → (m s).Accessible cells A P) : ReachFrame r R cells A P m :=
  ⟨fun s H C _ hr => h.replace s H C hr, fun s _ hr => h.preserves s hr, hs⟩

theorem ReachFrame.bind {r R cells A P Q} {m : M W α} {f : α → M W β}
    (hm : ReachFrame r R cells A P m) (hf : ∀ a, P a → ReachFrame r R cells A Q (f a)) :
    ReachFrame r R cells A Q (m >>= f) := by
  constructor
  · intro s H C hs hr
    rw [M.bind_apply, M.bind_apply, hm.replace s H C hs hr]
    have hp := hm.preserves s hs hr
    have hv := hm.accessible s hs hr
    cases h : m s with
    | ok a t =>
      simp only [h, Res.Preserves, Res.Accessible] at hp hv
      exact (hf a hv.1).replace t H C hv.2 (hp.region ▸ hr)
    | throw v t => rfl
    | unknown why t => rfl
    | fuel => rfl
  · intro s hs hr
    rw [M.bind_apply]
    have hp := hm.preserves s hs hr
    have hv := hm.accessible s hs hr
    cases h : m s with
    | ok a t =>
      simp only [h, Res.Preserves, Res.Accessible] at hp hv
      exact Res.Preserves.trans hp ((hf a hv.1).preserves t hv.2 (hp.region ▸ hr))
    | throw v t => simpa only [h, Res.Preserves] using hp
    | unknown why t => simpa only [h, Res.Preserves] using hp
    | fuel => trivial
  · intro s hs hr
    rw [M.bind_apply]
    have hp := hm.preserves s hs hr
    have hv := hm.accessible s hs hr
    cases h : m s with
    | ok a t =>
      simp only [h, Res.Preserves, Res.Accessible] at hp hv
      exact (hf a hv.1).accessible t hv.2 (hp.region ▸ hr)
    | throw v t => simpa only [h, Res.Accessible] using hv
    | unknown why t => simpa only [h, Res.Accessible] using hv
    | fuel => trivial

/-- Catching an exception retains both the state invariant and the safety of the caught value. -/
theorem ReachFrame.attempt {r R cells A P} {m : M W α} (hm : ReachFrame r R cells A P m) :
    ReachFrame r R cells A (fun result => match result with | .ok a => P a | .error v => SafeVal A v)
      (attempt m) := by
  constructor
  · intro s H C hs hr
    unfold CoreJS.attempt
    rw [hm.replace s H C hs hr]
    cases m s <;> rfl
  · intro s hs hr
    have hp := hm.preserves s hs hr
    unfold CoreJS.attempt
    cases h : m s with
    | fuel => trivial
    | ok | throw | unknown => simpa only [h, Res.Preserves] using hp
  · intro s hs hr
    have hp := hm.accessible s hs hr
    unfold CoreJS.attempt
    cases h : m s with
    | fuel => trivial
    | ok | throw | unknown => simpa only [h, Res.Accessible] using hp

theorem reachFrame_pure (r R cells A) {P : α → Prop} (a : α) (hp : P a) :
    ReachFrame r R cells A P (pure a : M W α) :=
  .of_frames (frames_pure r R a) (fun _ hs _ => ⟨hp, hs⟩)

theorem reachFrame_unknown (r R cells A) (P : α → Prop) (why : String) :
    ReachFrame r R cells A P (unknown why : M W α) :=
  .of_frames (frames_unknown r R why) (fun _ hs _ => hs)

theorem list_index_mem {α : Type} : ∀ {xs : List α} {i : Nat} {a : α}, xs[i]? = some a → a ∈ xs
  | [], _, _, h => by simp at h
  | x :: xs, 0, a, h => by simp only [List.getElem?_cons_zero, Option.some.injEq] at h; subst a; simp
  | x :: xs, i + 1, a, h => List.mem_cons_of_mem x (list_index_mem h)

theorem array_index_mem {α : Type} {xs : Array α} {i : Nat} {a : α} (h : xs[i]? = some a) : a ∈ xs := by
  have ht : xs.toList[i]? = some a := by simpa using h
  simpa using list_index_mem ht

theorem reachFrame_fromW (r R cells A) (v : WVal) :
    ReachFrame r R cells A (SafeVal A) (fromW v : M W Val) := by
  apply ReachFrame.of_frames (frames_fromW r R v)
  intro s hs hr
  cases v with
  | prim p =>
    simp only [fromW]
    split
    · refine ⟨?_, hs⟩
      cases p <;> simp [SafeVal, Primitive.val, Val.nodes]
    · exact hs
  | handle k =>
    simp only [fromW]
    split
    · rename_i a ha
      exact ⟨safeVal_obj.2 (hs.handed a (array_index_mem ha)), hs⟩
    · exact hs
  | error => exact ⟨by simp [SafeVal, Val.nodes], hs⟩
  | host => exact ⟨by simp [SafeVal, Val.nodes], hs⟩

theorem reachFrame_toW (r R cells A) (v : Val) (hv : SafeVal A v) :
    ReachFrame r R cells A (fun _ => True) (toW v : M W WVal) := by
  apply ReachFrame.of_frames (frames_toW r R v)
  intro s hs hr
  cases v <;> try exact ⟨trivial, hs⟩
  case obj a =>
    simp only [toW]
    split
    · exact ⟨trivial, hs⟩
    · refine ⟨trivial, hs.world _ ?_⟩
      intro b hb
      simp only [Array.mem_push] at hb
      rcases hb with hb | hb
      · exact hs.handed b hb
      · subst b; exact safeVal_obj.1 hv

theorem reachFrame_opaqueCell (r R cells A) (i : Nat) :
    ReachFrame r R cells A (fun a => A (.cell a)) (opaqueCell cells i : M W Addr) := by
  cases hc : cells[i]? with
  | none => simpa only [opaqueCell, hc] using (reachFrame_unknown (W := W) r R cells A (fun a => A (.cell a)) "the world names a binding the code was not handed")
  | some entry =>
    cases entry with
    | none => simpa only [opaqueCell, hc] using (reachFrame_unknown (W := W) r R cells A (fun a => A (.cell a)) "the world asks for the binding of a name the program does not declare")
    | some a =>
      constructor
      · intro s H C hs hr; simp only [opaqueCell, hc]; rfl
      · intro s hs hr; simp only [opaqueCell, hc]; exact Preserves.refl r R s
      · intro s hs hr
        simp only [opaqueCell, hc]
        exact ⟨hs.supplied a (list_index_mem hc), hs⟩

theorem reachFrame_readCell {r R cells A} (away : Away r R A) (a : Addr) (ha : A (.cell a)) :
    ReachFrame r R cells A (SafeVal A) (readCell a : M W Val) := by
  apply ReachFrame.of_frames (frames_readCell r R a (away.region _ ha))
  intro s hs hr
  simp only [readCell, M.bind_apply, getState]
  cases hb : s.cellAt a with
  | none => exact ⟨by simp [SafeVal, Val.nodes], hs⟩
  | some binding =>
    rcases binding with ⟨value, mutable⟩
    cases value with
    | none => exact ⟨by simp [SafeVal, Val.nodes], hs⟩
    | some v => exact ⟨hs.value ha hb rfl, hs⟩

theorem reachFrame_setCell {r R cells A} (away : Away r R A) (a : Addr) (ha : A (.cell a))
    (v : Val) (hv : SafeVal A v) : ReachFrame r R cells A (fun _ => True) (setCell a v : M W Unit) := by
  constructor
  · intro s H C hs hr
    exact congrArg (Res.ok ()) (State.assign_put s a v (away.region _ ha) H C)
  · intro s hs hr
    exact State.assign_preserves away ha s v
  · intro s hs hr
    exact ⟨trivial, hs.assign a v hv⟩

theorem reachFrame_writeCell {r R cells A} (away : Away r R A) (a : Addr) (ha : A (.cell a))
    (v : Val) (hv : SafeVal A v) : ReachFrame r R cells A (fun _ => True) (writeCell a v : M W Unit) := by
  have hset := reachFrame_setCell (W := W) (cells := cells) away a ha v hv
  constructor
  · intro s H C hs hr
    have hc : (s.put r H C).cellAt a = s.cellAt a := by
      have har : a.region ≠ r := away.region _ ha
      rw [State.cellAt_put, ite_eq_right har]
    simp only [writeCell, M.bind_apply, getState]
    rw [hc]
    cases s.cellAt a with
    | none => rfl
    | some b =>
      rcases b with ⟨value, mutable⟩
      cases value with
      | none => rfl
      | some old =>
        cases mutable
        · rfl
        · exact hset.replace s H C hs hr
  · intro s hs hr
    simp only [writeCell, M.bind_apply, getState]
    cases s.cellAt a with
    | none => exact Preserves.refl r R s
    | some b =>
      rcases b with ⟨value, mutable⟩
      cases value with
      | none => exact Preserves.refl r R s
      | some old =>
        cases mutable
        · exact Preserves.refl r R s
        · exact hset.preserves s hs hr
  · intro s hs hr
    simp only [writeCell, M.bind_apply, getState]
    cases s.cellAt a with
    | none => exact ⟨by simp [SafeVal, Val.nodes], hs⟩
    | some b =>
      rcases b with ⟨value, mutable⟩
      cases value with
      | none => exact ⟨by simp [SafeVal, Val.nodes], hs⟩
      | some old =>
        cases mutable
        · exact ⟨by simp [SafeVal, Val.nodes], hs⟩
        · exact hset.accessible s hs hr

theorem reachFrame_ownKeys {r R cells A} (away : Away r R A) (v : Val) (hv : SafeVal A v) :
    ReachFrame r R cells A (fun _ => True) (ownKeys v : M W (List String)) := by
  cases v <;> try exact reachFrame_unknown r R cells A _ _
  case obj a =>
    have har : a.region ≠ r := away.region _ (safeVal_obj.1 hv)
    constructor
    · intro s H C hs hr
      have ho : (s.put r H C).objAt a = s.objAt a := by rw [State.objAt_put, ite_eq_right har]
      simp only [ownKeys, M.bind_apply, getObj]
      rw [ho]
      cases s.objAt a <;> rfl
    · intro s hs hr
      simp only [ownKeys, M.bind_apply, getObj]
      cases s.objAt a <;> exact Preserves.refl r R s
    · intro s hs hr
      simp only [ownKeys, M.bind_apply, getObj]
      cases s.objAt a
      · exact hs
      · exact ⟨trivial, hs⟩

/-- The currently proved request family: binding reads and writes, initialization, and own-key enumeration. -/
def Request.bindingOrKeys : Request → Bool
  | .readVar _ | .writeVar _ _ | .initVar _ _ | .keys _ => true
  | _ => false

/-- Actual non-callback requests frame the protected region and retain the capability invariant on every outcome. -/
theorem perform_bindingOrKeys {r R cells A} (away : Away r R A) (call : Call W)
    (construct : Val → List Val → M W Val) (code : Nat) (req : Request) (hreq : req.bindingOrKeys = true) :
    ReachFrame r R cells A (fun _ => True) (perform call construct code cells req) := by
  cases req <;> simp only [Request.bindingOrKeys, Bool.false_eq_true] at hreq
  case readVar i =>
    exact (reachFrame_opaqueCell r R cells A i).bind fun a ha =>
      (reachFrame_readCell away a ha).bind fun v hv =>
        (reachFrame_toW r R cells A v hv).bind fun w _ => reachFrame_pure r R cells A (Told.value w) trivial
  case writeVar i w =>
    exact (reachFrame_opaqueCell r R cells A i).bind fun a ha =>
      (reachFrame_fromW r R cells A w).bind fun v hv =>
        (reachFrame_writeCell away a ha v hv).bind fun _ _ =>
          reachFrame_pure r R cells A (Told.value (.prim .undef)) trivial
  case initVar i w =>
    exact (reachFrame_opaqueCell r R cells A i).bind fun a ha =>
      (reachFrame_fromW r R cells A w).bind fun v hv =>
        (reachFrame_setCell away a ha v hv).bind fun _ _ =>
          reachFrame_pure r R cells A (Told.value (.prim .undef)) trivial
  case keys w =>
    exact (reachFrame_fromW r R cells A w).bind fun v hv =>
      (reachFrame_ownKeys away v hv).bind fun ks _ => reachFrame_pure r R cells A (Told.keys ks) trivial

/-- A read can publish an object handle, initialization can copy that handle into another binding, and a later read
can publish the alias again. The same capability envelope covers the whole sequence, including failed requests. -/
theorem publish_alias_frame {r R cells A} (away : Away r R A) (call : Call W)
    (construct : Val → List Val → M W Val) (code src dst handle : Nat) :
    ReachFrame r R cells A (fun _ => True) (do
      let _ ← perform call construct code cells (.readVar src)
      let _ ← perform call construct code cells (.initVar dst (.handle handle))
      perform call construct code cells (.readVar dst)) :=
  (perform_bindingOrKeys away call construct code (.readVar src) rfl).bind fun _ _ =>
    (perform_bindingOrKeys away call construct code (.initVar dst (.handle handle)) rfl).bind fun _ _ =>
      perform_bindingOrKeys away call construct code (.readVar dst) rfl

end CoreJS
