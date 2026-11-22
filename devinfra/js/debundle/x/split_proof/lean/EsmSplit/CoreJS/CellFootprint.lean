import EsmSplit.CoreJS.Reachability

/-!
# Successful computations with binding footprints

A successful computation reads complete binding records in `rd ∪ wr` and changes only binding values in `wr`.
Its finite assignment patch replays in every state agreeing on those records. The patch describes the exact state,
including store shape: equality of `cellAt` observations alone would lose trailing empty regions. Values can be
arbitrary references; these rules never inspect their objects. Abrupt executions have no postcondition here.
-/

namespace CoreJS

variable {W : World} {α β : Type}

/-- The complete binding record matters: assignment observes initialization and mutability as well as its value. -/
def CellsAgree (cells : Addr → Prop) (s t : State W) : Prop :=
  ∀ a, cells a → s.cellAt a = t.cellAt a

theorem CellsAgree.refl (cells : Addr → Prop) (s : State W) : CellsAgree cells s s := fun _ _ => rfl

theorem CellsAgree.symm {cells : Addr → Prop} {s t : State W} (h : CellsAgree cells s t) :
    CellsAgree cells t s := fun a ha => (h a ha).symm

theorem CellsAgree.mono {cells cells' : Addr → Prop} {s t : State W} (h : CellsAgree cells s t)
    (hs : ∀ a, cells' a → cells a) : CellsAgree cells' s t := fun a ha => h a (hs a ha)

theorem CellsAgree.assign {cells : Addr → Prop} {s t : State W} (h : CellsAgree cells s t) (a : Addr) (v : Val) :
    CellsAgree cells (s.assign a v) (t.assign a v) := by
  intro c hc
  by_cases he : c = a
  · subst c; rw [State.cellAt_assign_self, State.cellAt_assign_self, h a hc]
  · rw [State.cellAt_assign_other _ _ _ _ he, State.cellAt_assign_other _ _ _ _ he, h c hc]

private theorem array_modify_comm {γ : Type} (xs : Array γ) {i j : Nat} (f g : γ → γ) (hne : i ≠ j) :
    (xs.modify i f).modify j g = (xs.modify j g).modify i f := by
  apply Array.ext_getElem?
  intro k
  by_cases hi : i = k <;> by_cases hj : j = k <;> simp_all [Array.getElem?_modify]

private theorem array_modify_same {γ : Type} (xs : Array γ) (i : Nat) (f g : γ → γ) :
    (xs.modify i f).modify i g = xs.modify i (fun x => g (f x)) := by
  apply Array.ext_getElem?
  intro k
  by_cases hi : i = k <;> simp [Array.getElem?_modify, hi, Option.map_map, Function.comp_def]

/-- Disjoint assignments commute even at missing addresses, without identifying states by lookup observations. -/
theorem State.assign_comm (s : State W) (a b : Addr) (v w : Val) (hne : a ≠ b) :
    (s.assign a v).assign b w = (s.assign b w).assign a v := by
  simp only [State.assign]
  congr 1
  by_cases hr : a.region = b.region
  · have hi : a.index ≠ b.index := by
      intro he
      exact hne (by cases a; cases b; simp_all)
    simp only [hr, array_modify_same]
    congr 1
    funext bs
    exact array_modify_comm bs _ _ hi
  · exact array_modify_comm s.cells _ _ hr

abbrev CellPatch := List (Addr × Val)

def CellPatch.apply (patch : CellPatch) (s : State W) : State W :=
  patch.foldl (fun t p => t.assign p.1 p.2) s

def CellPatch.Supported (patch : CellPatch) (wr : Addr → Prop) : Prop := ∀ p ∈ patch, wr p.1

@[simp] theorem CellPatch.apply_nil (s : State W) : CellPatch.apply [] s = s := rfl

@[simp] theorem CellPatch.apply_cons (p : Addr × Val) (ps : CellPatch) (s : State W) :
    CellPatch.apply (p :: ps) s = ps.apply (s.assign p.1 p.2) := rfl

@[simp] theorem CellPatch.apply_append (ps qs : CellPatch) (s : State W) :
    (ps ++ qs).apply s = qs.apply (ps.apply s) := List.foldl_append

theorem CellPatch.agree (ps : CellPatch) {cells : Addr → Prop} {s t : State W} (h : CellsAgree cells s t) :
    CellsAgree cells (ps.apply s) (ps.apply t) := by
  induction ps generalizing s t with
  | nil => exact h
  | cons p ps ih => exact ih (h.assign p.1 p.2)

theorem CellPatch.agree_outside {ps : CellPatch} {wr cells : Addr → Prop} (hp : ps.Supported wr)
    (hd : ∀ a, wr a → ¬ cells a) (s : State W) : CellsAgree cells (ps.apply s) s := by
  induction ps generalizing s with
  | nil => exact .refl _ _
  | cons p ps ih =>
    have ht := ih (fun q hq => hp q (List.mem_cons_of_mem _ hq)) (s.assign p.1 p.2)
    intro a ha
    exact (ht a ha).trans (State.cellAt_assign_other _ _ _ _ (fun he => hd p.1 (hp p (by simp)) (he ▸ ha)))

private theorem CellPatch.apply_assign {ps : CellPatch} (s : State W) (a : Addr) (v : Val)
    (hd : ∀ p ∈ ps, p.1 ≠ a) : ps.apply (s.assign a v) = (ps.apply s).assign a v := by
  induction ps generalizing s with
  | nil => rfl
  | cons p ps ih =>
    simp only [apply_cons]
    rw [State.assign_comm _ _ _ _ _ (Ne.symm (hd p (by simp)))]
    exact ih _ (fun q hq => hd q (List.mem_cons_of_mem _ hq))

theorem CellPatch.apply_comm {ps qs : CellPatch} (s : State W)
    (hd : ∀ p ∈ ps, ∀ q ∈ qs, p.1 ≠ q.1) : qs.apply (ps.apply s) = ps.apply (qs.apply s) := by
  induction qs generalizing s with
  | nil => rfl
  | cons q qs ih =>
    simp only [apply_cons]
    rw [← ps.apply_assign s q.1 q.2 (fun p hp => hd p hp q (by simp))]
    exact ih _ (fun p hp q hq => hd p hp q (List.mem_cons_of_mem _ hq))

@[simp] theorem CellPatch.apply_region (ps : CellPatch) (s : State W) : (ps.apply s).region = s.region := by
  induction ps generalizing s with
  | nil => rfl
  | cons p ps ih => exact ih _

@[simp] theorem CellPatch.apply_heap (ps : CellPatch) (s : State W) : (ps.apply s).heap = s.heap := by
  induction ps generalizing s with
  | nil => rfl
  | cons p ps ih => exact ih _

@[simp] theorem CellPatch.apply_world (ps : CellPatch) (s : State W) : (ps.apply s).world = s.world := by
  induction ps generalizing s with
  | nil => rfl
  | cons p ps ih => exact ih _

@[simp] theorem CellPatch.apply_out (ps : CellPatch) (s : State W) : (ps.apply s).out = s.out := by
  induction ps generalizing s with
  | nil => rfl
  | cons p ps ih => exact ih _

@[simp] theorem CellPatch.apply_setRegion (ps : CellPatch) (s : State W) (r : Nat) :
    ps.apply { s with region := r } = { ps.apply s with region := r } := by
  induction ps generalizing s with
  | nil => rfl
  | cons p ps ih =>
    change CellPatch.apply ps (({ s with region := r }).assign p.1 p.2) =
      { CellPatch.apply ps (s.assign p.1 p.2) with region := r }
    simpa only [State.assign] using ih (s.assign p.1 p.2)

/-- The supported patch and returned value replay on any state agreeing on the read/write binding records.
No claim is made about an execution which throws, is unknown, or exhausts fuel. -/
structure CellFootprint (rd wr : Addr → Prop) (m : M W α) : Prop where
  replay : ∀ s a s', m s = .ok a s' → ∃ patch : CellPatch,
    patch.Supported wr ∧ s' = patch.apply s ∧
    ∀ t, CellsAgree (fun c => rd c ∨ wr c) s t → m t = .ok a (patch.apply t)

theorem CellFootprint.mono {rd wr rd' wr' : Addr → Prop} {m : M W α} (h : CellFootprint rd wr m)
    (hr : ∀ a, rd a → rd' a) (hw : ∀ a, wr a → wr' a) : CellFootprint rd' wr' m := by
  constructor
  intro s a s' hm
  obtain ⟨p, hp, hs, ht⟩ := h.replay s a s' hm
  exact ⟨p, fun q hq => hw q.1 (hp q hq), hs, fun t h => ht t (h.mono fun c hc => hc.elim
    (fun h => Or.inl (hr c h)) (fun h => Or.inr (hw c h)))⟩

/-- Sequential composition takes the union of each footprint. -/
theorem CellFootprint.bind {rd wr rd' wr' : Addr → Prop} {m : M W α} {f : α → M W β}
    (hm : CellFootprint rd wr m) (hf : ∀ a, CellFootprint rd' wr' (f a)) :
    CellFootprint (fun c => rd c ∨ rd' c) (fun c => wr c ∨ wr' c) (m >>= f) := by
  constructor
  intro s b s' h
  obtain ⟨a, u, ha, hb⟩ := bind_ok h
  obtain ⟨p, hp, hu, hpt⟩ := hm.replay s a u ha
  obtain ⟨q, hq, hs, hqt⟩ := (hf a).replay u b s' hb
  refine ⟨p ++ q, ?_, ?_, ?_⟩
  · intro x hx
    exact (List.mem_append.1 hx).elim (fun h => Or.inl (hp x h)) (fun h => Or.inr (hq x h))
  · rw [CellPatch.apply_append, ← hu]; exact hs
  · intro t ht
    have hp' := hpt t (ht.mono fun c h => h.elim (fun h => Or.inl (Or.inl h)) (fun h => Or.inr (Or.inl h)))
    rw [bind_of_ok hp', CellPatch.apply_append]
    apply hqt
    rw [hu]
    exact p.agree (ht.mono fun c h => h.elim (fun h => Or.inl (Or.inr h)) (fun h => Or.inr (Or.inr h)))

/-- A fixed result neither reads nor writes a binding. -/
theorem cellFootprint_pure (a : α) : CellFootprint (fun _ => False) (fun _ => False) (pure a : M W α) := by
  constructor
  intro s b s' h
  cases h
  exact ⟨[], by simp [CellPatch.Supported], rfl, fun _ _ => rfl⟩

theorem cellFootprint_outOfFuel : CellFootprint (fun _ => False) (fun _ => False) (outOfFuel : M W α) := by
  constructor
  intro s a s' h
  cases h

theorem cellFootprint_unknown (why : String) :
    CellFootprint (fun _ => False) (fun _ => False) (unknown why : M W α) := by
  constructor
  intro s a s' h
  cases h

theorem cellFootprint_throwErr (kind : ErrorKind) (msg : String) :
    CellFootprint (fun _ => False) (fun _ => False) (throwErr kind msg : M W α) := by
  constructor
  intro s a s' h
  cases h

/-- A fixed state-free operation either returns its value or has no successful execution. -/
theorem cellFootprint_lift (result : Except Abort α) :
    CellFootprint (fun _ => False) (fun _ => False) (lift result : M W α) := by
  cases result with
  | ok a => exact cellFootprint_pure a
  | error why =>
    cases why with
    | error kind msg => exact cellFootprint_throwErr kind msg
    | unknown why => exact cellFootprint_unknown why

/-- A branch selected from one complete binding record adds that cell to the read footprint. -/
theorem cellFootprint_inspect (a : Addr) {rd wr : Addr → Prop} (f : Option Binding → M W α)
    (hf : ∀ b, CellFootprint rd wr (f b)) :
    CellFootprint (fun c => c = a ∨ rd c) wr (fun s => f (s.cellAt a) s) := by
  constructor
  intro s v s' h
  obtain ⟨p, hp, hs, ht⟩ := (hf (s.cellAt a)).replay s v s' h
  refine ⟨p, hp, hs, fun t ha => ?_⟩
  rw [← ha a (Or.inl (Or.inl rfl))]
  exact ht t (ha.mono fun _ h => h.elim (fun h => Or.inl (Or.inr h)) Or.inr)

theorem cellFootprint_setCell (a : Addr) (v : Val) :
    CellFootprint (fun _ => False) (fun c => c = a) (setCell a v : M W Unit) := by
  constructor
  intro s b s' h
  cases h
  exact ⟨[(a, v)], by simp [CellPatch.Supported], rfl, fun _ _ => rfl⟩

theorem cellFootprint_readCell (a : Addr) :
    CellFootprint (fun c => c = a) (fun _ => False) (readCell a : M W Val) := by
  have h := cellFootprint_inspect (W := W) a (fun b => match b with
    | some { value := some v, .. } => pure v
    | _ => throwErr .reference "cannot access a binding before initialization") (rd := fun _ => False) (wr := fun _ => False) (by
      intro b
      cases b with
      | none => exact cellFootprint_throwErr _ _
      | some b =>
        rcases b with ⟨value, mutable⟩
        cases value <;> first | exact cellFootprint_throwErr _ _ | exact cellFootprint_pure _)
  exact h.mono (fun _ h => h.elim id False.elim) (fun _ h => h)

theorem cellFootprint_writeCell (a : Addr) (v : Val) :
    CellFootprint (fun _ => False) (fun c => c = a) (writeCell a v : M W Unit) := by
  constructor
  intro s b s' h
  simp only [writeCell, M.bind_apply, getState] at h
  cases hc : s.cellAt a with
  | none => simp [hc, throwErr, throwVal] at h
  | some binding =>
    rcases binding with ⟨value, mutable⟩
    cases value with
    | none => simp [hc, throwErr, throwVal] at h
    | some old =>
      cases mutable with
      | false => simp [hc, throwErr, throwVal] at h
      | true =>
        simp only [hc] at h
        obtain ⟨p, hp, hs, ht⟩ := (cellFootprint_setCell (W := W) a v).replay s b s' h
        refine ⟨p, hp, hs, fun t ha => ?_⟩
        have hc' : t.cellAt a = some { value := some old, mutable := true } := (ha a (Or.inr rfl)).symm.trans hc
        simp only [writeCell, M.bind_apply, getState, hc']
        exact ht t ha

/-- Name lookup is fixed by the environment, independently of the runtime store. -/
theorem cellFootprint_readVar (env : Env) (x : Name) :
    CellFootprint (fun c => env.vars.lookup x = some c) (fun _ => False) (readVar env x : M W Val) := by
  cases hx : env.vars.lookup x with
  | none => simpa only [readVar, hx, reduceCtorEq] using (cellFootprint_throwErr (W := W) .reference s!"{x} is not defined")
  | some a =>
    let f : Option Binding → M W Val := fun b => match b with
      | some { value := some v, .. } => pure v
      | _ => throwErr .reference s!"cannot access '{x}' before initialization"
    have hf : ∀ b, CellFootprint (fun _ => False) (fun _ => False) (f b) := by
      intro b
      cases b with
      | none => exact cellFootprint_throwErr _ _
      | some b =>
        rcases b with ⟨value, mutable⟩
        cases value <;> first | exact cellFootprint_throwErr _ _ | exact cellFootprint_pure _
    have he : readVar env x = (fun s => f (s.cellAt a) s) := by
      funext s
      simp only [readVar, hx, M.bind_apply, getState]
      rfl
    rw [he]
    exact (cellFootprint_inspect a f hf).mono
      (fun c hc => hc.elim (fun he => congrArg some he.symm) False.elim) (fun _ h => h)

theorem cellFootprint_initVar (env : Env) (x : Name) (v : Val) :
    CellFootprint (fun _ => False) (fun c => env.vars.lookup x = some c) (initVar env x v : M W Unit) := by
  cases hx : env.vars.lookup x with
  | none => simpa only [initVar, hx, reduceCtorEq] using (cellFootprint_unknown (W := W) "model: a declaration outside its scope")
  | some a =>
    simpa only [initVar, hx] using (cellFootprint_setCell (W := W) a v).mono (fun _ h => h)
      (wr' := fun c => some a = some c) (fun c hc => by subst c; rfl)

theorem cellFootprint_writeVar (env : Env) (x : Name) (v : Val) :
    CellFootprint (fun _ => False) (fun c => env.vars.lookup x = some c) (writeVar env x v : M W Unit) := by
  cases hx : env.vars.lookup x with
  | none => simpa only [writeVar, hx, reduceCtorEq] using (cellFootprint_throwErr (W := W) .reference s!"{x} is not defined")
  | some a =>
    constructor
    intro s b s' h
    simp only [writeVar, hx, M.bind_apply, getState] at h
    cases hc : s.cellAt a with
    | none => simp [hc, throwErr, throwVal] at h
    | some binding =>
      rcases binding with ⟨value, mutable⟩
      cases value with
      | none => simp [hc, throwErr, throwVal] at h
      | some old =>
        cases mutable with
        | false => simp [hc, throwErr, throwVal] at h
        | true =>
          simp only [hc] at h
          obtain ⟨p, hp, hs, ht⟩ := (cellFootprint_setCell (W := W) a v).replay s b s' h
          refine ⟨p, fun q hq => ?_, hs, fun t ha => ?_⟩
          · exact congrArg some (hp q hq).symm
          · have hc' : t.cellAt a = some { value := some old, mutable := true } := (ha a (Or.inr rfl)).symm.trans hc
            simp only [writeVar, hx, M.bind_apply, getState, hc']
            exact ht t (ha.mono fun c h => h.elim False.elim (fun he => Or.inr (congrArg some he.symm)))

/-- The two computations cannot write a binding which the other reads or writes. -/
theorem CellFootprint.commute {rd wr rd' wr' : Addr → Prop} {m : M W α} {n : M W β}
    (hm : CellFootprint rd wr m) (hn : CellFootprint rd' wr' n)
    (hab : ∀ c, wr c → ¬ (rd' c ∨ wr' c)) (hba : ∀ c, wr' c → ¬ (rd c ∨ wr c)) : Commute m n := by
  intro s u s' a b ha hb
  obtain ⟨p, hp, hu, ht⟩ := hm.replay s a u ha
  obtain ⟨q, hq, hs, hqt⟩ := hn.replay u b s' hb
  have hn' : n s = .ok b (q.apply s) := by
    apply hqt
    rw [hu]
    exact p.agree_outside hp hab s
  refine ⟨q.apply s, hn', ?_⟩
  have hm' := ht (q.apply s) (q.agree_outside hq hba s).symm
  rw [hs, hu]
  rw [CellPatch.apply_comm s (fun x hx y hy he => hab x.1 (hp x hx) (Or.inr (he ▸ hq y hy)))]
  exact hm'

/-- Successful runs preserve the heap, host state, output, and allocation region exactly. -/
theorem CellFootprint.preserves {rd wr : Addr → Prop} {m : M W α} (h : CellFootprint rd wr m)
    {s s' : State W} {a : α} (hm : m s = .ok a s') :
    s'.heap = s.heap ∧ s'.world = s.world ∧ s'.out = s.out ∧ s'.region = s.region := by
  obtain ⟨p, _, rfl, _⟩ := h.replay s a s' hm
  simp

/-- `inRegion` resets to zero after success. Its replay records that reset explicitly rather than claiming it
preserves an arbitrary input region. -/
theorem CellFootprint.inRegion_replay {rd wr : Addr → Prop} {m : M W α} (h : CellFootprint rd wr m)
    (id : Nat) (s : State W) (a : α) (s' : State W) (hm : inRegion id m s = .ok a s') :
    ∃ patch : CellPatch, patch.Supported wr ∧ s' = { patch.apply s with region := 0 } ∧
      ∀ t, CellsAgree (fun c => rd c ∨ wr c) s t →
        inRegion id m t = .ok a { patch.apply t with region := 0 } := by
  rw [inRegion_result] at hm
  cases hrun : m { s with region := id + 1 } with
  | throw v u => simp [hrun] at hm
  | unknown why u => simp [hrun] at hm
  | fuel => simp [hrun] at hm
  | ok b u =>
    simp only [hrun, Res.ok.injEq] at hm
    obtain ⟨rfl, rfl⟩ := hm
    obtain ⟨p, hp, hu, ht⟩ := h.replay _ b u hrun
    refine ⟨p, hp, ?_, fun t ha => ?_⟩
    · rw [hu, CellPatch.apply_setRegion]
    · rw [inRegion_result, ht { t with region := id + 1 } ha, CellPatch.apply_setRegion]

/-- The unit-region wrapper preserves disjoint-footprint commutation, including its reset to region zero. -/
theorem CellFootprint.inRegion_commute {rd wr rd' wr' : Addr → Prop} {m : M W α} {n : M W β}
    (hm : CellFootprint rd wr m) (hn : CellFootprint rd' wr' n)
    (hab : ∀ c, wr c → ¬ (rd' c ∨ wr' c)) (hba : ∀ c, wr' c → ¬ (rd c ∨ wr c))
    (i j : Nat) : Commute (inRegion i m) (inRegion j n) := by
  intro s u s' a b ha hb
  obtain ⟨p, hp, hu, ht⟩ := hm.inRegion_replay i s a u ha
  obtain ⟨q, hq, hs, hqt⟩ := hn.inRegion_replay j u b s' hb
  have hn' : inRegion j n s = .ok b { q.apply s with region := 0 } := by
    apply hqt
    rw [hu]
    exact p.agree_outside hp hab s
  refine ⟨{ q.apply s with region := 0 }, hn', ?_⟩
  have hm' := ht { q.apply s with region := 0 } (q.agree_outside hq hba s).symm
  have hpatch := CellPatch.apply_comm s (fun x hx y hy he => hab x.1 (hp x hx) (Or.inr (he ▸ hq y hy)))
  have hstate : s' = { p.apply { q.apply s with region := 0 } with region := 0 } := by
    rw [hs, hu, q.apply_setRegion (p.apply s) 0, p.apply_setRegion (q.apply s) 0, hpatch]
  exact hm'.trans (congrArg (Res.ok a) hstate.symm)

#print axioms CellFootprint.bind
#print axioms CellFootprint.commute
#print axioms CellFootprint.inRegion_commute

end CoreJS
