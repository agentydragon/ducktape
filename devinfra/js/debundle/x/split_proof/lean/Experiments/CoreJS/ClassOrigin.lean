import Experiments.CoreJS.Reachability

/-! # Heritage established by a class declaration

An anonymous plain class with literal instance members gives its binding a constructor
with an own data property `prototype`. The constructor and the binding are in the
declaration's region. Instance members install properties on the prototype, leaving
the constructor's property intact.
-/

namespace CoreJS

variable {W : World}

/-- Class creation only installs instance methods or accessors with literal keys. -/
def literalInstanceMembers : List ClassMember → Bool
  | [] => true
  | .method false (.str _) _ :: rest | .getter false (.str _) _ :: rest
  | .setter false (.str _) _ :: rest => literalInstanceMembers rest
  | _ :: _ => false

theorem literalInstanceMembers_plain {ms : List ClassMember} (h : literalInstanceMembers ms = true) :
    plainClassMembers ms = true := by
  induction ms with
  | nil => rfl
  | cons m ms ih =>
    cases m <;> rename_i static k f <;> cases static <;> cases k <;>
      simp only [literalInstanceMembers, Bool.false_eq_true] at h
    all_goals exact ih h

theorem alloc_objAt {s t : State W} {o : Obj} {a : Addr} (h : alloc o s = .ok a t) :
    t.objAt a = some o := by
  unfold alloc at h
  split at h
  · rename_i objs hobjs
    cases h
    rw [objAt_push hobjs] <;> simp
  · cases h

theorem modifyObj_self {s t : State W} {f : Obj → Obj} {a : Addr} {o : Obj}
    (ho : s.objAt a = some o) (h : modifyObj a f s = .ok () t) : t.objAt a = some (f o) := by
  cases h
  unfold State.objAt at ho ⊢
  simp only [Array.getElem?_modify, ↓reduceIte]
  cases hh : s.heap[a.region]? with
  | none => simp [hh] at ho
  | some objs =>
    have hi : objs[a.index]? = some o := by simpa [hh] using ho
    simp [Array.getElem?_modify, hi]

private theorem defineOwn_preserves_other {s t : State W} {a c : Addr} {key : String} {desc : Desc}
    (hne : c ≠ a) (h : defineOwn a key desc s = .ok () t) : t.objAt c = s.objAt c := by
  unfold defineOwn at h
  obtain ⟨o, s1, ho, hrest⟩ := bind_ok h
  have hs1 := (inert_getObj a).ok ho
  subst s1
  split at hrest
  · cases hrest
  · obtain ⟨p, s2, hp, hdefine⟩ := bind_ok hrest
    have hs2 := (inert_lift _).ok hp
    subst s2
    cases p with
    | none => cases hdefine
    | some prop =>
      cases hdefine
      exact objAt_modify _ _ _ _ hne

private theorem evalMembers_instance_preserves_ctor (n : Nat) (env : Env) (proto ctor : Addr)
    (hne : ctor ≠ proto) (ms : List ClassMember) (hm : literalInstanceMembers ms = true)
    {s t : State W} {keys : List (Bool × String)} {o : Obj} (ho : s.objAt ctor = some o)
    (h : evalMembers n env proto ctor ms s = .ok keys t) : t.objAt ctor = some o := by
  induction n generalizing ms s t keys with
  | zero => cases h
  | succ n ih =>
    cases ms with
    | nil => cases h; exact ho
    | cons m ms =>
      cases m <;> rename_i static k f <;> cases static <;> cases k <;>
        simp only [literalInstanceMembers, Bool.false_eq_true] at hm
      all_goals
        unfold evalMembers at h
        obtain ⟨key, s1, hk, h1⟩ := bind_ok h
        have hs1 := (evalExpr_str_inert n env _).ok hk
        subst s1
        obtain ⟨name, s2, hn, h2⟩ := bind_ok h1
        have hs2 := (inert_lift _).ok hn
        subst s2
        obtain ⟨value, s3, hf, h3⟩ := bind_ok h2
        have hfunc := makeFunc_keeps env f (Extends.refl s)
        rw [hf] at hfunc
        obtain ⟨_, s4, hd, h4⟩ := bind_ok h3
        have hother := defineOwn_preserves_other hne hd
        obtain ⟨field, s5, hnone, h5⟩ := bind_ok h4
        cases hnone
        obtain ⟨rest, s6, hrest, hresult⟩ := bind_ok h5
        cases hresult
        exact ih ms hm (hother.trans (hfunc.objs ctor o ho)) hrest

private theorem evalExpr_plainClass_origin {n : Nat} {env : Env} {name : Name}
    {ctor : Func} {ms : List ClassMember} (hm : literalInstanceMembers ms = true)
    {s t : State W} {v : Val} (h : evalExpr n env (.cls name false ctor ms) s = .ok v t) :
    ∃ a p o, v = .obj a ∧ a.region = s.region ∧ t.objAt a = some o ∧
      o.code = some (.closure ctor (env.capture (ctor.free ++ freeMembers ms)) []) ∧
      o.props.lookup "prototype" = some (.data (.obj p) false false false) := by
  cases n with
  | zero => cases h
  | succ n =>
    unfold evalExpr at h
    obtain ⟨env', s0, hscope, h⟩ := bind_ok h
    cases hscope
    obtain ⟨proto, s1, hp, h⟩ := bind_ok h
    obtain ⟨c, s2, hc, h⟩ := bind_ok h
    have hcObj := alloc_objAt hc
    have hne : c ≠ proto := by
      intro he
      have hfree := (alloc_ok hc).1
      rw [he, alloc_objAt hp] at hfree
      cases hfree
    obtain ⟨_, s3, hback, hmembers⟩ := bind_ok h
    have hsame : s3.objAt c = s2.objAt c := by
      cases hback
      exact objAt_modify _ _ _ _ hne
    obtain ⟨keys, s4, hmRun, hfinish⟩ := bind_ok hmembers
    have hcObj' := evalMembers_instance_preserves_ctor n env proto c hne ms hm (hsame.trans hcObj) hmRun
    rw [plainClassMembers_fieldInits (literalInstanceMembers_plain hm)] at hfinish
    have hz : keys.zip ([] : List Expr) = [] := by cases keys <;> rfl
    rw [hz] at hfinish
    simp only [List.filterMap_nil] at hfinish
    obtain ⟨_, s5, hu, hinit⟩ := bind_ok hfinish
    have hfound := modifyObj_self hcObj' hu
    obtain ⟨_, s6, hi, hfields⟩ := bind_ok hinit
    cases hi
    obtain ⟨_, s7, he, hresult⟩ := bind_ok hfields
    have hs : s7 = s5 := by cases n <;> cases he <;> rfl
    subst s7
    cases hresult
    exact ⟨c, proto, _, rfl, (alloc_region hc).trans (alloc_ok hp).2.region, hfound, rfl, rfl⟩

theorem writeVar_origin {env : Env} {x : Name} {v : Val} {s t : State W}
    (h : writeVar env x v s = .ok () t) :
    ∃ c, env.vars.lookup x = some c ∧ t.cellAt c = some ⟨some v, true⟩ ∧ t.heap = s.heap := by
  unfold writeVar at h
  cases hx : env.vars.lookup x with
  | none => rw [hx] at h; cases h
  | some c =>
    rw [hx] at h
    change (match s.cellAt c with
      | some { value := some _, mutable := true } => setCell c v
      | some { value := some _, mutable := false } => throwErr .type "assignment to constant variable"
      | _ => throwErr .reference s!"cannot access '{x}' before initialization" : M W Unit) s = .ok () t at h
    cases hb : s.cellAt c with
    | none => rw [hb] at h; cases h
    | some b =>
      rcases b with ⟨_ | _, _ | _⟩ <;> rw [hb] at h <;> cases h
      refine ⟨c, rfl, ?_, rfl⟩
      change (s.assign c v).cellAt c = _
      simp [State.cellAt_assign_self, hb]

/-- A successful plain class declaration establishes the binding and constructor needed for a heritage read. -/
theorem plainClassVar_origin {n id : Nat} {env : Env} {x name : Name} {ctor : Func} {ms : List ClassMember}
    (hm : literalInstanceMembers ms = true) (hhome : ∀ c, env.vars.lookup x = some c → c.region = id + 1)
    {s t : State W} {flow : Flow} (h : runUnit n env id (.decl .var x (.cls name false ctor ms)) s = .ok flow t) :
    ∃ cell a p o, env.vars.lookup x = some cell ∧ cell.region = id + 1 ∧
      t.cellAt cell = some ⟨some (.obj a), true⟩ ∧ a.region = id + 1 ∧ t.objAt a = some o ∧
      o.code = some (.closure ctor (env.capture (ctor.free ++ freeMembers ms)) []) ∧
      o.props.lookup "prototype" = some (.data (.obj p) false false false) := by
  obtain ⟨u, hexec, rfl⟩ := runUnit_ok h
  cases n with
  | zero => cases hexec
  | succ n =>
    unfold execStmt at hexec
    obtain ⟨v, s1, hc, hwrite⟩ := bind_ok hexec
    obtain ⟨_, s2, hw, hresult⟩ := bind_ok hwrite
    cases hresult
    obtain ⟨a, p, o, rfl, hr, ho, hcode, hp⟩ := evalExpr_plainClass_origin hm hc
    obtain ⟨cell, hl, hcell, hheap⟩ := writeVar_origin hw
    exact ⟨cell, a, p, o, hl, hhome _ hl, hcell, hr, by simpa [State.objAt, hheap] using ho, hcode, hp⟩

end CoreJS
