import EsmSplit.CoreJS.ClassOrigin

namespace CoreJS

variable {W : World}

/-- The factory has exactly one operation in its body: return a newly created function. -/
def wrapperFactory (name : Name) (params : List Name) (returned : Func) : Func :=
  .mk name .arrow false params [.ret (.func returned)]

/-- The linked binding still denotes the factory closure at the start of this swap. -/
structure WrapperAt (s : State W) (env : Env) (alias : Name) (cell factory : Addr)
    (closed : Env) (name : Name) (params : List Name) (returned : Func) : Prop where
  lookup : env.vars.lookup alias = some cell
  binding : ∃ mutable, s.cellAt cell = some ⟨some (.obj factory), mutable⟩
  object : ∃ o, s.objAt factory = some o ∧
    o.code = some (.closure (wrapperFactory name params returned) closed [])

theorem WrapperAt.in_region {s : State W} (h : WrapperAt s env alias cell factory closed name params returned)
    (r : Nat) : WrapperAt { s with region := r } env alias cell factory closed name params returned :=
  ⟨h.lookup, h.binding, h.object⟩

theorem WrapperAt.after_local {s t : State W} (h : WrapperAt s env alias cell factory closed name params returned)
    {m : M W α} (hl : Local (i + 1) R m) (hc : cell.region ≠ i + 1) (ho : factory.region ≠ i + 1)
    (run : inRegion i m s = .ok a t) : WrapperAt t env alias cell factory closed name params returned := by
  obtain ⟨u, hu, rfl⟩ := inRegion_ok_state run
  obtain ⟨H, C, rfl, _⟩ := hl _ _ _ _ (Sees.refl rfl) hu
  obtain ⟨mutable, hb⟩ := h.binding
  obtain ⟨o, hob, hcode⟩ := h.object
  refine ⟨h.lookup, ⟨mutable, ?_⟩, ⟨o, ?_, hcode⟩⟩
  · change (s.put (i + 1) H C).cellAt cell = _
    simpa [State.cellAt_put, hc] using hb
  · change (s.put (i + 1) H C).objAt factory = _
    simpa [State.objAt_put, ho] using hob

/-- An import alias that resolves to the exported cell denotes the same factory. -/
theorem WrapperAt.alias {s : State W} {consumer : Env} {alias : Name} (h : WrapperAt s env x cell factory closed name params returned)
    (hl : consumer.vars.lookup alias = some cell) :
    WrapperAt s consumer alias cell factory closed name params returned := ⟨hl, h.binding, h.object⟩

/-- Evaluating the provider's declaration installs this factory and its captured environment. -/
theorem wrapperVar_origin {fuel id : Nat} {s t : State W} {flow : Flow}
    (run : runUnit fuel env id (.decl .var x (.func (wrapperFactory name params returned))) s = .ok flow t) :
    ∃ cell factory, factory.region = id + 1 ∧
      WrapperAt t env x cell factory (env.capture (wrapperFactory name params returned).closes) name params returned := by
  obtain ⟨u, hexec, rfl⟩ := runUnit_ok run
  cases fuel with
  | zero => cases hexec
  | succ n =>
    unfold execStmt at hexec
    obtain ⟨v, s1, hmake, hw⟩ := bind_ok hexec
    obtain ⟨_, t1, hwrite, hf⟩ := bind_ok hw
    cases hf
    cases n with
    | zero => cases hmake
    | succ n =>
      unfold evalExpr makeFunc at hmake
      obtain ⟨_, _, hscope, hmake⟩ := bind_ok hmake
      cases hscope
      obtain ⟨a, t2, ha, hmake⟩ := bind_ok hmake
      obtain ⟨_, _, hproto, hmake⟩ := bind_ok hmake
      cases hproto
      obtain ⟨_, _, hself, hv⟩ := bind_ok hmake
      cases hself
      cases hv
      obtain ⟨cell, hl, hb, hh⟩ := writeVar_origin hwrite
      have ho := alloc_objAt ha
      exact ⟨cell, a, alloc_region ha, hl, ⟨true, hb⟩, ⟨_, by simpa [State.objAt, hh] using ho, rfl⟩⟩

private def resolvedCall (n : Nat) (closed : Env) (name : Name) (params : List Name) (returned : Func)
    (args : List Val) : M W Val := do
  let env ← enterFunction (wrapperFactory name params returned) closed .undef args
  match ← execStmts n env [.ret (.func returned)] with
  | .ret v => pure v
  | _ => pure .undef

private theorem resolvedCall_local (n : Nat) (closed : Env) (name : Name) (params : List Name)
    (returned : Func) (args : List Val) :
    Local r R (resolvedCall n closed name params returned args : M W Val) := by
  unfold resolvedCall
  apply Local.bind'
  · unfold enterFunction
    refine (local_declare _ _).bind' fun env => ?_
    unfold enterBlock
    refine (local_declare _ _).bind' fun env => ?_
    exact (local_pure ()).bind' fun _ => local_pure env
  · intro env
    apply Local.bind'
    · cases n with
      | zero => exact local_outOfFuel
      | succ n =>
        unfold execStmts
        apply Local.bind'
        · cases n with
          | zero => exact local_outOfFuel
          | succ n =>
            change Local r R (evalExpr n env (.func returned) >>= fun v => pure (Flow.ret v) : M W Flow)
            apply Local.bind'
            · cases n with
              | zero => exact local_outOfFuel
              | succ n => exact local_makeFunc env returned
            · intro v; exact local_pure _
        · intro flow
          cases flow <;> try exact local_pure _
          cases n with
          | zero => exact local_outOfFuel
          | succ n => exact local_pure _
    · intro flow; cases flow <;> exact local_pure _

private def withWrapper (fuel : Nat) (env closed : Env) (name : Name) (params : List Name)
    (returned callback : Func) : M W Val :=
  match fuel with
  | 0 | 1 => outOfFuel
  | n + 2 => evalArgs (n + 1) env [.func callback] >>= resolvedCall n closed name params returned

private theorem withWrapper_local (fuel : Nat) (env closed : Env) (name : Name) (params : List Name)
    (returned callback : Func) : Local r R (withWrapper fuel env closed name params returned callback : M W Val) := by
  cases fuel with
  | zero => exact local_outOfFuel
  | succ fuel =>
    cases fuel with
    | zero => exact local_outOfFuel
    | succ n =>
      apply Local.bind'
      · exact (allocOnly_local r R (n + 1)).2.1 env [.func callback] rfl (by intro z hz; cases hz)
      · exact resolvedCall_local n closed name params returned

private theorem eval_withWrapper (h : WrapperAt s env alias cell factory closed name params returned)
    (fuel : Nat) (callback : Func) :
    evalExpr fuel env (.call (.var alias) [.func callback]) s =
      withWrapper fuel env closed name params returned callback s := by
  cases fuel with
  | zero => rfl
  | succ fuel =>
    cases fuel with
    | zero => rfl
    | succ n =>
      obtain ⟨mutable, hb⟩ := h.binding
      obtain ⟨o, ho, hcode⟩ := h.object
      have hr : evalExpr (n + 1) env (.var alias) s = .ok (.obj factory) s := by
        simp only [evalExpr, readVar, h.lookup, M.bind_apply, getState, hb]; rfl
      change ((evalExpr (n + 1) env (.var alias)) >>= fun v =>
        evalArgs (n + 1) env [.func callback] >>= callFn (n + 1) v .undef) s = _
      rw [bind_of_ok hr]
      unfold withWrapper
      simp only [M.bind_apply]
      cases ha : evalArgs (n + 1) env [.func callback] s with
      | ok args t =>
        have he := (allocOnly_keeps (n + 1)).2.1 env [.func callback] s s rfl (Extends.refl s)
        rw [ha] at he
        have hg : getObj factory t = .ok o t := by simp [getObj, he.objs _ _ ho]
        simp only [callFn, M.bind_apply, hg, hcode]
        rfl
      | _ => rfl

private def wrapperDecl (fuel : Nat) (env closed : Env) (name : Name) (params : List Name)
    (returned callback : Func) (x : Name) : M W Flow :=
  match fuel with
  | 0 => outOfFuel
  | n + 1 => do
    writeVar env x (← withWrapper n env closed name params returned callback)
    pure .normal

private theorem wrapperDecl_eq (h : WrapperAt s env alias cell factory closed name params returned)
    (fuel : Nat) (callback : Func) (x : Name) :
    runUnit fuel env i (.decl .var x (.call (.var alias) [.func callback])) s =
      inRegion i (wrapperDecl fuel env closed name params returned callback x) s := by
  cases fuel with
  | zero => rfl
  | succ n =>
    have he := eval_withWrapper (h.in_region (i + 1)) n callback
    simp only [runUnit, inRegion, execStmt, wrapperDecl, M.bind_apply, modifyState, he]

private theorem wrapperDecl_local (fuel : Nat) (closed : Env) (name : Name) (params : List Name)
    (returned callback : Func) (hx : ∀ c, env.vars.lookup x = some c → c.region = r) :
    Local r R (wrapperDecl fuel env closed name params returned callback x : M W Flow) := by
  cases fuel with
  | zero => exact local_outOfFuel
  | succ n =>
    exact (withWrapper_local n env closed name params returned callback).bind' fun v =>
      (local_writeVar hx v).bind' fun _ => local_pure _

/-- Calling the linked wrapper creator with a function argument commutes on successful runs with a
separate local unit. The callback and returned function bodies are preserved in the heap. -/
theorem wrapper_var_commutes_at {s : State W} {m : M W α}
    (h : WrapperAt s env alias cell factory closed name params returned)
    (hx : ∀ c, env.vars.lookup x = some c → c.region = i + 1)
    (hij : i ≠ j) (hl : Local (j + 1) R m) (hr : ∀ c, R c → c.region ≠ i + 1)
    (hc : cell.region ≠ j + 1) (ho : factory.region ≠ j + 1) (n : Nat) (callback : Func) :
    let a := runUnit n env i (.decl .var x (.call (.var alias) [.func callback]))
    let b := inRegion j m
    ∀ f v t, (∃ u, a s = .ok f u ∧ b u = .ok v t) ↔
             (∃ u, b s = .ok v u ∧ a u = .ok f t) := by
  intro a b f v t
  have hs := wrapperDecl_local (W := W) (R := fun _ => False) n closed name params returned callback hx
  have hnone : ∀ c : Addr, False → c.region ≠ j + 1 := fun _ h => h.elim
  constructor
  · rintro ⟨u, ha, hb⟩
    rw [show a s = _ from wrapperDecl_eq h n callback x] at ha
    obtain ⟨u', hb', ha'⟩ := local_inRegion_commute hij hs hl hnone hr _ _ _ _ _ ha hb
    exact ⟨u', hb', (wrapperDecl_eq (h.after_local hl hc ho hb') n callback x).trans ha'⟩
  · rintro ⟨u, hb, ha⟩
    rw [show a u = _ from wrapperDecl_eq (h.after_local hl hc ho hb) n callback x] at ha
    obtain ⟨u', ha', hb'⟩ := local_inRegion_commute hij.symm hl hs hr hnone _ _ _ _ _ hb ha
    exact ⟨u', (wrapperDecl_eq h n callback x).trans ha', hb'⟩

end CoreJS
