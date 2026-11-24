import EsmSplit.CoreJS.Reachability

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

/-- Selecting a unit's allocation region leaves the stored graph and its roots intact. -/
private theorem Access.with_region {cells A} {s : State W} (hs : Access cells A s) (region : Nat) :
    Access cells A { s with region } := by
  refine ⟨hs.handed, hs.supplied, ?_⟩
  intro src dst hsrc edge
  cases edge with
  | obj ho hm => exact hs.closed _ _ hsrc (.obj ho hm)
  | cell hc hm => exact hs.closed _ _ hsrc (.cell hc hm)

/-- A successful crossed unit preserves the condition needed at the next swap. -/
theorem ReachFrame.runUnit_access {r R cells A Q n j env st} (hf : ReachFrame r R cells A Q (execStmt n env st : M W Flow))
    (hj : j + 1 ≠ r) {s t : State W} {f : Flow} (hs : Access cells A s) (run : runUnit n env j st s = .ok f t) :
    Access cells A t := by
  obtain ⟨u, hu, rfl⟩ := runUnit_ok run
  have hp := hf.accessible _ (hs.with_region _) hj
  rw [hu] at hp
  exact hp.2.with_region 0

/-- Both orders have the same successful outcomes from states with the required capability envelope. -/
theorem ReachFrame.local_commuteOn {n i j : Nat} {e1 e2 : Env} {st1 st2 : Stmt} {R cells A Q}
    (hf : ReachFrame (i + 1) R cells A Q (execStmt n e2 st2 : M W Flow)) (hij : i ≠ j)
    (hl : Local (i + 1) R (execStmt n e1 st1 : M W Flow)) :
    CommuteOn (Access cells A) (runUnit n e1 i st1 : M W Flow) (runUnit n e2 j st2) ∧
    CommuteOn (Access cells A) (runUnit n e2 j st2 : M W Flow) (runUnit n e1 i st1) := by
  have hj : j + 1 ≠ i + 1 := by omega
  have frame (s : State W) (hs : Access cells A s) :
      FramesAt (i + 1) R (execStmt n e2 st2) { s with region := j + 1 } :=
    ⟨fun H C => hf.replace _ H C (hs.with_region _) hj, hf.preserves _ (hs.with_region _) hj⟩
  exact ⟨local_frame_units_commuteOn hl frame, frame_local_units_commuteOn hl frame⟩

/-- Move a local unit across a block with one closed heap envelope. Each crossed
unit may receive different binding cells, all of which must lie inside the envelope. -/
theorem runUnits_reachFrame_pivot (n i : Nat) (env : Env) (st : Stmt) (R : Addr → Prop) (A : HeapNode → Prop)
    (pre crossed suffix : List LinkedUnit) (s s' : State W)
    (localUnit : Local (i + 1) R (execStmt n env st : M W Flow))
    (hprefix : ∀ t, runUnits n pre s = .ok () t → Access [] A t)
    (framed : ∀ u ∈ crossed, i ≠ u.1 ∧ ∃ cells,
      (∀ a, some a ∈ cells → A (.cell a)) ∧
      ReachFrame (i + 1) R cells A (fun _ => True) (execStmt n u.2.2 u.2.1 : M W Flow)) :
    runUnits n (pre ++ [(i, st, env)] ++ crossed ++ suffix) s = .ok () s' ↔
      runUnits n (pre ++ crossed ++ [(i, st, env)] ++ suffix) s = .ok () s' := by
  apply runUnits_pivot n pre crossed suffix (i, st, env) (Access [] A) s s' hprefix
  · intro u hu t t' f ht run
    obtain ⟨hij, cells, roots, hf⟩ := framed u hu
    have next := hf.runUnit_access (show u.1 + 1 ≠ i + 1 by omega) (s := t)
      ⟨ht.handed, roots, ht.closed⟩ run
    exact ⟨next.handed, by simp, next.closed⟩
  · intro u hu
    obtain ⟨hij, cells, roots, hf⟩ := framed u hu
    obtain ⟨forward, backward⟩ := hf.local_commuteOn hij localUnit
    exact ⟨fun t ht => forward t ⟨ht.handed, roots, ht.closed⟩,
      fun t ht => backward t ⟨ht.handed, roots, ht.closed⟩⟩

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

theorem reachFrame_lift {r R cells A} {P : α → Prop} (result : Except Abort α)
    (safe : ∀ a, result = .ok a → P a) :
    ReachFrame r R cells A P (lift result : M W α) := by
  cases result with
  | ok value => exact reachFrame_pure r R cells A value (safe value rfl)
  | error why =>
    cases why with
    | unknown reason => exact reachFrame_unknown r R cells A P reason
    | error kind message =>
      exact ReachFrame.of_frames (frames_throwVal r R (.error kind message))
        (fun _ hs _ => ⟨(fun _ h => by cases h), hs⟩)

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
  | prim p => exact ⟨by cases p <;> simp [SafeVal, Primitive.val, Val.nodes], hs⟩
  | illFormedString => exact hs
  | handle k =>
    simp only [fromW]
    split
    · rename_i a ha
      exact ⟨safeVal_obj.2 (hs.handed a (array_index_mem ha)), hs⟩
    · exact hs
  | error kind _ => cases kind <;> first | exact ⟨by simp [SafeVal, Val.nodes], hs⟩ | exact hs
  | host => exact ⟨by simp [SafeVal, Val.nodes], hs⟩

theorem reachFrame_toW (r R cells A) (v : Val) (hv : SafeVal A v) :
    ReachFrame r R cells A (fun _ => True) (toW v : M W WVal) := by
  apply ReachFrame.of_frames (frames_toW r R v)
  intro s hs hr
  cases v <;> try first | exact ⟨trivial, hs⟩ | exact hs
  case error kind msg => cases kind <;> first | exact ⟨trivial, hs⟩ | exact hs
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

theorem reachFrame_getObj {r R cells A} (away : Away r R A) {a : Addr} (ha : A (.obj a)) :
    ReachFrame r R cells A (fun o => ∀ n ∈ o.nodes, A n) (getObj a : M W Obj) := by
  constructor
  · intro s H C _ _
    have har : a.region ≠ r := away.region _ ha
    have same : (s.put r H C).objAt a = s.objAt a := by rw [State.objAt_put, ite_eq_right har]
    simp only [getObj, same]
    cases s.objAt a <;> rfl
  · intro s _ _
    simp only [getObj]
    cases s.objAt a <;> exact Preserves.refl r R s
  · intro s hs _
    simp only [getObj]
    cases ho : s.objAt a with
    | none => exact hs
    | some o => exact ⟨fun n hn => hs.closed _ _ ha (.obj ho hn), hs⟩

theorem frames_modifyObj {r R} (a : Addr) (ha : a.region ≠ r) (f : Obj → Obj) :
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
theorem reachFrame_modifyObj {r R cells A} (away : Away r R A) (a : Addr) (ha : A (.obj a))
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

theorem reachFrame_ownKeys {r R cells A} (away : Away r R A) (v : Val) (hv : SafeVal A v) :
    ReachFrame r R cells A (fun _ => True) (ownKeys v : M W (List PropertyKey)) := by
  cases v <;> try exact reachFrame_unknown r R cells A _ _
  case obj a =>
    exact (reachFrame_getObj away (safeVal_obj.1 hv)).bind fun o _ =>
      reachFrame_pure r R cells A (ownPropertyKeys o.props) trivial

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
    apply (reachFrame_fromW r R cells A w).bind
    intro v hv
    apply (reachFrame_ownKeys away v hv).bind
    intro ks _
    split
    · exact reachFrame_pure r R cells A (Told.keys _) trivial
    · exact reachFrame_unknown r R cells A _ _

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

private theorem reachFrame_talk (r R cells A) (told : Told) :
    ReachFrame r R cells A (fun req => ∃ state, (W.talk told state).1 = req) (talk told : M W Request) := by
  apply ReachFrame.of_frames (frames_talk r R told)
  intro s hs hr
  exact ⟨⟨s.world.state, rfl⟩, hs.world _ hs.handed⟩

private theorem reachFrame_toldOf (r R cells A) (result : Except Val Told)
    (safe : match result with | .ok _ => True | .error v => SafeVal A v) :
    ReachFrame r R cells A (fun _ => True) (toldOf result : M W Told) := by
  cases result with
  | ok t => exact reachFrame_pure r R cells A t trivial
  | error v =>
    exact (reachFrame_toW r R cells A v safe).bind fun w _ =>
      reachFrame_pure r R cells A (Told.threw w) trivial

/-- A whole opaque conversation frames the protected region when the requests this world can issue
preserve the same closed envelope. Returned and thrown object handles resolve inside that envelope.
Exceptions from individual requests can be reported to the world and the conversation can continue. -/
theorem converse_reachFrame (r R cells A) (code n : Nat)
    (requests : ∀ k < n, ∀ req, (∃ msg state, (W.talk msg state).1 = req) →
      ReachFrame r R cells A (fun _ => True) (perform (callFn k) (construct k) code cells req : M W Told))
    (told : Told) : ReachFrame r R cells A (SafeVal A) (converse n code cells told : M W Val) := by
  induction n generalizing told with
  | zero => exact ReachFrame.of_frames (frames_outOfFuel r R) fun _ _ _ => trivial
  | succ n ih =>
    unfold converse
    apply (reachFrame_talk r R cells A told).bind
    intro req ⟨state, issued⟩
    cases req with
    | done v => exact reachFrame_fromW r R cells A v
    | throw v =>
      exact (reachFrame_fromW r R cells A v).bind fun value safe =>
        ReachFrame.of_frames (frames_throwVal r R value) fun _ hs _ => ⟨safe, hs⟩
    | unknown why => exact reachFrame_unknown r R cells A _ why
    | _ =>
      apply (requests n (by omega) _ ⟨told, state, issued⟩).attempt.bind
      intro result safe
      exact (reachFrame_toldOf r R cells A result (by cases result <;> exact safe)).bind fun next _ =>
        ih (fun k hk req possible => requests k (by omega) req possible) next

/-- Execute a world-backed statement, including publishing its receiver and handing the conversation
its actual declaration and capture cells. -/
theorem opaque_world_reachFrame (r R A) (n : Nat) (env : Env) (code : Nat)
    (decls : List (Name × DeclKind)) (funs : List OpaqueHoist) (free : List Name)
    (receiver : SafeVal A env.this)
    (requests : ∀ k < n, ∀ req, (∃ msg state, (W.talk msg state).1 = req) →
      ReachFrame r R (opaqueCells env decls funs free) A (fun _ => True)
        (perform (callFn k) (construct k) code (opaqueCells env decls funs free) req : M W Told)) :
    ReachFrame r R (opaqueCells env decls funs free) A (fun _ => True)
      (execStmt n env (.opaque code decls funs free .world) : M W Flow) := by
  cases n with
  | zero => exact ReachFrame.of_frames (frames_outOfFuel r R) fun _ _ _ => trivial
  | succ n =>
    unfold execStmt
    apply (reachFrame_toW r R _ A env.this receiver).bind
    intro this _
    apply (converse_reachFrame r R _ A code n (fun k hk req hp => requests k (by omega) req hp)
      (.run code 0 (.prim .undef) this [] false)).bind
    exact fun _ _ => reachFrame_pure r R _ A Flow.normal trivial

end CoreJS
