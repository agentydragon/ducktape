import EsmSplit.CoreJS.ClassOrigin

namespace CoreJS

variable {W : World}

/-- The factory has exactly one operation in its body: return a newly created function. -/
def wrapperFactory (name : Name) (params : List Name) (returned : Func) : Func :=
  .mk name .arrow false params [.ret (.func returned)]

/-- The linked binding still denotes the factory closure at the start of this swap. -/
structure WrapperAt (s : State W) (env : Env) (alias : Name) (cell factory : Addr) (closed : Env)
    (name : Name) (params : List Name) (returned : Func) : Prop where
  lookup : env.vars.lookup alias = some cell
  binding : ∃ mutable, s.cellAt cell = some ⟨some (.obj factory), mutable⟩
  object :
    ∃ o,
      s.objAt factory = some o ∧
        o.code = some (.closure (wrapperFactory name params returned) closed [])

theorem WrapperAt.in_region {s : State W}
    (h : WrapperAt s env alias cell factory closed name params returned) (r : Nat) :
    WrapperAt { s with region := r } env alias cell factory closed name params returned :=
  ⟨h.lookup, h.binding, h.object⟩

theorem WrapperAt.after_local {s t : State W}
    (h : WrapperAt s env alias cell factory closed name params returned) {m : M W α}
    (hl : Local (i + 1) R m) (hc : cell.region ≠ i + 1) (ho : factory.region ≠ i + 1)
    (run : inRegion i m s = .ok a t) :
    WrapperAt t env alias cell factory closed name params returned := by
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
theorem WrapperAt.alias {s : State W} {consumer : Env} {alias : Name}
    (h : WrapperAt s env x cell factory closed name params returned)
    (hl : consumer.vars.lookup alias = some cell) :
    WrapperAt s consumer alias cell factory closed name params returned :=
  ⟨hl, h.binding, h.object⟩

/-- Evaluating the provider's declaration installs this factory and its captured environment. -/
theorem wrapperVar_origin {fuel id : Nat} {s t : State W} {flow : Flow}
    (run :
      runUnit fuel env id (.decl .var x (.func (wrapperFactory name params returned))) s =
        .ok flow t) :
    ∃ cell factory,
      factory.region = id + 1 ∧
        WrapperAt t env x cell factory (env.capture (wrapperFactory name params returned).closes)
          name params returned := by
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
      exact
        ⟨cell, a, alloc_region ha, hl, ⟨true, hb⟩, ⟨_, by simpa [State.objAt, hh] using ho, rfl⟩⟩

private def resolvedCall (n : Nat) (closed : Env) (name : Name) (params : List Name)
    (returned : Func) (args : List Val) : M W Val := do
  let env ← enterFunction (wrapperFactory name params returned) closed .undef args
  match ← execStmts n env [.ret (.func returned)] with
  | .ret v =>
    pure v
  | _ =>
    pure .undef

private theorem resolvedCall_local (n : Nat) (closed : Env) (name : Name) (params : List Name)
    (returned : Func) (args : List Val) :
    Local r R (resolvedCall n closed name params returned args : M W Val) := by
  unfold resolvedCall
  apply Local.bind'
  · simp only [enterFunction, strictArgumentsBindings, parameterBindings, wrapperFactory, Func.kind,
      M.pure_bind]
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
          | succ
            n =>
            change
              Local r R (evalExpr n env (.func returned) >>= fun v => pure (Flow.ret v) : M W Flow)
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
    (returned callback : Func) :
    Local r R (withWrapper fuel env closed name params returned callback : M W Val) := by
  cases fuel with
  | zero => exact local_outOfFuel
  | succ fuel =>
    cases fuel with
    | zero => exact local_outOfFuel
    | succ n =>
      apply Local.bind'
      · exact
          (allocOnly_local r R (n + 1)).2.1 env [.func callback] rfl
            (by
              intro z hz; cases hz)
      · exact resolvedCall_local n closed name params returned

private theorem eval_withWrapper
    (h : WrapperAt s env alias cell factory closed name params returned) (fuel : Nat)
    (callback : Func) :
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
      change
        ((evalExpr (n + 1) env (.var alias)) >>= fun v =>
              evalArgs (n + 1) env [.func callback] >>= callFn (n + 1) v .undef)
            s =
          _
      rw [bind_of_ok hr]
      unfold withWrapper
      simp only [M.bind_apply]
      cases ha : evalArgs (n + 1) env [.func callback] s with
      | ok args
        t =>
        have he := (allocOnly_keeps (n + 1)).2.1 env [.func callback] s s rfl (Extends.refl s)
        rw [ha] at he
        have hg : getObj factory t = .ok o t := by simp [getObj, he.objs _ _ ho]
        simp only [callFn, dispatchCall, dispatchCallWithDeferred, runLocalCall, M.bind_apply, hg,
          hcode]
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
    exact
      (withWrapper_local n env closed name params returned callback).bind' fun v =>
        (local_writeVar hx v).bind' fun _ => local_pure _

/-- Calling the linked wrapper creator with a function argument commutes on successful runs with a
separate local unit. The callback and returned function bodies are preserved in the heap. -/
theorem wrapper_var_commutes_at {s : State W} {m : M W α}
    (h : WrapperAt s env alias cell factory closed name params returned)
    (hx : ∀ c, env.vars.lookup x = some c → c.region = i + 1) (hij : i ≠ j) (hl : Local (j + 1) R m)
    (hr : ∀ c, R c → c.region ≠ i + 1) (hc : cell.region ≠ j + 1) (ho : factory.region ≠ j + 1)
    (n : Nat) (callback : Func) :
    let a := runUnit n env i (.decl .var x (.call (.var alias) [.func callback]))
    let b := inRegion j m
    ∀ f v t, (∃ u, a s = .ok f u ∧ b u = .ok v t) ↔ (∃ u, b s = .ok v u ∧ a u = .ok f t) := by
  intro a b f v t
  have hs :=
    wrapperDecl_local (W := W) (R := fun _ => False) n closed name params returned callback hx
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

/-- An initialized local var cell and an available heap region. These facts
come from linking and the common prefix, not from any host integrity premise. -/
structure ClosureWriteReady (s : State W) (id : Nat) (env : Env) (name : Name) : Prop where
  heap : ∃ h, s.heap[id + 1]? = some h
  cell : ∃ c v, env.vars.lookup name = some c ∧ c.region = id + 1 ∧ s.cellAt c = some ⟨some v, true⟩

private theorem make_arrow_ok {s : State W} (env : Env) (f : Func)
    (kind : f.kind = .arrow) (noSelf : f.self = false)
    (heap : ∃ h, s.heap[s.region]? = some h) :
    ∃ a t, makeFunc env f f.name s = .ok (.obj a) t ∧
      t.heap[t.region]?.isSome = true ∧ t.cells = s.cells ∧ t.region = s.region := by
  obtain ⟨h, hh⟩ := heap
  let obj : Obj := {
    proto := some f.intrinsicProto, code := some (.closure f (env.capture f.closes) []),
    props := [("length", .data (.num (Float.ofNat f.length)) false false true),
      ("name", .data (.str f.name) false false true)] }
  refine ⟨⟨s.region, h.size⟩, { s with heap := s.heap.set! s.region (h.push obj) }, ?_, ?_, rfl, rfl⟩
  · have notNormal : (FuncKind.arrow == FuncKind.normal) = false := rfl
    have p : (pure env : M W Env) s = .ok env s := rfl
    simp only [makeFunc, selfScope, initSelf, noSelf, Bool.false_eq_true, ↓reduceIte,
      kind, notNormal, FuncKind.hasOwnPrototype, M.pure_bind, alloc, M.bind_apply, p, hh]
    rfl
  · change (s.heap.set! s.region (h.push _))[s.region]?.isSome = true
    have within := (Array.getElem?_eq_some_iff.mp hh).1
    simp only [set!_getElem?, within, ↓reduceIte, Option.isSome_some]

private theorem new_cells_ok {s : State W} (bindings : List (Name × Binding))
    (cells : ∃ b, s.cells[s.region]? = some b) :
    ∃ frame t, newCells bindings s = .ok frame t ∧ t.heap = s.heap ∧ t.region = s.region := by
  induction bindings generalizing s with
  | nil => exact ⟨[], s, rfl, rfl, rfl⟩
  | cons b bs ih =>
    obtain ⟨frame, hf⟩ := cells
    let t := { s with cells := s.cells.set! s.region (frame.push b.2) }
    have step : newCell b.2 s = .ok ⟨s.region, frame.size⟩ t := by simp [newCell, hf, t]
    have within := (Array.getElem?_eq_some_iff.mp hf).1
    have next : t.cells[t.region]? = some (frame.push b.2) := by
      simp only [t, set!_getElem?, within, ↓reduceIte]
    obtain ⟨rest, u, allocated, heap, region⟩ := ih ⟨_, next⟩
    refine ⟨(b.1, ⟨s.region, frame.size⟩) :: rest, u, ?_, heap, region⟩
    rcases b with ⟨x, v⟩
    unfold newCells
    rw [bind_of_ok step, bind_of_ok allocated]
    rfl

private theorem enter_wrapper_ok {s : State W} (closed : Env) (name : Name)
    (params : List Name) (returned : Func) (args : List Val)
    (cells : ∃ b, s.cells[s.region]? = some b) :
    ∃ env t, enterFunction (wrapperFactory name params returned) closed .undef args .undef s = .ok env t ∧
      t.heap = s.heap ∧ t.region = s.region ∧ Extends s t := by
  obtain ⟨frame, t, run, heap, region⟩ := new_cells_ok (fixedParameterBindings params args) cells
  have keep := newCells_keeps (fixedParameterBindings params args) (Extends.refl s)
  rw [run] at keep
  refine ⟨{ closed with vars := frame ++ closed.vars }, t, ?_, heap, region, keep⟩
  have vars : varNamesOf [.ret (.func returned)] = [] := rfl
  have nt : newTargetBindings (.mk name .arrow false params [.ret (.func returned)]) .undef = [] := rfl
  have arrowNe : (FuncKind.arrow != FuncKind.arrow) = false := rfl
  have arrow : (FuncKind.arrow == FuncKind.arrow) = true := rfl
  simp only [enterFunction, strictArgumentsBindings, parameterBindings, nt, wrapperFactory,
    Func.kind, Func.params, Func.body, arrow, arrowNe, Bool.false_and, Bool.false_eq_true, ↓reduceIte, M.pure_bind, vars,
    List.eraseDups_nil, List.filter_nil, List.map_nil, List.append_nil]
  unfold declare
  rw [M.bind_assoc, bind_of_ok run]
  simp only [M.pure_bind]
  rfl

private theorem var_write_ok {s : State W} (env : Env) (x : Name) (c : Addr) (old v : Val)
    (lookup : env.vars.lookup x = some c) (bound : s.cellAt c = some ⟨some old, true⟩) :
    ∃ t, (writeVar env x v >>= fun _ => pure Flow.normal) s = .ok .normal t := by
  refine ⟨s.assign c v, ?_⟩
  simp only [writeVar, lookup, M.bind_apply, getState, bound, setCell]
  rfl

private theorem arrow_var_ok {s : State W} (env : Env) (id n : Nat) (x : Name) (f : Func)
    (kind : f.kind = .arrow) (noSelf : f.self = false) (r : ClosureWriteReady s id env x) :
    ∃ t, runUnit (n + 2) env id (.decl .var x (.func f)) s = .ok .normal t := by
  obtain ⟨c, v, lookup, _, bound⟩ := r.cell
  obtain ⟨a, t, made, _, cells, _⟩ := make_arrow_ok (s := { s with region := id + 1 }) env f kind noSelf r.heap
  obtain ⟨u, write⟩ := var_write_ok env x c v (.obj a) lookup
    (s := t) (by simpa only [State.cellAt, cells] using bound)
  apply Exists.intro { u with region := 0 }
  apply runUnit_of
  simp only [execStmt, evalExpr]
  rw [bind_of_ok made]
  exact write

private theorem withWrapper_run {s : State W} (env closed : Env) (name : Name)
    (params : List Name) (returned callback : Func)
    (cbKind : callback.kind = .arrow) (cbSelf : callback.self = false)
    (retKind : returned.kind = .arrow) (retSelf : returned.self = false)
    (heap : ∃ h, s.heap[s.region]? = some h)
    (cells : ∃ b, s.cells[s.region]? = some b) (fuel : Nat) :
    (fuel < 5 ∧ withWrapper fuel env closed name params returned callback s = .fuel) ∨
      (5 ≤ fuel ∧ ∃ a t,
        withWrapper fuel env closed name params returned callback s = .ok (.obj a) t ∧ Extends s t) := by
  match fuel with
  | 0 | 1 | 2 => exact Or.inl ⟨by omega, rfl⟩
  | n + 3 =>
    obtain ⟨a, u, made, hheap, hcell, hreg⟩ := make_arrow_ok env callback cbKind cbSelf heap
    have available : ∃ b, u.cells[u.region]? = some b := by simpa only [hcell, hreg] using cells
    obtain ⟨activation, t, entered, th, tr, kept⟩ :=
      enter_wrapper_ok closed name params returned [.obj a] available
    have args : evalArgs (n + 2) env [.func callback] s = .ok [.obj a] u := by
      simp only [evalArgs, Expr.argument, evalExpr, Bool.false_eq_true, ↓reduceIte, M.pure_bind]
      rw [bind_of_ok made]
      rfl
    have run : withWrapper (n + 3) env closed name params returned callback s =
        (execStmts (n + 1) activation [.ret (.func returned)] >>= fun flow =>
          match flow with | .ret v => pure v | _ => pure .undef) t := by
      change ((evalArgs (n + 2) env [.func callback]) >>= resolvedCall (n + 1) closed name params returned) s = _
      rw [bind_of_ok args]
      unfold resolvedCall
      rw [bind_of_ok entered]
    match n with
    | 0 | 1 => exact Or.inl ⟨by omega, run.trans rfl⟩
    | m + 2 =>
      have availableHeap : ∃ h, t.heap[t.region]? = some h := by
        rw [th, tr]
        cases eq : u.heap[u.region]? with
        | none => simp [eq] at hheap
        | some h => exact ⟨h, rfl⟩
      obtain ⟨b, v, loader, _, _, _⟩ := make_arrow_ok activation returned retKind retSelf availableHeap
      refine Or.inr ⟨by omega, b, v, ?_, ?_⟩
      · rw [run]
        simp only [execStmts, execStmt, evalExpr, continueBody, M.bind_assoc]
        change ((makeFunc activation returned >>= fun value => pure (Flow.ret value)) >>= _) t = _
        rw [M.bind_assoc, bind_of_ok loader]
        rfl
      · have hc := makeFunc_keeps env callback (Extends.refl s) callback.name
        rw [made] at hc
        have hr := makeFunc_keeps activation returned (hc.trans kept) returned.name
        rwa [loader] at hr

private theorem ClosureWriteReady.after_local {s t : State W} {m : M W α}
    (ready : ClosureWriteReady s i env x) (distinct : i ≠ j)
    (localStep : Local (j + 1) R m) (run : inRegion j m s = .ok a t) :
    ClosureWriteReady t i env x := by
  obtain ⟨u, hu, rfl⟩ := inRegion_ok_state run
  obtain ⟨H, C, rfl, _⟩ := localStep _ _ _ _ (Sees.refl rfl) hu
  obtain ⟨h, heap⟩ := ready.heap
  obtain ⟨cell, old, lookup, home, bound⟩ := ready.cell
  refine ⟨⟨h, ?_⟩, ⟨cell, old, lookup, home, ?_⟩⟩
  · change (s.heap.set! (j + 1) H)[i + 1]? = some h
    rw [set!_getElem?]
    simp [Ne.symm distinct, heap]
  · change (s.put (j + 1) H C).cellAt cell = _
    simpa [State.cellAt_put, home, distinct] using bound

private theorem wrapper_var_run {s : State W}
    (factoryAt : WrapperAt s env alias cell factory closed name params returned)
    (ready : ClosureWriteReady s i env x) (n : Nat) (callback : Func)
    (cbKind : callback.kind = .arrow) (cbSelf : callback.self = false)
    (retKind : returned.kind = .arrow) (retSelf : returned.self = false) :
    let a := runUnit n env i (.decl .var x (.call (.var alias) [.func callback]))
    (n < 6 ∧ a s = .fuel) ∨ (6 ≤ n ∧ ∃ t, a s = .ok .normal t) := by
  dsimp only
  rw [wrapperDecl_eq factoryAt n callback x]
  cases n with
  | zero => exact Or.inl ⟨by omega, rfl⟩
  | succ n =>
    obtain ⟨dst, old, lookup, home, bound⟩ := ready.cell
    have available : ∃ b, s.cells[i + 1]? = some b := by
      unfold State.cellAt at bound
      rw [home] at bound
      obtain ⟨b, hb, _⟩ := Option.bind_eq_some_iff.mp bound
      exact ⟨b, hb⟩
    let start := { s with region := i + 1 }
    rcases withWrapper_run (s := start) env closed name params returned callback
      cbKind cbSelf retKind retSelf ready.heap available n with ⟨low, fuel⟩ | ⟨enough, a, t, run, kept⟩
    · refine Or.inl ⟨by omega, ?_⟩
      have localRun : wrapperDecl (n + 1) env closed name params returned callback x start = .fuel := by
        change (withWrapper n env closed name params returned callback >>= _) start = _
        rw [M.bind_apply, fuel]
      unfold inRegion
      change (wrapperDecl (n + 1) env closed name params returned callback x >>= _) start = _
      rw [M.bind_apply, localRun]
    · obtain ⟨u, write⟩ := var_write_ok (s := t) env x dst old (.obj a) lookup (kept.cells _ _ bound)
      refine Or.inr ⟨by omega, { u with region := 0 }, inRegion_of ?_⟩
      change (withWrapper n env closed name params returned callback >>= _) start = _
      rw [bind_of_ok run]
      exact write

/-- Two genuine allocating arrow initializers can cross from a checked common
state. The wrapper's factory and both declaration cells have to be linked in
that state. The continuation receives the same identities and complete state
at equal fuel; exhausted fuel on either route is preserved. -/
theorem wrapper_arrow_all_outcomes {s : State W} {α : Type}
    (factoryAt : WrapperAt s env alias cell factory closed name params returned)
    (xReady : ClosureWriteReady s i env x) (yReady : ClosureWriteReady s j other y)
    (hij : i ≠ j) (hc : cell.region ≠ j + 1) (hf : factory.region ≠ j + 1)
    (callback moved : Func)
    (cbKind : callback.kind = .arrow) (cbSelf : callback.self = false)
    (retKind : returned.kind = .arrow) (retSelf : returned.self = false)
    (movedKind : moved.kind = .arrow) (movedSelf : moved.self = false)
    (fuel : Nat) (continuation : M W α) :
    let a := runUnit fuel env i (.decl .var x (.call (.var alias) [.func callback]))
    let b := runUnit fuel other j (.decl .var y (.func moved))
    (a >>= fun _ => b >>= fun _ => continuation) s =
      (b >>= fun _ => a >>= fun _ => continuation) s := by
  intro a b
  have hx : ∀ c, env.vars.lookup x = some c → c.region = i + 1 := by
    obtain ⟨c, v, lookup, home, _⟩ := xReady.cell
    intro d hd
    rw [lookup] at hd
    cases hd
    exact home
  have hy : ∀ c, other.vars.lookup y = some c → c.region = j + 1 := by
    obtain ⟨c, v, lookup, home, _⟩ := yReady.cell
    intro d hd
    rw [lookup] at hd
    cases hd
    exact home
  let noneRead : Addr → Prop := fun _ => False
  have bLocal := decl_local (W := W) (r := j + 1) (R := noneRead) (e := .func moved) rfl fuel
    other .var y (by intro z hz; cases hz) hy
  rcases wrapper_var_run factoryAt xReady fuel callback cbKind cbSelf retKind retSelf with
    ⟨low, afuel⟩ | ⟨enough, u, au⟩
  · have af : a s = .fuel := afuel
    simp only [M.bind_apply, af]
    by_cases small : fuel < 2
    · have early : b s = .fuel := by
        have only : fuel = 0 ∨ fuel = 1 := by omega
        rcases only with h | h <;> simp only [b, h] <;> rfl
      rw [early]
    · obtain ⟨n, hn⟩ : ∃ n, fuel = n + 2 := ⟨fuel - 2, by omega⟩
      have hw := arrow_var_ok other j n y moved movedKind movedSelf yReady
      obtain ⟨t, bt⟩ : ∃ t, b s = .ok .normal t := by simpa only [b, hn] using hw
      have atFuel := wrapper_var_run (factoryAt.after_local bLocal hc hf bt)
        (xReady.after_local hij bLocal bt) fuel callback cbKind cbSelf retKind retSelf
      rcases atFuel with ⟨_, nextFuel⟩ | ⟨tooHigh, _⟩
      · change a t = .fuel at nextFuel
        simp only [bt, nextFuel]
      · omega
  · have au' : a s = .ok .normal u := au
    have aLocal := wrapperDecl_local (W := W) (R := noneRead) fuel closed name params
      returned callback hx
    have aru : inRegion i (wrapperDecl fuel env closed name params returned callback x) s = .ok .normal u :=
      (wrapperDecl_eq factoryAt fuel callback x).symm.trans au
    have yAfter := yReady.after_local hij.symm aLocal aru
    obtain ⟨n, hn⟩ : ∃ n, fuel = n + 2 := ⟨fuel - 2, by omega⟩
    obtain ⟨t, bt⟩ := hn ▸ arrow_var_ok other j n y moved movedKind movedSelf yAfter
    have commute := wrapper_var_commutes_at (j := j) factoryAt hx hij bLocal
      (by intro z hz; cases hz) hc hf fuel callback
    obtain ⟨v, bv, av⟩ := (commute .normal .normal t).1 ⟨u, au, bt⟩
    change b u = .ok .normal t at bt
    change b s = .ok .normal v at bv
    change a v = .ok .normal t at av
    rw [bind_of_ok au', bind_of_ok bt, bind_of_ok bv, bind_of_ok av]

end CoreJS
