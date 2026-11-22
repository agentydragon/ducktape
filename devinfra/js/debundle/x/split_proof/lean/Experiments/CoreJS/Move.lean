import EsmSplit.Semantics
import Experiments.CoreJS.Frame
import Experiments.CoreJS.Split

/-!
# CoreJS: a unit that only allocates sees and changes its own region and nothing else

`Local r R m`: run in region `r`, the computation `m` reads only the objects and bindings of region `r` and the
bindings `R`, and changes only region `r`. Two states that agree on those give it the same value, and it changes both
the same way: it replaces region `r` by the same contents.
-/

namespace CoreJS

variable {W : World}

/-! ## Replacing a region -/

/-- `s` with region `r` replaced: its objects by `H`, its bindings by `C`. Nothing else changes. -/
def State.put (s : State W) (r : Nat) (H : Array Obj) (C : Array Binding) : State W :=
  { s with heap := s.heap.set! r H, cells := s.cells.set! r C }

theorem set!_getElem? {α : Type} (xs : Array α) (r : Nat) (d : α) (j : Nat) :
    (xs.set! r d)[j]? = if r = j then (if r < xs.size then some d else none) else xs[j]? := by
  simp [Array.set!, Array.getElem?_setIfInBounds]

theorem set!_same {α : Type} {xs : Array α} {r : Nat} {o : Option α} (h : xs[r]? = o) (d : α) :
    xs.set! r (o.getD d) = xs := by
  refine Array.ext_getElem? fun j => ?_
  rw [set!_getElem?]
  by_cases hj : r = j
  · subst hj
    by_cases hlt : r < xs.size
    · simp only [hlt, ↓reduceIte]; rw [← h, Array.getElem?_eq_getElem hlt]; rfl
    · simp only [hlt, ↓reduceIte]; rw [Array.getElem?_eq_none (Nat.not_lt.1 hlt)]
  · simp [hj]

theorem lt_iff_of_getElem? {α : Type} {xs : Array α} {ys : Array α} {r : Nat} (h : xs[r]? = ys[r]?) :
    r < xs.size ↔ r < ys.size := by
  constructor
  · intro hx
    rcases Nat.lt_or_ge r ys.size with hy | hy
    · exact hy
    · rw [Array.getElem?_eq_getElem hx, Array.getElem?_eq_none hy] at h; cases h
  · intro hy
    rcases Nat.lt_or_ge r xs.size with hx | hx
    · exact hx
    · rw [Array.getElem?_eq_getElem hy, Array.getElem?_eq_none hx] at h; cases h

@[simp] theorem State.put_region (s : State W) (r : Nat) (H : Array Obj) (C : Array Binding) :
    (s.put r H C).region = r' ↔ s.region = r' := Iff.rfl

theorem State.put_put (s : State W) (r : Nat) (H H' : Array Obj) (C C' : Array Binding) :
    (s.put r H C).put r H' C' = s.put r H' C' := by
  simp [State.put, Array.set!, Array.setIfInBounds_setIfInBounds]

theorem set!_comm {α : Type} (xs : Array α) {r q : Nat} (h : r ≠ q) (a b : α) :
    (xs.set! r a).set! q b = (xs.set! q b).set! r a := by
  refine Array.ext_getElem? fun j => ?_
  simp only [Array.set!, Array.getElem?_setIfInBounds, Array.size_setIfInBounds]
  by_cases hr : r = j <;> by_cases hq : q = j
  · exact absurd (hr.trans hq.symm) h
  all_goals simp [hr, hq]

theorem State.put_comm (s : State W) {r q : Nat} (h : r ≠ q) (H H' : Array Obj) (C C' : Array Binding) :
    (s.put r H C).put q H' C' = (s.put q H' C').put r H C := by
  simp only [State.put, set!_comm _ h]

theorem State.cellAt_put (s : State W) (r : Nat) (H : Array Obj) (C : Array Binding) (c : Addr) :
    (s.put r H C).cellAt c = if c.region = r then (if r < s.cells.size then C[c.index]? else none) else s.cellAt c := by
  unfold State.cellAt State.put
  rw [set!_getElem?]
  by_cases hc : c.region = r
  · subst hc; by_cases hlt : c.region < s.cells.size <;> simp [hlt]
  · have : ¬ r = c.region := fun h => hc h.symm
    simp [hc, this]

theorem State.objAt_put (s : State W) (r : Nat) (H : Array Obj) (C : Array Binding) (a : Addr) :
    (s.put r H C).objAt a = if a.region = r then (if r < s.heap.size then H[a.index]? else none) else s.objAt a := by
  unfold State.objAt State.put
  rw [set!_getElem?]
  by_cases hc : a.region = r
  · subst hc; by_cases hlt : a.region < s.heap.size <;> simp [hlt]
  · have : ¬ r = a.region := fun h => hc h.symm
    simp [hc, this]

/-! ## What a computation in region `r` sees -/

/-- `s` and `t` both allocate in region `r`, have the same region `r`, and the same bindings `R`. -/
structure Sees (r : Nat) (R : Addr → Prop) (s t : State W) : Prop where
  region : s.region = r
  region' : t.region = r
  heap : s.heap[r]? = t.heap[r]?
  cells : s.cells[r]? = t.cells[r]?
  reads : ∀ c, R c → s.cellAt c = t.cellAt c

theorem Sees.cellAt {r : Nat} {R : Addr → Prop} {s t : State W} (h : Sees r R s t) {c : Addr}
    (hc : R c ∨ c.region = r) : s.cellAt c = t.cellAt c := by
  rcases hc with hc | hc
  · exact h.reads c hc
  · unfold State.cellAt; rw [hc, h.cells]

theorem Sees.objAt {r : Nat} {R : Addr → Prop} {s t : State W} (h : Sees r R s t) {a : Addr} (ha : a.region = r) :
    s.objAt a = t.objAt a := by
  unfold State.objAt; rw [ha, h.heap]

theorem Sees.put {r : Nat} {R : Addr → Prop} {s t : State W} (h : Sees r R s t) (H : Array Obj) (C : Array Binding) :
    Sees r R (s.put r H C) (t.put r H C) := by
  have hh := lt_iff_of_getElem? h.heap
  have hc := lt_iff_of_getElem? h.cells
  refine ⟨h.region, h.region', ?_, ?_, fun c hR => ?_⟩
  · simp only [State.put, set!_getElem?, ↓reduceIte, hh]
  · simp only [State.put, set!_getElem?, ↓reduceIte, hc]
  · rw [State.cellAt_put, State.cellAt_put]
    by_cases hcr : c.region = r
    · by_cases hlt : r < s.cells.size
      · simp [hcr, hlt, hc.1 hlt]
      · have : ¬ r < t.cells.size := fun h' => hlt (hc.2 h')
        simp [hcr, hlt, this]
    · simp only [hcr, ↓reduceIte]; exact h.reads c hR

/-- Run in region `r`, `m` sees only region `r` and the bindings `R`, and replaces region `r`. -/
def Local (r : Nat) (R : Addr → Prop) {α : Type} (m : M W α) : Prop :=
  ∀ s t a s', Sees r R s t → m s = .ok a s' → ∃ H C, s' = s.put r H C ∧ m t = .ok a (t.put r H C)

section local_
variable {r : Nat} {R : Addr → Prop}

theorem Local.same {s t : State W} (h : Sees r R s t) :
    ∃ H C, s = s.put r H C ∧ t = t.put r H C :=
  ⟨s.heap[r]?.getD #[], s.cells[r]?.getD #[],
    by simp only [State.put, set!_same rfl],
    by simp only [State.put, h.heap, h.cells, set!_same rfl]⟩

/-- A computation that ends well without changing the state, with a value that depends on what it sees. -/
theorem Local.of_view {α : Type} {m : M W α}
    (h : ∀ s t a s', Sees r R s t → m s = .ok a s' → s' = s ∧ m t = .ok a t) : Local r R m := by
  intro s t a s' hst hm
  obtain ⟨hs', hmt⟩ := h s t a s' hst hm
  obtain ⟨H, C, hs, ht⟩ := Local.same hst
  exact ⟨H, C, hs'.trans hs, ht ▸ hmt⟩

theorem local_pure {α : Type} (a : α) : Local r R (pure a : M W α) :=
  Local.of_view fun _ _ _ _ _ h => by cases h; exact ⟨rfl, rfl⟩

theorem local_throwVal {α : Type} (v : Val) : Local r R (throwVal v : M W α) := fun _ _ _ _ _ h => by cases h
theorem local_throwErr {α : Type} (k : ErrorKind) (msg : String) : Local r R (throwErr k msg : M W α) :=
  fun _ _ _ _ _ h => by cases h
theorem local_unknown {α : Type} (why : String) : Local r R (unknown why : M W α) := fun _ _ _ _ _ h => by cases h
theorem local_outOfFuel {α : Type} : Local r R (outOfFuel : M W α) := fun _ _ _ _ _ h => by cases h

theorem local_lift {α : Type} (x : Except Abort α) : Local r R (lift x : M W α) := by
  rcases x with (_ | _) | a
  · exact local_throwErr _ _
  · exact local_unknown _
  · exact local_pure a

/-- `m`, then `f` of what it gave. `f` need only be local for the values `m` can give in region `r`. -/
theorem Local.bind {α β : Type} {m : M W α} {f : α → M W β} (hm : Local r R m)
    (hf : ∀ a, (∃ s s', s.region = r ∧ m s = .ok a s') → Local r R (f a)) : Local r R (m >>= f) := by
  intro s t b s'' hst h
  obtain ⟨a, s1, hm1, hf1⟩ := bind_ok h
  obtain ⟨H, C, hs1, hmt⟩ := hm s t a s1 hst hm1
  subst hs1
  obtain ⟨H', C', hs'', hft⟩ := hf a ⟨s, _, hst.region, hm1⟩ _ _ b s'' (hst.put H C) hf1
  refine ⟨H', C', by rw [hs'', State.put_put], ?_⟩
  show (match m t with
    | .ok a s' => f a s'
    | .throw v s' => .throw v s'
    | .unknown why s' => .unknown why s'
    | .fuel => .fuel : Res W β) = _
  rw [hmt]
  exact hft.trans (by rw [State.put_put])

theorem Local.bind' {α β : Type} {m : M W α} {f : α → M W β} (hm : Local r R m) (hf : ∀ a, Local r R (f a)) :
    Local r R (m >>= f) :=
  hm.bind fun a _ => hf a

theorem Local.seq {α β : Type} {m : M W α} {k : M W β} (hm : Local r R m) (hk : Local r R k) :
    Local r R (m >>= fun _ => k) :=
  hm.bind' fun _ => hk

theorem local_getObj {a : Addr} (ha : a.region = r) : Local r R (getObj a : M W Obj) := by
  refine Local.of_view fun s t o s' hst h => ?_
  unfold getObj at h ⊢
  rw [← hst.objAt ha]
  cases hs : s.objAt a <;> rw [hs] at h <;> cases h
  exact ⟨rfl, rfl⟩

/-- A step that looks at the state: local if what it does depends only on what region `r` sees. -/
theorem local_getState_bind {α : Type} {g : State W → M W α}
    (h : ∀ s t, Sees r R s t → g s = g t ∧ Local r R (g s)) : Local r R (getState >>= g) := by
  intro s t a s' hst hm
  change g s s = _ at hm
  obtain ⟨heq, hl⟩ := h s t hst
  obtain ⟨H, C, hs', ht⟩ := hl s t a s' hst hm
  exact ⟨H, C, hs', by change g t t = _; rw [← heq]; exact ht⟩

theorem local_readVar {env : Env} {x : Name} (hx : ∀ c, env.vars.lookup x = some c → R c ∨ c.region = r) :
    Local r R (readVar env x : M W Val) := by
  unfold readVar
  cases hl : env.vars.lookup x with
  | none => exact local_throwErr _ _
  | some c =>
    refine local_getState_bind fun s t hst => ⟨by simp only [hst.cellAt (hx c hl)], ?_⟩
    split
    · exact local_pure _
    · exact local_throwErr _ _

theorem alloc_region {o : Obj} {s s' : State W} {a : Addr} (h : alloc o s = .ok a s') : a.region = s.region := by
  unfold alloc at h
  split at h
  · cases h; rfl
  · cases h

theorem newCell_region {b : Binding} {s s' : State W} {a : Addr} (h : newCell b s = .ok a s') :
    a.region = s.region := by
  unfold newCell at h
  split at h
  · cases h; rfl
  · cases h

theorem State.with_heap (s : State W) (r : Nat) (H : Array Obj) :
    ({ s with heap := s.heap.set! r H } : State W) = s.put r H (s.cells[r]?.getD #[]) := by
  unfold State.put; rw [set!_same rfl]

theorem State.with_cells (s : State W) (r : Nat) (C : Array Binding) :
    ({ s with cells := s.cells.set! r C } : State W) = s.put r (s.heap[r]?.getD #[]) C := by
  unfold State.put; rw [set!_same rfl]

theorem local_alloc (o : Obj) : Local r R (alloc o : M W Addr) := by
  intro s t a s' hst h
  unfold alloc at h ⊢
  cases hh : s.heap[s.region]? with
  | none => rw [hh] at h; cases h
  | some objs =>
    rw [hh] at h
    cases h
    have ht : t.heap[t.region]? = some objs := by rw [hst.region', ← hst.heap, ← hst.region, hh]
    rw [ht]
    refine ⟨objs.push o, s.cells[r]?.getD #[], ?_, ?_⟩
    · rw [State.with_heap, hst.region]
    · show Res.ok _ { t with heap := t.heap.set! t.region (objs.push o) } = _
      rw [State.with_heap, hst.region', hst.region, hst.cells]

theorem local_newCell (b : Binding) : Local r R (newCell b : M W Addr) := by
  intro s t a s' hst h
  unfold newCell at h ⊢
  cases hh : s.cells[s.region]? with
  | none => rw [hh] at h; cases h
  | some bs =>
    rw [hh] at h
    cases h
    have ht : t.cells[t.region]? = some bs := by rw [hst.region', ← hst.cells, ← hst.region, hh]
    rw [ht]
    refine ⟨s.heap[r]?.getD #[], bs.push b, ?_, ?_⟩
    · rw [State.with_cells, hst.region]
    · show Res.ok _ { t with cells := t.cells.set! t.region (bs.push b) } = _
      rw [State.with_cells, hst.region', hst.region, hst.heap]

theorem modify_eq_set! {α : Type} [Inhabited α] (xs : Array α) (i : Nat) (f : α → α) (d : α) :
    xs.modify i f = xs.set! i (f (xs[i]?.getD d)) := by
  refine Array.ext_getElem? fun j => ?_
  rw [Array.getElem?_modify, set!_getElem?]
  by_cases hij : i = j
  · subst hij
    by_cases hlt : i < xs.size
    · simp [hlt]
    · simp [hlt]
  · simp [hij]

theorem local_modifyObj {a : Addr} (ha : a.region = r) (f : Obj → Obj) : Local r R (modifyObj a f : M W Unit) := by
  intro s t u s' hst h
  unfold modifyObj at h ⊢
  cases h
  refine ⟨(s.heap[r]?.getD #[]).modify a.index f, s.cells[r]?.getD #[], ?_, ?_⟩
  · rw [modify_eq_set! _ _ _ #[], ha, State.with_heap]
  · rw [modify_eq_set! _ _ _ #[], ha, State.with_heap, hst.heap, hst.cells]

theorem local_setCell {c : Addr} (hc : c.region = r) (v : Val) : Local r R (setCell c v : M W Unit) := by
  intro s t u s' hst h
  unfold setCell at h ⊢
  cases h
  refine ⟨s.heap[r]?.getD #[], (s.cells[r]?.getD #[]).modify c.index fun b => { b with value := some v }, ?_, ?_⟩
  · rw [modify_eq_set! _ _ _ #[], hc, State.with_cells]
  · rw [modify_eq_set! _ _ _ #[], hc, State.with_cells, hst.heap, hst.cells]

theorem local_defineOwn {a : Addr} (ha : a.region = r) (k : String) (d : Desc) :
    Local r R (defineOwn a k d : M W Unit) := by
  unfold defineOwn
  refine (local_getObj ha).bind' fun o => ?_
  split
  · exact local_unknown _
  · refine (local_lift _).bind' fun p => ?_
    split
    · exact local_modifyObj ha _
    · exact local_throwErr _ _

theorem local_newCells : ∀ (bs : List (Name × Binding)), Local r R (newCells bs : M W (List (Name × Addr)))
  | [] => local_pure _
  | (_, b) :: rest => by
    unfold newCells
    exact (local_newCell b).bind' fun _ => (local_newCells rest).bind' fun _ => local_pure _

theorem local_declare (env : Env) (bs : List (Name × Binding)) : Local r R (declare env bs : M W Env) := by
  unfold declare
  exact (local_newCells bs).bind' fun _ => local_pure _

theorem local_selfScope (env : Env) (self : Bool) (x : Name) : Local r R (selfScope env self x : M W Env) := by
  unfold selfScope
  split
  · exact local_declare _ _
  · exact local_pure _

/-- The binding `selfScope` makes for the name is in the region it ran in. -/
theorem selfScope_lookup {env env' : Env} {x : Name} {s s' : State W} (h : selfScope env true x s = .ok env' s') :
    ∃ c, env'.vars.lookup x = some c ∧ c.region = s.region := by
  unfold selfScope at h
  simp only [ite_true] at h
  unfold declare at h
  obtain ⟨frame, t, hf, h⟩ := bind_ok h
  unfold newCells at hf
  obtain ⟨c, t1, hc, hf⟩ := bind_ok hf
  unfold newCells at hf
  cases hf
  cases h
  exact ⟨c, by simp, newCell_region hc⟩

theorem local_initVar {env : Env} {x : Name} (hx : ∀ c, env.vars.lookup x = some c → c.region = r) (v : Val) :
    Local r R (initVar env x v : M W Unit) := by
  unfold initVar
  split
  · rename_i c hc
    exact local_setCell (hx c hc) v
  · exact local_unknown _

theorem local_writeVar {env : Env} {x : Name} (hx : ∀ c, env.vars.lookup x = some c → c.region = r) (v : Val) :
    Local r R (writeVar env x v : M W Unit) := by
  unfold writeVar
  cases hl : env.vars.lookup x with
  | none => exact local_throwErr _ _
  | some c =>
    refine local_getState_bind fun s t hst => ⟨by simp only [hst.cellAt (Or.inr (hx c hl))], ?_⟩
    split
    · exact local_setCell (hx c hl) v
    · exact local_throwErr _ _
    · exact local_throwErr _ _

theorem local_makeFunc (env : Env) (f : Func) : Local r R (makeFunc env f : M W Val) := by
  unfold makeFunc
  refine (local_selfScope env f.self f.name).bind fun env1 ⟨s1, s1', hr1, h1⟩ => ?_
  refine (local_alloc _).bind fun a ⟨s2, s2', hr2, h2⟩ => ?_
  have ha : a.region = r := (alloc_region h2).trans hr2
  refine Local.bind' ?_ fun _ => Local.bind' ?_ fun _ => local_pure _
  · split
    · unfold addPrototype
      exact (local_alloc _).bind' fun _ => local_modifyObj ha _
    · exact local_pure _
  · unfold initSelf
    split
    · rename_i hself
      rw [hself] at h1
      obtain ⟨c, hc, hcr⟩ := selfScope_lookup h1
      exact local_initVar (fun c' hc' => by rw [hc] at hc'; cases hc'; exact hcr.trans hr1) _
    · exact local_pure _

theorem local_evalExpr_str (n : Nat) (env : Env) (key : String) :
    Local r R (evalExpr n env (.str key) : M W Val) := by
  cases n with
  | zero => exact local_outOfFuel
  | succ n => exact local_pure _

theorem local_evalMembers_plain (n : Nat) (env : Env) (proto c : Addr) (ms : List ClassMember)
    (h : plainClassMembers ms = true) (hp : proto.region = r) (hc : c.region = r) :
    Local r R (evalMembers n env proto c ms : M W (List (Bool × String))) := by
  induction n generalizing ms with
  | zero => exact local_outOfFuel
  | succ n ih =>
    cases ms with
    | nil => exact local_pure _
    | cons m ms =>
      cases m <;> rename_i static k f <;> cases k <;>
        simp only [plainClassMembers, Bool.false_eq_true] at h
      all_goals
        unfold evalMembers
        refine (local_evalExpr_str n env _).bind' fun key => ?_
        refine (local_lift _).bind' fun name => ?_
        refine (local_makeFunc env f).bind' fun value => ?_
        refine (local_defineOwn ?_ name _).bind' fun _ => ?_
        · cases static <;> assumption
        · exact (local_pure _).bind' fun field => (ih ms h).bind' fun _ => local_pure _

/-- Once its prototype parents are known, an anonymous class with plain members is local. -/
theorem local_finishDerived_plain (n : Nat) (env : Env) (name : Name) (members : List ClassMember)
    (parents : Addr × Option Addr) (h : plainClassMembers members = true) :
    Local r R (finishDerived (evalMembers n) env name false members parents : M W Val) := by
  unfold finishDerived
  refine (local_alloc _).bind fun proto ⟨s1, s1', hr1, h1⟩ => ?_
  have hp : proto.region = r := (alloc_region h1).trans hr1
  refine (local_alloc _).bind fun c ⟨s2, s2', hr2, h2⟩ => ?_
  have hc : c.region = r := (alloc_region h2).trans hr2
  refine (local_modifyObj hp _).bind' fun _ => ?_
  refine (local_evalMembers_plain n env proto c members h hp hc).bind' fun _ => ?_
  exact (local_pure ()).bind' fun _ => local_pure _

end local_

/-! ## An expression that only allocates is local -/

/-- Every binding that the names `N` find in `env` is in region `r` or among `R`. -/
def Within (r : Nat) (R : Addr → Prop) (env : Env) (N : List Name) : Prop :=
  ∀ z ∈ N, ∀ c, env.vars.lookup z = some c → R c ∨ c.region = r

theorem allocOnly_local (r : Nat) (R : Addr → Prop) (n : Nat) :
    (∀ env e, e.allocOnly = true → Within r R env e.readsNow → Local r R (evalExpr n env e : M W Val)) ∧
    (∀ env es, allocOnlyAll es = true → Within r R env (readsNowAll es) →
      Local r R (evalArgs n env es : M W (List Val))) ∧
    (∀ env a ps, allocOnlyProps ps = true → Within r R env (readsNowProps ps) → a.region = r →
      Local r R (evalProps n env a ps : M W Unit)) := by
  induction n with
  | zero =>
    refine ⟨fun env e _ _ => ?_, fun env es _ _ => ?_, fun env a ps _ _ _ => ?_⟩
    · unfold evalExpr; exact local_outOfFuel
    · unfold evalArgs; exact local_outOfFuel
    · unfold evalProps; exact local_outOfFuel
  | succ n ih =>
    obtain ⟨ihE, ihA, ihP⟩ := ih
    refine ⟨fun env e he hw => ?_, fun env es he hw => ?_, fun env a ps he hw ha => ?_⟩
    · cases e <;> simp only [Expr.allocOnly, Bool.false_eq_true] at he <;> unfold evalExpr
      case undef | null | bool | num | str | host | this => exact local_pure _
      case var x => exact local_readVar (hw x (by simp [Expr.readsNow]))
      case func f => exact local_makeFunc env f
      case obj props =>
        refine (local_alloc _).bind fun a ⟨s, s', hr, h⟩ => ?_
        exact (ihP env a props he (by simpa [Expr.readsNow] using hw) ((alloc_region h).trans hr)).bind' fun _ =>
          local_pure _
      case cls name self ctor members =>
        refine (local_selfScope env self name).bind fun env1 ⟨s1, s1', hr1, h1⟩ => ?_
        refine (local_alloc _).bind fun proto ⟨s2, s2', hr2, h2⟩ => ?_
        have hp : proto.region = r := (alloc_region h2).trans hr2
        refine (local_alloc _).bind fun c ⟨s3, s3', hr3, h3⟩ => ?_
        have hc : c.region = r := (alloc_region h3).trans hr3
        refine (local_modifyObj hp _).bind' fun _ => ?_
        refine (local_evalMembers_plain n env1 proto c members he hp hc).bind' fun keys => ?_
        refine (local_modifyObj hc _).bind' fun _ => ?_
        refine Local.bind' ?_ fun _ => ?_
        · unfold initSelf
          split
          · rename_i hself
            rw [hself] at h1
            obtain ⟨cell, hl, hr⟩ := selfScope_lookup h1
            exact local_initVar (fun cell' hl' => by rw [hl] at hl'; cases hl'; exact hr.trans hr1) _
          · exact local_pure _
        · rw [plainClassMembers_fieldInits he]
          have hz : keys.zip ([] : List Expr) = [] := by cases keys <;> rfl
          rw [hz]
          simp only [List.filterMap_nil]
          cases n with
          | zero => exact local_outOfFuel.bind' fun _ => local_pure _
          | succ n => exact (local_pure _).bind' fun _ => local_pure _
      case arr elems =>
        exact (ihA env elems he (by simpa [Expr.readsNow] using hw)).bind' fun _ =>
          (local_alloc _).bind' fun _ => local_pure _
    · cases es with
      | nil => unfold evalArgs; exact local_pure _
      | cons e es =>
        simp only [allocOnlyAll, Bool.and_eq_true] at he
        unfold evalArgs
        exact (ihE env e he.1 fun z hz => hw z (by simp [readsNowAll, hz])).bind' fun _ =>
          (ihA env es he.2 fun z hz => hw z (by simp [readsNowAll, hz])).bind' fun _ => local_pure _
    · cases ps with
      | nil => unfold evalProps; exact local_pure _
      | cons p ps =>
        cases p with
        | init k v =>
          cases k <;> simp only [allocOnlyProps, Bool.and_eq_true, Bool.false_eq_true] at he
          rename_i key
          unfold evalProps
          refine (ihE env (.str key) rfl fun z hz => by simp [Expr.readsNow] at hz).bind' fun _ => ?_
          refine (local_lift _).bind' fun name => ?_
          refine (ihE env v he.1 fun z hz => hw z (by simp [readsNowProps, hz])).bind' fun _ => ?_
          exact (local_defineOwn ha name _).bind' fun _ =>
            ihP env a ps he.2 (fun z hz => hw z (by simp [readsNowProps, hz])) ha
        | getter | setter | proto => simp only [allocOnlyProps, Bool.false_eq_true] at he

/-- **A declaration whose initializer only allocates is local**: run in region `r`, it sees region `r` and the
bindings of the names its initializer reads, and changes region `r`, when its own binding is in region `r`. -/
theorem decl_local {r : Nat} {R : Addr → Prop} {e : Expr} (he : e.allocOnly = true) (n : Nat) (env : Env)
    (kind : DeclKind) (x : Name) (hw : Within r R env e.readsNow)
    (hx : ∀ c, env.vars.lookup x = some c → c.region = r) :
    Local r R (execStmt n env (.decl kind x e) : M W Flow) := by
  cases n with
  | zero => unfold execStmt; exact local_outOfFuel
  | succ n =>
    cases kind <;> unfold execStmt
    case var =>
      exact ((allocOnly_local r R n).1 env e he hw).bind' fun v => (local_writeVar hx v).bind' fun _ => local_pure _
    all_goals
      exact ((allocOnly_local r R n).1 env e he hw).bind' fun v => (local_initVar hx v).bind' fun _ => local_pure _

theorem local_makeOpaque (code part : Nat) (cells : List (Option Addr)) (name : String) (length : Nat)
    (constructor : Bool) : Local r R (makeOpaque code part cells name length constructor : M W Val) := by
  unfold makeOpaque
  refine (local_alloc _).bind fun a ⟨s, s', hr, h⟩ => ?_
  have ha : a.region = r := (alloc_region h).trans hr
  refine Local.bind' ?_ fun _ => local_pure _
  split
  · unfold addPrototype
    exact (local_alloc _).bind' fun _ => local_modifyObj ha _
  · exact local_pure _

theorem local_initClosures {env : Env} (code : Nat) (cells : List (Option Addr)) :
    ∀ (decls : List (Name × DeclKind)) (fns : List (String × Nat × Bool)) (part : Nat),
      (∀ x ∈ decls.map (·.1), ∀ c, env.vars.lookup x = some c → c.region = r) →
      Local r R (initClosures code cells env decls fns part : M W Unit)
  | [], [], _, _ => by unfold initClosures; exact local_pure _
  | [], _ :: _, _, _ => by unfold initClosures; exact local_unknown _
  | _ :: _, [], _, _ => by unfold initClosures; exact local_unknown _
  | (x, kind) :: decls, (name, length, constructor) :: fns, part, h => by
    unfold initClosures
    refine (local_makeOpaque _ _ _ _ _ _).bind' fun v => Local.bind' ?_ fun _ =>
      local_initClosures code cells decls fns (part + 1) fun y hy => h y (List.mem_cons_of_mem _ hy)
    split
    · exact local_writeVar (h x List.mem_cons_self) v
    · exact local_initVar (h x List.mem_cons_self) v

/-- **An opaque statement that only makes functions is local**: run in region `r`, it sees only region `r` and
changes region `r`, when the bindings it declares are in region `r`. -/
theorem closures_local {r : Nat} {R : Addr → Prop} (n : Nat) (env : Env) (code : Nat)
    (decls : List (Name × DeclKind)) (funs : List (Name × Nat × Bool)) (free : List Name) (fns : List (String × Nat × Bool))
    (hx : ∀ x ∈ decls.map (·.1), ∀ c, env.vars.lookup x = some c → c.region = r) :
    Local r R (execStmt n env (.opaque code decls funs free (.closures fns)) : M W Flow) := by
  cases n with
  | zero => unfold execStmt; exact local_outOfFuel
  | succ n =>
    unfold execStmt
    exact (local_initClosures code _ decls fns _ hx).bind' fun _ => local_pure _

/-! ## Two units commute -/

/-- `B` can run before `A`: whenever `A` then `B` ends well, `B` then `A` ends well in the same state, each with the
same value. -/
def Commute {α β : Type} (A : M W α) (B : M W β) : Prop :=
  ∀ s u s' a b, A s = .ok a u → B u = .ok b s' → ∃ u', B s = .ok b u' ∧ A u' = .ok a s'

theorem bind_of_ok {α β : Type} {m : M W α} {f : α → M W β} {s s' : State W} {a : α} (h : m s = .ok a s') :
    (m >>= f) s = f a s' := by
  show (match m s with
    | .ok a s' => f a s'
    | .throw v s' => .throw v s'
    | .unknown why s' => .unknown why s'
    | .fuel => .fuel : Res W β) = _
  rw [h]

theorem inRegion_ok_state {α : Type} {id : Nat} {m : M W α} {s s' : State W} {f : α}
    (h : inRegion id m s = .ok f s') :
    ∃ u, m { s with region := id + 1 } = .ok f u ∧ s' = { u with region := 0 } := by
  unfold inRegion at h
  obtain ⟨_, t0, h0, h⟩ := bind_ok h
  cases h0
  obtain ⟨f', t1, h1, h⟩ := bind_ok h
  obtain ⟨_, t2, h2, h⟩ := bind_ok h
  cases h2
  cases h
  exact ⟨t1, h1, rfl⟩

theorem inRegion_of {α : Type} {id : Nat} {m : M W α} {s u : State W} {f : α}
    (h : m { s with region := id + 1 } = .ok f u) :
    inRegion id m s = .ok f { u with region := 0 } := by
  unfold inRegion
  change (m >>= fun a => (modifyState (fun s : State W => { s with region := 0 }) >>= fun _ => pure a))
    { s with region := id + 1 } = _
  rw [bind_of_ok h]
  rfl

theorem runUnit_ok {n id : Nat} {env : Env} {st : Stmt} {s s' : State W} {f : Flow}
    (h : runUnit n env id st s = .ok f s') :
    ∃ u, execStmt n env st { s with region := id + 1 } = .ok f u ∧ s' = { u with region := 0 } := inRegion_ok_state h

theorem runUnit_of {n id : Nat} {env : Env} {st : Stmt} {s u : State W} {f : Flow}
    (h : execStmt n env st { s with region := id + 1 } = .ok f u) :
    runUnit n env id st s = .ok f { u with region := 0 } := inRegion_of h

theorem Sees.symm {r : Nat} {R : Addr → Prop} {s t : State W} (h : Sees r R s t) : Sees r R t s :=
  ⟨h.region', h.region, h.heap.symm, h.cells.symm, fun c hc => (h.reads c hc).symm⟩

theorem Sees.refl {r : Nat} {R : Addr → Prop} {s : State W} (h : s.region = r) : Sees r R s s :=
  ⟨h, h, rfl, rfl, fun _ _ => rfl⟩

/-- A state that another region's replacement made sees region `r` as before. -/
theorem Sees.put_other {r q : Nat} {R : Addr → Prop} {s : State W} (hq : q ≠ r) (hR : ∀ c, R c → c.region ≠ q)
    (H : Array Obj) (C : Array Binding) (k : Nat) (hk : k = r) :
    Sees r R { s.put q H C with region := k } { s with region := k } := by
  subst hk
  refine ⟨rfl, rfl, ?_, ?_, fun c hc => ?_⟩
  · show (s.heap.set! q H)[k]? = s.heap[k]?
    rw [set!_getElem?]; simp [hq]
  · show (s.cells.set! q C)[k]? = s.cells[k]?
    rw [set!_getElem?]; simp [hq]
  · show (s.put q H C).cellAt c = s.cellAt c
    rw [State.cellAt_put]; simp [hR c hc]

/-- **Two local computations in different regions commute**, when neither reads a binding of the other's region. -/
theorem local_inRegion_commute {α β : Type} {i j : Nat} {a : M W α} {b : M W β} {R1 R2 : Addr → Prop} (hij : i ≠ j)
    (h1 : Local (i + 1) R1 a) (h2 : Local (j + 1) R2 b)
    (hR1 : ∀ c, R1 c → c.region ≠ j + 1) (hR2 : ∀ c, R2 c → c.region ≠ i + 1) :
    Commute (inRegion i a) (inRegion j b) := by
  intro s u s' fa fb ha hb
  have hne : i + 1 ≠ j + 1 := fun h => hij (by omega)
  obtain ⟨u0, hu0, rfl⟩ := inRegion_ok_state ha
  obtain ⟨v0, hv0, rfl⟩ := inRegion_ok_state hb
  obtain ⟨H1, C1, rfl, -⟩ := h1 _ _ fa u0 (Sees.refl rfl) hu0
  -- `j` sees the same after `i` as before it
  obtain ⟨H2, C2, rfl, hj⟩ := h2 _ { s with region := j + 1 } fb v0
    (Sees.put_other hne (fun c hc => (hR2 c hc)) H1 C1 (j + 1) rfl) hv0
  refine ⟨_, inRegion_of hj, ?_⟩
  -- and `i` sees the same after `j` as before it
  obtain ⟨H, C, hu, hi⟩ := h1 { s with region := i + 1 } { s.put (j + 1) H2 C2 with region := i + 1 } fa _
    (Sees.put_other (s := s) hne.symm hR1 H2 C2 (i + 1) rfl).symm hu0
  refine (inRegion_of hi).trans ?_
  have hh : s.heap.set! (i + 1) H1 = s.heap.set! (i + 1) H := congrArg State.heap hu
  have hc : s.cells.set! (i + 1) C1 = s.cells.set! (i + 1) C := congrArg State.cells hu
  simp only [State.put]
  rw [hh, hc, set!_comm s.heap hne, set!_comm s.cells hne]

/-- **Two local units in different regions commute**, when neither reads a binding of the other's region. -/
theorem local_units_commute {n i j : Nat} {e1 e2 : Env} {st1 st2 : Stmt} {R1 R2 : Addr → Prop} (hij : i ≠ j)
    (h1 : Local (i + 1) R1 (execStmt n e1 st1 : M W Flow)) (h2 : Local (j + 1) R2 (execStmt n e2 st2 : M W Flow))
    (hR1 : ∀ c, R1 c → c.region ≠ j + 1) (hR2 : ∀ c, R2 c → c.region ≠ i + 1) :
    Commute (runUnit n e1 i st1 : M W Flow) (runUnit n e2 j st2) :=
  local_inRegion_commute hij h1 h2 hR1 hR2

/-! ## A unit that does nothing when it runs commutes with every unit

A function declaration, a `var` declaration without initializer and an opaque statement that declares only functions
do their work when the program is linked. -/

theorem runUnit_region (n id : Nat) (env : Env) (st : Stmt) (s : State W) (k : Nat) :
    runUnit n env id st { s with region := k } = runUnit n env id st s := rfl

theorem runUnit_region_zero {n id : Nat} {env : Env} {st : Stmt} {s s' : State W} {f : Flow}
    (h : runUnit n env id st s = .ok f s') : s' = { s' with region := 0 } := by
  obtain ⟨u, _, rfl⟩ := runUnit_ok h
  rfl

theorem inert_unit {n id : Nat} {env : Env} {st : Stmt} (hst : st.inert = true) {u s' : State W} {f : Flow}
    (h : runUnit n env id st u = .ok f s') :
    f = .normal ∧ s' = { u with region := 0 } ∧ ∀ x : State W, runUnit n env id st x = .ok .normal { x with region := 0 } := by
  obtain ⟨u0, hu0, rfl⟩ := runUnit_ok h
  cases n with
  | zero => unfold execStmt at hu0; cases hu0
  | succ n =>
    have hrun : ∀ x : State W, execStmt (n + 1) env st x = .ok .normal x := by
      intro x
      cases st with
      | «opaque» code decls funs free run =>
        cases decls with
        | cons => simp [Stmt.inert] at hst
        | nil =>
          cases run with
          | world => simp [Stmt.inert] at hst
          | closures fns =>
            cases fns with
            | cons => simp [Stmt.inert] at hst
            | nil => unfold execStmt; rfl
      | _ => first | (unfold execStmt; rfl) | simp [Stmt.inert] at hst
    rw [hrun] at hu0
    cases hu0
    exact ⟨rfl, rfl, fun x => runUnit_of (s := x) (u := { x with region := id + 1 }) (hrun _)⟩

theorem commute_inert {n i j : Nat} {e1 e2 : Env} {st1 st2 : Stmt} (h2 : st2.inert = true) :
    Commute (runUnit n e1 i st1 : M W Flow) (runUnit n e2 j st2) := by
  intro s u s' a b ha hb
  obtain ⟨rfl, rfl, hall⟩ := inert_unit h2 hb
  refine ⟨_, hall s, ?_⟩
  rw [runUnit_region, ha]
  exact congrArg _ (runUnit_region_zero ha)

theorem inert_commute {n i j : Nat} {e1 e2 : Env} {st1 st2 : Stmt} (h1 : st1.inert = true) :
    Commute (runUnit n e1 i st1 : M W Flow) (runUnit n e2 j st2) := by
  intro s u s' a b ha hb
  obtain ⟨rfl, rfl, hall⟩ := inert_unit h1 ha
  rw [runUnit_region] at hb
  refine ⟨s', hb, ?_⟩
  rw [hall s']
  exact congrArg _ (runUnit_region_zero hb).symm

/-! ## Which pairs may change their order -/

theorem mayMove_symm (a b : Stmt) : mayMove a b = mayMove b a := by
  unfold mayMove
  rcases a.allocs with _ | ⟨xs, rs⟩ <;> rcases b.allocs with _ | ⟨ys, rs'⟩ <;>
    simp only [Bool.or_comm, Bool.and_comm]

theorem allocs_eq {s : Stmt} {xs rs : List Name} (h : s.allocs = some (xs, rs)) :
    (∃ k x e, s = .decl k x e ∧ e.allocOnly = true ∧ xs = [x] ∧ rs = e.readsNow) ∨
      ∃ code decls funs free fns, s = .opaque code decls funs free (.closures fns) ∧
        xs = decls.map (·.1) ++ funs.map (·.1) ∧ rs = [] := by
  cases s <;> simp only [Stmt.allocs, reduceCtorEq] at h
  case decl k x e =>
    by_cases he : e.allocOnly = true
    · simp only [he, ↓reduceIte, Option.some.injEq, Prod.mk.injEq] at h
      exact .inl ⟨k, x, e, rfl, he, h.1.symm, h.2.symm⟩
    · simp [he] at h
  case «opaque» code decls funs free run =>
    cases run with
    | world => simp at h
    | closures fns =>
      simp only [Option.some.injEq, Prod.mk.injEq] at h
      exact .inr ⟨code, decls, funs, free, fns, rfl, h.1.symm, h.2.symm⟩

theorem decl_binding_names {k : DeclKind} {x z : Name} {e : Expr} :
    z ∈ (Stmt.decl k x e).bindings.map (·.1) ↔ z = x := by
  cases k <;> simp [Stmt.bindings, varNamesOf, Stmt.varNames, lexDecls, List.eraseDups, List.eraseDupsBy,
    List.eraseDupsBy.loop]

/-! ## The bindings of the bundle are in the regions of their statements -/

theorem newCells_regions : ∀ (bs : List (Name × Binding)) {s s' : State W} {frame : List (Name × Addr)},
    newCells bs s = .ok frame s' → (∀ p ∈ frame, p.2.region = s.region) ∧ s'.region = s.region
  | [], s, s', frame, h => by unfold newCells at h; cases h; simp
  | (x, b) :: rest, s, s', frame, h => by
    unfold newCells at h
    obtain ⟨c, t1, hc, h⟩ := bind_ok h
    obtain ⟨tail, t2, ht, h⟩ := bind_ok h
    cases h
    have hc' := newCell_region hc
    have ht1 : t1.region = s.region := by
      unfold newCell at hc; split at hc
      · cases hc; rfl
      · cases hc
    obtain ⟨ih1, ih2⟩ := newCells_regions rest ht
    refine ⟨fun p hp => ?_, ih2.trans ht1⟩
    rcases List.mem_cons.1 hp with rfl | hp
    · exact hc'
    · exact (ih1 p hp).trans ht1

theorem declareTops_regions : ∀ (tops : List (Nat × Stmt)) {s s' : State W} {frames : List (Nat × List (Name × Addr))},
    declareTops tops s = .ok frames s' → ∀ q ∈ frames, ∀ p ∈ q.2, p.2.region = q.1 + 1
  | [], s, s', frames, h => by unfold declareTops at h; cases h; simp
  | (id, stmt) :: rest, s, s', frames, h => by
    unfold declareTops at h
    obtain ⟨frame, t1, hframe, h⟩ := bind_ok h
    obtain ⟨tail, t2, htail, h⟩ := bind_ok h
    cases h
    intro q hq p hp
    rcases List.mem_cons.1 hq with rfl | hq
    · unfold inRegion at hframe
      obtain ⟨_, u0, hu0, hframe⟩ := bind_ok hframe
      cases hu0
      obtain ⟨fr, u1, hu1, hframe⟩ := bind_ok hframe
      obtain ⟨_, u2, hu2, hframe⟩ := bind_ok hframe
      cases hframe
      exact (newCells_regions _ hu1).1 p hp
    · exact declareTops_regions rest htail q hq p hp

theorem lookup_mem {α γ : Type} [BEq α] [LawfulBEq α] {k : α} {v : γ} : ∀ {l : List (α × γ)}, l.lookup k = some v → (k, v) ∈ l
  | [], h => by cases h
  | (k', v') :: l, h => by
    by_cases hk : k = k'
    · subst hk; simp only [List.lookup, beq_self_eq_true, Option.some.injEq] at h; subst h; simp
    · have : (k == k') = false := beq_false_of_ne hk
      simp only [List.lookup, this] at h
      exact List.mem_cons_of_mem _ (lookup_mem h)

/-- A name of the bundle's scope means the binding of the statement that declares it, in that statement's region. -/
theorem bundleEnv_lookup {ss : List Stmt} {B : EsmSplit.Bundle} {frames : List (Nat × List (Name × Addr))}
    (hfp : FootprintsOfSyntax ss B) (hF : FramesOf ((List.range ss.length).zip ss) frames)
    (hreg : ∀ q ∈ frames, ∀ p ∈ q.2, p.2.region = q.1 + 1) (hud : EsmSplit.UniqueDefs B) {z : Name} {c : Addr}
    (h : (bundleEnv ss frames).vars.lookup z = some c) : ∃ k < ss.length, z ∈ (B.stmt k).defs ∧ c.region = k + 1 := by
  have hbody : ∀ p, p ∈ (bundleModule ss).body ↔ p.1 < ss.length ∧ ss[p.1]? = some p.2 := by
    intro p
    show p ∈ (List.range ss.length).zip ss ↔ _
    rw [← filterMap_numbered_range, mem_numbered, List.mem_range]
  have h' : ((bundleModule ss).scope frames).lookup z = some c := by simpa [bundleEnv] using h
  by_cases hd : ∃ k < ss.length, z ∈ (B.stmt k).defs
  · obtain ⟨k, hk, hz⟩ := hd
    refine ⟨k, hk, hz, ?_⟩
    rw [scope_lookup_decl hfp hF hud ⟨(k, ss[k]), (hbody _).2 ⟨hk, by simp [hk]⟩, rfl⟩ hk hz] at h'
    cases hl : frames.lookup k with
    | none => simp [frameAt, hl] at h'
    | some fr =>
      simp only [frameAt, hl, Option.getD_some] at h'
      exact hreg (k, fr) (lookup_mem hl) (z, c) (lookup_mem h')
  · rw [scope_lookup_none hfp hF fun p hp hz => hd ⟨p.1, ((hbody p).1 hp).1, hz⟩] at h'
    cases h'

/-! ## Two statements that may move commute in the bundle's scope -/

section commute
variable {ss : List Stmt} {B : EsmSplit.Bundle} {frames : List (Nat × List (Name × Addr))}
  (hfp : FootprintsOfSyntax ss B) (hF : FramesOf ((List.range ss.length).zip ss) frames)
  (hreg : ∀ q ∈ frames, ∀ p ∈ q.2, p.2.region = q.1 + 1) (hud : EsmSplit.UniqueDefs B)
include hfp hF hreg hud

omit hF hreg hud in
/-- The names a statement that only allocates declares are those of its statement. -/
theorem allocs_defs {i : Nat} {s : Stmt} {xs rs : List Name} (hs : ss[i]? = some s) (h : s.allocs = some (xs, rs))
    {z : Name} : z ∈ (B.stmt i).defs ↔ z ∈ xs := by
  rw [(hfp.2 i _ hs).1 z]
  rcases allocs_eq h with ⟨k, x, e, rfl, -, rfl, -⟩ | ⟨code, decls, funs, free, fns, rfl, rfl, -⟩
  · rw [decl_binding_names]; simp
  · simp [Stmt.bindings, Function.comp_def]

/-- What a statement that only allocates reads, in the bundle's scope. -/
def readCells (env : Env) (rs : List Name) (c : Addr) : Prop := ∃ z ∈ rs, env.vars.lookup z = some c

/-- A name that statement `i` declares means a binding of region `i + 1`. -/
theorem own_region {i : Nat} {s : Stmt} {xs rs : List Name} (hs : ss[i]? = some s) (h : s.allocs = some (xs, rs))
    {x : Name} (hx : x ∈ xs) : ∀ c, (bundleEnv ss frames).vars.lookup x = some c → c.region = i + 1 := by
  intro c hc
  obtain ⟨k', hk', hxk, hr⟩ := bundleEnv_lookup hfp hF hreg hud hc
  have hi : i < ss.length := (List.getElem?_eq_some_iff.1 hs).1
  rw [unique_defs hud (hfp.1 ▸ hk') (hfp.1 ▸ hi) hxk ((allocs_defs hfp hs h).2 hx)] at hr
  exact hr

theorem allocs_unit_local {i : Nat} {s : Stmt} {xs rs : List Name} (hs : ss[i]? = some s)
    (h : s.allocs = some (xs, rs)) (n : Nat) :
    Local (i + 1) (readCells (bundleEnv ss frames) rs) (execStmt n (bundleEnv ss frames) s : M W Flow) := by
  have hown : ∀ x ∈ xs, _ := fun x hx => own_region hfp hF hreg hud hs h (x := x) hx
  rcases allocs_eq h with ⟨k, x, e, rfl, he, rfl, rfl⟩ | ⟨code, decls, funs, free, fns, rfl, rfl, rfl⟩
  · exact decl_local he n _ k x (fun z hz c hc => Or.inl ⟨z, hz, hc⟩) (hown x List.mem_cons_self)
  · exact closures_local n _ code decls funs free fns fun x hx => hown x (List.mem_append_left _ hx)

theorem readCells_other {i : Nat} {s : Stmt} {ys rs' rs : List Name} (hs : ss[i]? = some s)
    (h : s.allocs = some (ys, rs')) (hy : rs.any ys.contains = false) {c : Addr}
    (hc : readCells (bundleEnv ss frames) rs c) : c.region ≠ i + 1 := by
  obtain ⟨z, hz, hl⟩ := hc
  obtain ⟨k', hk', hzk, hr⟩ := bundleEnv_lookup hfp hF hreg hud hl
  intro heq
  have hk : k' = i := by omega
  subst hk
  have := (allocs_defs hfp hs h).1 hzk
  simp only [List.any_eq_false, List.contains_iff_mem] at hy
  exact absurd this (by simpa using hy z hz)

theorem mayMove_commute {i j : Nat} {a b : Stmt} (hij : i ≠ j) (ha : ss[i]? = some a) (hb : ss[j]? = some b)
    (hm : mayMove a b = true) (n : Nat) :
    Commute (runUnit n (bundleEnv ss frames) i a : M W Flow) (runUnit n (bundleEnv ss frames) j b) := by
  unfold mayMove at hm
  cases hai : a.inert
  · cases hbi : b.inert
    · simp only [hai, hbi, Bool.false_or] at hm
      cases hda : a.allocs with
      | none => simp [hda] at hm
      | some p =>
        cases hdb : b.allocs with
        | none => simp [hda, hdb] at hm
        | some q =>
          obtain ⟨xs, rs⟩ := p
          obtain ⟨ys, rs'⟩ := q
          simp only [hda, hdb, Bool.and_eq_true, Bool.not_eq_true'] at hm
          exact local_units_commute hij (allocs_unit_local hfp hF hreg hud ha hda n)
            (allocs_unit_local hfp hF hreg hud hb hdb n) (fun c hc => readCells_other hfp hF hreg hud hb hdb hm.1 hc)
            (fun c hc => readCells_other hfp hF hreg hud ha hda hm.2 hc)
    · exact commute_inert hbi
  · exact inert_commute hai

end commute

/-! ## Running units in another order -/

/-- The states `A` can end well in, from a state of `X`. -/
def Img {α : Type} (A : M W α) (X : State W → Prop) : State W → Prop := fun s' => ∃ s a, X s ∧ A s = .ok a s'

/-- A successful swap from a state satisfying `P`. -/
def CommuteOn {α β : Type} (P : State W → Prop) (A : M W α) (B : M W β) : Prop :=
  ∀ s, P s → ∀ u s' a b, A s = .ok a u → B u = .ok b s' → ∃ u', B s = .ok b u' ∧ A u' = .ok a s'

theorem img_comm_on {α β : Type} {P X : State W → Prop} {A : M W α} {B : M W β}
    (hX : ∀ s, X s → P s) (hab : CommuteOn P A B) (hba : CommuteOn P B A) :
    Img B (Img A X) = Img A (Img B X) := by
  funext s'
  apply propext
  constructor
  · rintro ⟨u, b, ⟨s, a, hx, ha⟩, hb⟩
    obtain ⟨u', hb', ha'⟩ := hab s (hX s hx) u s' a b ha hb
    exact ⟨u', a, ⟨s, b, hx, hb'⟩, ha'⟩
  · rintro ⟨u, a, ⟨s, b, hx, hb⟩, ha⟩
    obtain ⟨u', ha', hb'⟩ := hba s (hX s hx) u s' b a hb ha
    exact ⟨u', b, ⟨s, a, hx, ha'⟩, hb'⟩

theorem img_comm {α β : Type} {A : M W α} {B : M W β} (hab : Commute A B) (hba : Commute B A) (X : State W → Prop) :
    Img B (Img A X) = Img A (Img B X) :=
  img_comm_on (P := fun _ => True) (fun _ _ => trivial) (fun s _ => hab s) (fun s _ => hba s)

def unitStep (n : Nat) (u : Nat × Stmt × Env) : M W Flow := runUnit n u.2.2 u.1 u.2.1

theorem runUnits_img (n : Nat) : ∀ (us : List (Nat × Stmt × Env)) (X : State W → Prop) (s' : State W),
    (∃ s, X s ∧ runUnits n us s = .ok () s') ↔ us.foldl (fun X u => Img (unitStep n u) X) X s'
  | [], X, s' => by
    simp only [List.foldl]
    constructor
    · rintro ⟨s, hX, h⟩; cases h; exact hX
    · intro hX; exact ⟨s', hX, rfl⟩
  | u :: us, X, s' => by
    rw [List.foldl_cons, ← runUnits_img n us]
    obtain ⟨id, st, env⟩ := u
    simp only [unitStep]
    constructor
    · rintro ⟨s, hX, h⟩
      unfold runUnits at h
      obtain ⟨f, t, ht, h⟩ := bind_ok h
      exact ⟨t, ⟨s, f, hX, ht⟩, h⟩
    · rintro ⟨t, ⟨s, f, hX, ht⟩, h⟩
      refine ⟨s, hX, ?_⟩
      unfold runUnits
      rw [bind_of_ok ht]
      exact h

/-- A unit can cross a block after a prefix establishes the condition for each successive swap. -/
theorem runUnits_pivot (n : Nat) (pre crossed suffix : List (Nat × Stmt × Env)) (pivot : Nat × Stmt × Env)
    (P : State W → Prop) (s s' : State W)
    (hprefix : ∀ t, runUnits n pre s = .ok () t → P t)
    (hkeep : ∀ u ∈ crossed, ∀ t t' f, P t → unitStep n u t = .ok f t' → P t')
    (hcomm : ∀ u ∈ crossed, CommuteOn P (unitStep n pivot) (unitStep n u) ∧
      CommuteOn P (unitStep n u) (unitStep n pivot)) :
    runUnits n (pre ++ [pivot] ++ crossed ++ suffix) s = .ok () s' ↔
      runUnits n (pre ++ crossed ++ [pivot] ++ suffix) s = .ok () s' := by
  let step (X : State W → Prop) (u : Nat × Stmt × Env) := Img (unitStep n u) X
  have hfold : ∀ (us : List (Nat × Stmt × Env)) (X : State W → Prop),
      (∀ t, X t → P t) →
      (∀ u ∈ us, ∀ t t' f, P t → unitStep n u t = .ok f t' → P t') →
      (∀ u ∈ us, CommuteOn P (unitStep n pivot) (unitStep n u) ∧
        CommuteOn P (unitStep n u) (unitStep n pivot)) →
      us.foldl step (step X pivot) = step (us.foldl step X) pivot := by
    intro us
    induction us with
    | nil => intros; rfl
    | cons u us ih =>
      intro X hX hk hc
      obtain ⟨hab, hba⟩ := hc u List.mem_cons_self
      simp only [List.foldl_cons]
      rw [show step (step X pivot) u = step (step X u) pivot from img_comm_on hX hab hba]
      refine ih (step X u) ?_ (fun v hv => hk v (List.mem_cons_of_mem u hv)) fun v hv =>
        hc v (List.mem_cons_of_mem u hv)
      rintro t' ⟨t, f, ht, hrun⟩
      exact hk u List.mem_cons_self t t' f (hX t ht) hrun
  have hrun (us : List (Nat × Stmt × Env)) (t : State W) :
      runUnits n us s = .ok () t ↔ us.foldl step (· = s) t := by
    rw [← runUnits_img]
    exact ⟨fun h => ⟨s, rfl, h⟩, fun ⟨_, rfl, h⟩ => h⟩
  rw [hrun, hrun]
  simp only [List.foldl_append, List.foldl_cons, List.foldl_nil]
  rw [hfold crossed _ (fun t ht => hprefix t ((hrun pre t).2 ht)) hkeep hcomm]

/-- The unit of the bundle with identity `j`. -/
def bundleUnit (ss : List Stmt) (frames : List (Nat × List (Name × Addr))) (j : Nat) : Nat × Stmt × Env :=
  (j, (ss[j]?).getD .brk, bundleEnv ss frames)

/-- A proposed move relation is sound when each allowed pair commutes in both directions in every linked scope.
The hypothesis is semantic; it does not assume how a syntax check or footprint certificate established it. -/
def MovesSound (W : World) (ss : List Stmt) (allows : Nat → Nat → Bool) : Prop :=
  ∀ frames (s s' : State W), declareTops ((List.range ss.length).zip ss) s = .ok frames s' →
    ∀ n i j, i ≠ j → allows i j = true →
      Commute (unitStep n (bundleUnit ss frames i) : M W Flow) (unitStep n (bundleUnit ss frames j)) ∧
      Commute (unitStep n (bundleUnit ss frames j) : M W Flow) (unitStep n (bundleUnit ss frames i))

/-- Independently proved move relations can be combined without changing the reordering argument. -/
theorem MovesSound.or {ss : List Stmt} {a b : Nat → Nat → Bool}
    (ha : MovesSound W ss a) (hb : MovesSound W ss b) : MovesSound W ss (fun i j => a i j || b i j) := by
  intro frames s s' hframes n i j hij h
  simp only [Bool.or_eq_true] at h
  rcases h with h | h
  · exact ha frames s s' hframes n i j hij h
  · exact hb frames s s' hframes n i j hij h

/-- Syntax-based commutation needs only the names and regions of linked frames. -/
theorem mayMove_commutes {ss : List Stmt} {B : EsmSplit.Bundle}
    (hfp : FootprintsOfSyntax ss B) (hud : EsmSplit.UniqueDefs B)
    (frames : List (Nat × List (Name × Addr)))
    (hF : FramesOf ((List.range ss.length).zip ss) frames)
    (hreg : ∀ q ∈ frames, ∀ p ∈ q.2, p.2.region = q.1 + 1) (n i j : Nat)
    (hij : i ≠ j) (hc : MayMove ss i j = true) :
    Commute (unitStep n (bundleUnit ss frames i) : M W Flow) (unitStep n (bundleUnit ss frames j)) ∧
    Commute (unitStep n (bundleUnit ss frames j) : M W Flow) (unitStep n (bundleUnit ss frames i)) := by
  unfold MayMove at hc
  cases ha : ss[i]? with
  | none => simp [ha] at hc
  | some a =>
    cases hb : ss[j]? with
    | none => simp [ha, hb] at hc
    | some b =>
      simp only [ha, hb] at hc
      have hab := mayMove_commute (W := W) hfp hF hreg hud hij ha hb hc n
      have hba := mayMove_commute (W := W) hfp hF hreg hud (Ne.symm hij) hb ha (by rw [mayMove_symm]; exact hc) n
      simpa only [unitStep, bundleUnit, ha, hb, Option.getD_some] using And.intro hab hba

/-- The existing syntax-based relation supplies the semantic move contract. -/
theorem mayMove_sound {ss : List Stmt} {B : EsmSplit.Bundle}
    (hfp : FootprintsOfSyntax ss B) (hud : EsmSplit.UniqueDefs B) : MovesSound W ss (MayMove ss) := by
  intro frames s s' hframes
  exact mayMove_commutes hfp hud frames (declareTops_names _ hframes) (declareTops_regions _ hframes)

/-- Reordering needs semantic commutation, independently of the procedure that certifies allowed pairs.
As with `Commute`, the conclusion concerns successful executions; it does not identify abrupt outcomes. -/
theorem reorder_units_of_commute (n : Nat) (units : Nat → Nat × Stmt × Env) (allows : Nat → Nat → Bool)
    (hcomm : ∀ i j, i ≠ j → allows i j = true →
      Commute (unitStep n (units i) : M W Flow) (unitStep n (units j)) ∧
      Commute (unitStep n (units j) : M W Flow) (unitStep n (units i)))
    {count : Nat} {ord : List Nat} (hperm : ord.Perm (List.range count))
    (hmove : ord.Pairwise fun a b => a < b ∨ allows a b = true) (s s' : State W) :
    runUnits n (ord.map units) s = .ok () s' ↔
      runUnits n ((List.range count).map units) s = .ok () s' := by
  have key : ∀ l : List Nat, runUnits n (l.map units) s = .ok () s' ↔
      l.foldl (fun X j => Img (unitStep n (units j)) X) (· = s) s' := by
    intro l
    rw [← List.foldl_map (g := fun X u => Img (unitStep n u) X), ← runUnits_img]
    exact ⟨fun h => ⟨s, rfl, h⟩, fun ⟨_, rfl, h⟩ => h⟩
  rw [key, key]
  have hnd := hperm.nodup_iff.2 List.nodup_range
  refine iff_of_eq (congrFun (EsmSplit.foldl_reorder (fun j X => Img (unitStep n (units j)) X)
    (fun a b => !allows a b) (fun i j hc X => ?_) (· = s) count ord hnd
    (fun x => hperm.mem_iff.trans List.mem_range) (hmove.imp fun h => by simpa using h)) s')
  simp only [Bool.not_eq_false'] at hc
  by_cases hij : i = j
  · subst hij; rfl
  obtain ⟨hab, hba⟩ := hcomm i j hij hc
  exact img_comm hab hba X

theorem reorder_units {ss : List Stmt} {B : EsmSplit.Bundle} {frames : List (Nat × List (Name × Addr))}
    (hfp : FootprintsOfSyntax ss B) (hF : FramesOf ((List.range ss.length).zip ss) frames)
    (hreg : ∀ q ∈ frames, ∀ p ∈ q.2, p.2.region = q.1 + 1) (hud : EsmSplit.UniqueDefs B) (n : Nat)
    {ord : List Nat} (hperm : ord.Perm (List.range ss.length))
    (hmove : ord.Pairwise fun a b => a < b ∨ MayMove ss a b = true) (s s' : State W) :
    runUnits n (ord.map (bundleUnit ss frames)) s = .ok () s' ↔
      runUnits n ((List.range ss.length).map (bundleUnit ss frames)) s = .ok () s' :=
  reorder_units_of_commute n (bundleUnit ss frames) (MayMove ss)
    (mayMove_commutes hfp hud frames hF hreg n) hperm hmove s s'

/-! ## The theorem -/

theorem bind_ok_congr {α β : Type} {m : M W α} {f g : α → M W β} {s0 : State W}
    (h : ∀ a t, m s0 = .ok a t → ∀ b s', f a t = .ok b s' ↔ g a t = .ok b s') (b : β) (s' : State W) :
    (m >>= f) s0 = .ok b s' ↔ (m >>= g) s0 = .ok b s' := by
  rw [M.bind_apply, M.bind_apply]
  cases hm : m s0 with
  | ok a t => exact h a t hm b s'
  | _ => exact Iff.rfl

theorem map_bundleUnit_keys {ss : List Stmt} (frames : List (Nat × List (Name × Addr))) :
    ∀ l : List Nat, (∀ i ∈ l, i < ss.length) →
      (l.map (bundleUnit ss frames)).map (fun u => (u.1, u.2.1)) = l.filterMap (numbered ss)
  | [], _ => rfl
  | i :: l, h => by
    have hi := h i (by simp)
    rw [List.map_cons, List.map_cons, List.filterMap_cons, map_bundleUnit_keys frames l fun j hj => h j (by simp [hj])]
    simp [numbered, bundleUnit, List.getElem?_eq_getElem hi]

theorem Agree.refl (N : List Name) (env : Env) : Agree N env env := ⟨rfl, fun _ _ => rfl⟩

/-- A successful-run equivalence between the orders in the linked bundle scope also holds for the split program. -/
theorem moves_preserve_run_of_order {W : World} {ss : List Stmt} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} (hvalid : EsmSplit.Valid B S P) (hfp : FootprintsOfSyntax ss B) (n : Nat)
    (horder : ∀ frames (t t1 t2 : State W),
      declareTops ((List.range ss.length).zip ss) t = .ok frames t1 →
      initFunctions ((List.range ss.length).map (bundleUnit ss frames)) t1 = .ok () t2 →
      ∀ s', runUnits n ((List.range ss.length).map (bundleUnit ss frames)) t2 = .ok () s' ↔
        runUnits n (P.execOrder.map (bundleUnit ss frames)) t2 = .ok () s')
    (s : State W) :
    runProgram W n (Program.single ss) = .ok () s ↔ runProgram W n (splitProgram ss B P) = .ok () s := by
  have hperm := valid_perm hvalid hfp.1
  obtain ⟨hregions, htops⟩ := split_holds_statements (B := B) hperm
  have hlt : ∀ i ∈ P.execOrder, i < ss.length := fun i hi => List.mem_range.1 (hperm.mem_iff.1 hi)
  unfold runProgram
  rw [← hregions]
  generalize initialState W (Program.single ss).regions = s0
  rw [link_bind, link_bind, ← htops]
  refine bind_ok_congr (fun _ t _ => bind_ok_congr (fun frames t1 hframes => ?_)) () s
  have hF : FramesOf ((List.range ss.length).zip ss) frames := single_tops ss ▸ declareTops_names _ hframes
  obtain ⟨L', hL', hkeys, hagree⟩ := split_units_agree hvalid hfp hF
  have hL : (Program.single ss).scopes frames (Program.single ss).loaded =
      .ok [(bundleModule ss, bundleEnv ss frames)] := by rw [single_loaded]; rfl
  rw [hL, hL']
  intro b s'
  change (initFunctions ((unitsOf [(bundleModule ss, bundleEnv ss frames)]).mergeSort fun a b => decide (a.1 ≤ b.1))
      >>= fun _ => runUnits n (unitsOf [(bundleModule ss, bundleEnv ss frames)])) t1 = .ok b s' ↔
    (initFunctions ((unitsOf L').mergeSort fun a b => decide (a.1 ≤ b.1)) >>= fun _ => runUnits n (unitsOf L')) t1 =
      .ok b s'
  have hkeys0 : (unitsOf [(bundleModule ss, bundleEnv ss frames)]).map (fun u => (u.1, u.2.1)) =
      (List.range ss.length).zip ss := by
    rw [unitsOf_keys]; simp [bundleModule]
  have hsorted0 : (unitsOf [(bundleModule ss, bundleEnv ss frames)]).Pairwise fun a b => a.1 ≤ b.1 := by
    have h := zip_range_sorted ss
    rw [← hkeys0] at h
    exact (List.pairwise_map (R := fun a b => a.1 ≤ b.1) (f := fun u : Nat × Stmt × Env => (u.1, u.2.1))).1 h
  have henv0 : ∀ u ∈ unitsOf [(bundleModule ss, bundleEnv ss frames)], u.2.2 = bundleEnv ss frames := by
    intro u hu
    obtain ⟨p, hp, _, he⟩ := mem_unitsOf hu
    rw [List.mem_singleton] at hp
    subst hp
    exact he
  -- the functions are made in the order of the identities, the same in both
  have hinit : SameUnits (unitsOf [(bundleModule ss, bundleEnv ss frames)])
      ((unitsOf L').mergeSort fun a b => decide (a.1 ≤ b.1)) := by
    refine sameUnits_of ?_ fun u hu u' hu' _ h2 => ?_
    · rw [hkeys0, List.map_mergeSort (f := fun u : Nat × Stmt × Env => (u.1, u.2.1))
        (s := fun a b : Nat × Stmt => decide (a.1 ≤ b.1)) (fun _ _ _ _ => rfl), hkeys,
        mergeSort_numbered ss hperm]
    · rw [henv0 u hu, h2]
      exact hagree u' ((List.mergeSort_perm _ _).mem_iff.1 hu')
  -- the bundle's units, and the split's, as units of the bundle
  have hbundle : SameUnits (unitsOf [(bundleModule ss, bundleEnv ss frames)])
      ((List.range ss.length).map (bundleUnit ss frames)) := by
    refine sameUnits_of ?_ fun u hu u' hu' _ h2 => ?_
    · rw [hkeys0, map_bundleUnit_keys frames _ fun i hi => List.mem_range.1 hi, filterMap_numbered_range]
    · obtain ⟨i, _, rfl⟩ := List.mem_map.1 hu'
      rw [henv0 u hu]
      exact Agree.refl _ _
  have hsplit : SameUnits (P.execOrder.map (bundleUnit ss frames)) (unitsOf L') := by
    refine sameUnits_of ?_ fun u hu u' hu' _ h2 => ?_
    · rw [map_bundleUnit_keys frames _ hlt, hkeys]
    · obtain ⟨i, _, rfl⟩ := List.mem_map.1 hu
      rw [h2]
      exact hagree u' hu'
  rw [sorted_units hsorted0, ← initFunctions_same hinit, initFunctions_same hbundle]
  refine bind_ok_congr (fun a t2 hfunctions b s' => ?_) b s'
  rw [runUnits_same n hbundle, ← runUnits_same n hsplit]
  cases a
  cases b
  exact horder frames t t1 t2 (single_tops ss ▸ hframes) hfunctions s'

/-- **A split that moves only units that commute ends well as its bundle does.** For a plan that `Valid` accepts,
in which every pair of statements that runs out of the bundle's order is certified by a sound move relation, the split ends well in a
state exactly when the bundle ends well in that state, for every bound on the depth and every world. -/
theorem moves_preserve_run_of_sound {W : World} {ss : List Stmt} {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (hvalid : EsmSplit.Valid B S P) (hfp : FootprintsOfSyntax ss B)
    {allows : Nat → Nat → Bool} (hsound : MovesSound W ss allows)
    (hmove : P.execOrder.Pairwise fun a b => a < b ∨ allows a b = true) (n : Nat) (s : State W) :
    runProgram W n (Program.single ss) = .ok () s ↔ runProgram W n (splitProgram ss B P) = .ok () s :=
  moves_preserve_run_of_order hvalid hfp n (fun frames t t1 t2 hframes _ s' =>
    (reorder_units_of_commute n (bundleUnit ss frames) allows (hsound frames t t1 hframes n)
      (valid_perm hvalid hfp.1) hmove t2 s').symm) s

/-- The syntax-based move checker instantiates the semantic program-equivalence theorem. -/
theorem moves_preserve_run {W : World} {ss : List Stmt} {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (hvalid : EsmSplit.Valid B S P) (hfp : FootprintsOfSyntax ss B)
    (hmove : P.execOrder.Pairwise fun a b => a < b ∨ MayMove ss a b = true) (n : Nat) (s : State W) :
    runProgram W n (Program.single ss) = .ok () s ↔ runProgram W n (splitProgram ss B P) = .ok () s :=
  moves_preserve_run_of_sound hvalid hfp (mayMove_sound hfp hvalid.1) hmove n s

/-- Any sound move relation can justify whole-program equivalence for successful executions. -/
theorem moves_preserve_of_sound {W : World} {ss : List Stmt} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} (hvalid : EsmSplit.Valid B S P) (hfp : FootprintsOfSyntax ss B)
    {allows : Nat → Nat → Bool} (hsound : MovesSound W ss allows)
    (hmove : P.execOrder.Pairwise fun a b => a < b ∨ allows a b = true) (s : State W) :
    Ends W (Program.single ss) (.ok () s) ↔ Ends W (splitProgram ss B P) (.ok () s) :=
  ⟨fun ⟨n, h, hr⟩ => ⟨n, (moves_preserve_run_of_sound hvalid hfp hsound hmove n s).1 h, hr⟩,
    fun ⟨n, h, hr⟩ => ⟨n, (moves_preserve_run_of_sound hvalid hfp hsound hmove n s).2 h, hr⟩⟩

/-- **A split that moves only units that commute**: the bundle ends well in a state exactly when the split does. -/
theorem moves_preserve {W : World} {ss : List Stmt} {B : EsmSplit.Bundle} {S : EsmSplit.Spec} {P : EsmSplit.Plan}
    (hvalid : EsmSplit.Valid B S P) (hfp : FootprintsOfSyntax ss B)
    (hmove : P.execOrder.Pairwise fun a b => a < b ∨ MayMove ss a b = true) (s : State W) :
    Ends W (Program.single ss) (.ok () s) ↔ Ends W (splitProgram ss B P) (.ok () s) :=
  ⟨fun ⟨n, h, hr⟩ => ⟨n, (moves_preserve_run hvalid hfp hmove n s).1 h, hr⟩,
    fun ⟨n, h, hr⟩ => ⟨n, (moves_preserve_run hvalid hfp hmove n s).2 h, hr⟩⟩

#print axioms moves_preserve

/-- `moves_preserve` with its last hypothesis decided by `movesOk`, as `lowersplit` decides it. -/
theorem moves_preserve_of_movesOk {W : World} {ss : List Stmt} {B : EsmSplit.Bundle} {S : EsmSplit.Spec}
    {P : EsmSplit.Plan} (hvalid : EsmSplit.Valid B S P) (hfp : FootprintsOfSyntax ss B)
    (hmove : movesOk ss.toArray P.execOrder = true) (s : State W) :
    Ends W (Program.single ss) (.ok () s) ↔ Ends W (splitProgram ss B P) (.ok () s) :=
  moves_preserve hvalid hfp ((movesOk_iff ss _).1 hmove) s

#print axioms moves_preserve_of_movesOk

end CoreJS
