import EsmSplit.CoreJS.IteratorFrame
import EsmSplit.CoreJS.ObjectFrame

/-! # Statement reachability frames

A statement composes the frames of the smaller expressions and statements
it actually evaluates. A catch binds the escaped thrown value, and finally
runs with the same envelope after either a jump or a thrown value.
-/

namespace CoreJS

variable {W : World}

def FlowSafe (A : HeapNode → Prop) (flow : Flow) : Prop :=
  ∀ v, flow = .ret v → SafeVal A v

theorem FlowSafe.normal (A : HeapNode → Prop) : FlowSafe A .normal :=
  fun _ h => by cases h

theorem FlowSafe.ret {A : HeapNode → Prop} {v : Val} (hv : SafeVal A v) : FlowSafe A (.ret v) :=
  fun _ h => by cases h; exact hv

theorem execStmts_reachFrame_step {r R cells A n}
    (step : ∀ env st, (∀ node ∈ Env.nodes env, A node) →
      ReachFrame r R cells A (FlowSafe A) (execStmt n env st : M W Flow))
    (tail : ∀ env ss, (∀ node ∈ Env.nodes env, A node) →
      ReachFrame r R cells A (FlowSafe A) (execStmts n env ss : M W Flow))
    (env : Env) (h : ∀ node ∈ env.nodes, A node) (ss : List Stmt) :
    ReachFrame r R cells A (FlowSafe A) (execStmts (n + 1) env ss : M W Flow) := by
  cases ss with
  | nil => exact reachFrame_pure r R cells A Flow.normal (FlowSafe.normal A)
  | cons head body =>
    simp only [execStmts]
    apply (step env head h).bind
    intro flow safe
    cases flow
    case normal => exact tail env body h
    all_goals exact reachFrame_pure r R cells A _ safe

private theorem reachFrame_truth {r R cells A} (v : Val) :
    ReachFrame r R cells A (fun _ => True) (lift (toBoolean v) : M W Bool) :=
  reachFrame_lift (toBoolean v) (fun _ _ => trivial)

theorem loop_reachFrame_step {r R cells A n} (away : Away r R A)
    (outside : ∀ a, a.region ≠ r → A (.cell a))
    (eval : ∀ e env, (∀ node ∈ Env.nodes env, A node) →
      ReachFrame r R cells A (SafeVal A) (evalExpr n env e : M W Val))
    (step : ∀ env st, (∀ node ∈ Env.nodes env, A node) →
      ReachFrame r R cells A (FlowSafe A) (execStmt n env st : M W Flow))
    (tail : ∀ env fresh test update body, (∀ node ∈ Env.nodes env, A node) →
      ReachFrame r R cells A (FlowSafe A) (loop n env fresh test update body : M W Flow))
    (env : Env) (safe : ∀ node ∈ env.nodes, A node) (fresh : List Name) (test update : Expr) (body : Stmt) :
    ReachFrame r R cells A (FlowSafe A) (loop (n + 1) env fresh test update body : M W Flow) := by
  simp only [loop]
  apply (eval test env safe).bind
  intro v _
  apply (reachFrame_truth v).bind
  intro testVal _
  cases testVal
  · exact reachFrame_pure r R cells A Flow.normal (FlowSafe.normal A)
  · apply (step env body safe).bind
    intro flow hflow
    have repeatLoop := (reachFrame_copyBindings away outside env fresh safe).bind
      (fun next hn => (eval update next hn).bind fun _ _ => tail next fresh test update body hn)
    cases flow
    case brk => exact reachFrame_pure r R cells A Flow.normal (FlowSafe.normal A)
    case ret value => exact reachFrame_pure r R cells A (Flow.ret value) hflow
    all_goals exact repeatLoop

private def IteratorRemainderSafe (A : HeapNode → Prop) : Option IteratorCursor → Prop
  | none => True
  | some cursor => cursor.Safe A

private theorem bindArrayElements_reachFrame {r R cells A} (away : Away r R A) (call : Call W)
    (framed : CallFramed r R cells A call) (objectRoot : A (.obj objectProto))
    (arrayIteratorRoot : A (.obj arrayIteratorProto)) (bindField : BindingPattern → Val → M W Unit)
    (frame : ∀ p v, SafeVal A v → ReachFrame r R cells A (fun _ => True) (bindField p v))
    (elements : List (Option BindingPattern)) (cursor : Option IteratorCursor) (safe : IteratorRemainderSafe A cursor) :
    ReachFrame r R cells A (IteratorRemainderSafe A) (bindArrayElements call bindField elements cursor) := by
  induction elements generalizing cursor with
  | nil => exact reachFrame_pure r R cells A cursor safe
  | cons pattern rest ih =>
    simp only [bindArrayElements]
    have step : ReachFrame r R cells A (IteratorStepSafe A)
        (cursor.elim (pure none) (fun c => iteratorStep call c pattern.isSome)) := by
      cases cursor
      · exact reachFrame_pure r R cells A none trivial
      · exact iteratorStep_reachFrame away call framed objectRoot _ _ safe
    refine step.bind fun result hStep => ?_
    have hv : SafeVal A ((result.map Prod.fst).getD .undef) := by
      cases result
      · simp [SafeVal, Val.nodes]
      · exact hStep.1
    have hn : IteratorRemainderSafe A (result.map Prod.snd) := by
      cases result
      · trivial
      · exact hStep.2
    have binding : ReachFrame r R cells A (fun _ => True)
        (pattern.elim (pure ()) (fun p => bindField p ((result.map Prod.fst).getD .undef))) := by
      cases pattern with
      | none => exact reachFrame_pure r R cells A () trivial
      | some target => exact frame target _ hv
    refine binding.attempt.bind fun bound hBound => ?_
    cases bound with
    | ok => exact ih _ hn
    | error e =>
      generalize checked : result.map Prod.snd = next at hn ⊢
      cases next with
      | none => exact reachFrame_errorValue e hBound
      | some current =>
        exact iteratorClose_reachFrame away call framed objectRoot arrayIteratorRoot current hn (.error e) hBound

theorem bindArrayPattern_reachFrame {r R cells A} (away : Away r R A) (call : Call W)
    (framed : CallFramed r R cells A call) (objectRoot : A (.obj objectProto))
    (arrayIteratorRoot : A (.obj arrayIteratorProto)) (arrayRoot : A (.obj arrayProto))
    (allocate : ∀ o : Obj, (∀ node ∈ o.nodes, A node) →
      ReachFrame r R cells A (fun a => A (.obj a)) (alloc o : M W Addr))
    (fuel : Nat) (bindField : BindingPattern → Val → M W Unit)
    (frame : ∀ p v, SafeVal A v → ReachFrame r R cells A (fun _ => True) (bindField p v))
    (elements : List (Option BindingPattern)) (rest : Option BindingPattern) (v : Val) (hv : SafeVal A v) :
    ReachFrame r R cells A (fun _ => True) (bindArrayPattern call fuel bindField elements rest v) := by
  unfold bindArrayPattern
  refine (getIterator_reachFrame away call framed objectRoot arrayIteratorRoot v hv).bind fun cursor hc => ?_
  refine (bindArrayElements_reachFrame away call framed objectRoot arrayIteratorRoot bindField frame
    elements (some cursor) hc).bind fun next hn => ?_
  cases rest with
  | none =>
    cases next with
    | none => exact reachFrame_pure r R cells A () trivial
    | some current =>
      exact iteratorClose_reachFrame away call framed objectRoot arrayIteratorRoot current hn (.ok ()) trivial
  | some target =>
    have collected : ReachFrame r R cells A (fun vs => ∀ v ∈ vs, SafeVal A v)
        (next.elim (pure []) (collectIterator call fuel)) := by
      cases next with
      | none => exact reachFrame_pure r R cells A [] (by simp)
      | some current => exact collectIterator_reachFrame away call framed objectRoot fuel current hn
    exact collected.bind fun values hv =>
      (allocate _ (literalArray_nodes arrayRoot values hv)).bind fun a ha => frame target (.obj a) (safeVal_obj.2 ha)

private theorem loopIterator_reachFrame {r R cells A} (away : Away r R A) (call : Call W)
    (framed : CallFramed r R cells A call) (objectRoot : A (.obj objectProto))
    (arrayIteratorRoot : A (.obj arrayIteratorProto)) (iteration : Val → M W Flow)
    (frame : ∀ v, SafeVal A v → ReachFrame r R cells A (FlowSafe A) (iteration v))
    (fuel : Nat) (cursor : IteratorCursor) (safe : cursor.Safe A) :
    ReachFrame r R cells A (FlowSafe A) (loopIterator call iteration fuel cursor) := by
  induction fuel generalizing cursor with
  | zero => exact reachFrame_fuel
  | succ fuel ih =>
    simp only [loopIterator]
    refine (iteratorStep_reachFrame away call framed objectRoot cursor true safe).bind fun result hStep => ?_
    cases result with
    | none => exact reachFrame_pure r R cells A Flow.normal (FlowSafe.normal A)
    | some pair =>
      obtain ⟨value, next⟩ := pair
      refine (frame value hStep.1).attempt.bind fun result h => ?_
      have close := iteratorClose_reachFrame (P := FlowSafe A) away call framed objectRoot arrayIteratorRoot next hStep.2
      cases result with
      | error e => exact close (.error e) h
      | ok flow =>
        cases flow with
        | normal | cont => exact ih next hStep.2
        | brk => exact close (.ok .normal) (FlowSafe.normal A)
        | ret v => exact close (.ok (.ret v)) h

theorem forOf_reachFrame_step {r R cells A n} (away : Away r R A)
    (framed : CallFramed r R cells A (callFn n : Call W)) (objectRoot : A (.obj objectProto))
    (arrayIteratorRoot : A (.obj arrayIteratorProto))
    (eval : ∀ e env, (∀ node ∈ Env.nodes env, A node) →
      ReachFrame r R cells A (SafeVal A) (evalExpr n env e : M W Val))
    (binding : ∀ env kind p v, (∀ node ∈ Env.nodes env, A node) → SafeVal A v →
      ReachFrame r R cells A (fun _ => True) (bindPattern n env kind p v : M W Unit))
    (step : ∀ env st, (∀ node ∈ Env.nodes env, A node) →
      ReachFrame r R cells A (FlowSafe A) (execStmt n env st : M W Flow))
    (enter : ∀ env ss, (∀ node ∈ Env.nodes env, A node) →
      ReachFrame r R cells A (fun env' => ∀ node ∈ env'.nodes, A node) (enterBlock env ss : M W Env))
    (env : Env) (safe : ∀ node ∈ env.nodes, A node) (kind : DeclKind)
    (pattern : BindingPattern) (source : Expr) (body : Stmt) :
    ReachFrame r R cells A (FlowSafe A) (execStmt (n + 1) env (.forOf kind pattern source body) : M W Flow) := by
  simp only [execStmt]
  let head := [Stmt.bind kind pattern .undef]
  refine (enter env head safe).bind fun sourceEnv hs => ?_
  refine (eval source sourceEnv hs).bind fun v hv => ?_
  refine (getIterator_reachFrame away (callFn n) framed objectRoot arrayIteratorRoot v hv).bind fun cursor hc => ?_
  apply loopIterator_reachFrame away (callFn n) framed objectRoot arrayIteratorRoot _ _ _ _ hc
  intro value hv
  exact (enter env head safe).bind fun next hn =>
    (binding next kind pattern value hn hv).bind fun _ _ => step next body hn

theorem bindPattern_reachFrame_step {r R cells A n} (away : Away r R A)
    (env : Env) (safe : ∀ node ∈ env.nodes, A node)
    (eval : ∀ e, ReachFrame r R cells A (SafeVal A) (evalExpr n env e : M W Val))
    (get : ∀ base key, SafeVal A base → ReachFrame r R cells A (SafeVal A) (getProp (callFn n) base key : M W Val))
    (step : ∀ kind p v, SafeVal A v →
      ReachFrame r R cells A (fun _ => True) (bindPattern n env kind p v : M W Unit))
    (array : ∀ kind items rest v, SafeVal A v →
      ReachFrame r R cells A (fun _ => True)
        (bindArrayPattern (callFn n) n (bindPattern n env kind) items rest v : M W Unit))
    (kind : DeclKind) (p : BindingPattern) (v : Val) (hv : SafeVal A v) :
    ReachFrame r R cells A (fun _ => True) (bindPattern (n + 1) env kind p v : M W Unit) := by
  cases p with
  | name x =>
    cases kind
    · exact reachFrame_writeVar away env x v safe hv
    all_goals exact reachFrame_initVar away env x v safe hv
  | defaultValue target fallback =>
    cases v <;> simp only [bindPattern]
    case undef => exact (eval fallback).bind fun value h => step kind target value h
    all_goals exact step kind target _ hv
  | array elements rest => exact array kind elements rest v hv
  | object props =>
    have fields : ∀ props : List (Expr × BindingPattern), ReachFrame r R cells A (fun _ => True)
        (props.forM fun (key, target) => do
          let key ← evalExpr n env key
          let value ← getProp (callFn n) v key
          bindPattern n env kind target value : M W Unit) := by
      intro props
      induction props with
      | nil => exact reachFrame_pure r R cells A () trivial
      | cons item rest ih =>
        obtain ⟨key, target⟩ := item
        exact ((eval key).bind fun k _ => (get v k hv).bind fun value h =>
          step kind target value h).bind fun _ _ => ih
    simp only [bindPattern]
    split <;> first
    | exact fields props
    | exact reachFrame_errorValue (.error .type _) (by simp [SafeVal, Val.nodes])

theorem execStmt_reachFrame_step {r R cells A n} (away : Away r R A)
    (outside : ∀ a, a.region ≠ r → A (.cell a))
    (eval : ∀ e env, (∀ node ∈ Env.nodes env, A node) →
      ReachFrame r R cells A (SafeVal A) (evalExpr n env e : M W Val))
    (step : ∀ env st, (∀ node ∈ Env.nodes env, A node) →
      ReachFrame r R cells A (FlowSafe A) (execStmt n env st : M W Flow))
    (binding : ∀ env kind p v, (∀ node ∈ Env.nodes env, A node) → SafeVal A v →
      ReachFrame r R cells A (fun _ => True) (bindPattern n env kind p v : M W Unit))
    (many : ∀ env ss, (∀ node ∈ Env.nodes env, A node) →
      ReachFrame r R cells A (FlowSafe A) (execStmts n env ss : M W Flow))
    (repeatLoop : ∀ env fresh test update body, (∀ node ∈ Env.nodes env, A node) →
      ReachFrame r R cells A (FlowSafe A) (loop n env fresh test update body : M W Flow))
    (enter : ∀ env ss, (∀ node ∈ Env.nodes env, A node) →
      ReachFrame r R cells A (fun env' => ∀ node ∈ env'.nodes, A node) (enterBlock env ss : M W Env))
    (forOfFrame : ∀ env kind pattern source body, (∀ node ∈ Env.nodes env, A node) →
      ReachFrame r R cells A (FlowSafe A) (execStmt (n + 1) env (.forOf kind pattern source body) : M W Flow))
    (opaqueFrame : ∀ env code decls funs free action, (∀ node ∈ Env.nodes env, A node) →
      ReachFrame r R cells A (FlowSafe A) (execStmt (n + 1) env (.opaque code decls funs free action) : M W Flow))
    (env : Env) (h : ∀ node ∈ env.nodes, A node) (st : Stmt) :
    ReachFrame r R cells A (FlowSafe A) (execStmt (n + 1) env st : M W Flow) := by
  have normal : ReachFrame r R cells A (FlowSafe A) (pure Flow.normal : M W Flow) :=
    reachFrame_pure r R cells A Flow.normal (FlowSafe.normal A)
  cases st with
  | «opaque» code decls funs free action =>
    exact opaqueFrame env code decls funs free action h
  | expr e =>
    exact (eval e env h).bind fun _ _ => normal
  | decl kind name initial =>
    simp only [execStmt]
    cases kind <;>
      apply (eval initial env h).bind <;> intro v hv
    · exact (reachFrame_writeVar away env name v h hv).bind fun _ _ => normal
    all_goals exact (reachFrame_initVar away env name v h hv).bind fun _ _ => normal
  | bind kind p initial =>
    exact (eval initial env h).bind fun v hv => (binding env kind p v h hv).bind fun _ _ => normal
  | varDecl _ | funDecl _ => exact normal
  | ret e =>
    exact (eval e env h).bind fun v hv => reachFrame_pure r R cells A (Flow.ret v) (FlowSafe.ret hv)
  | ite c t f =>
    simp only [execStmt]
    apply (eval c env h).bind
    intro v _
    apply (reachFrame_truth v).bind
    intro test _
    cases test
    · exact step env f h
    · exact step env t h
  | block ss =>
    exact (enter env ss h).bind fun next hn => many next ss hn
  | loop init test update body =>
    simp only [execStmt]
    apply (enter env init h).bind
    intro next hn
    apply (many next init hn).bind
    intro _ _
    exact (reachFrame_copyBindings away outside next _ hn).bind fun iteration hi =>
      repeatLoop iteration _ test update body hi
  | breakScope body =>
    simp only [execStmt]
    apply (step env body h).bind
    intro flow safe
    cases flow <;> exact reachFrame_pure r R cells A _ (by first | exact safe | intro _ eq; cases eq)

  | forOf kind pattern source body => exact forOfFrame env kind pattern source body h
  | brk | cont =>
    exact reachFrame_pure r R cells A _ (fun _ eq => by cases eq)
  | «throw» e =>
    exact (eval e env h).bind fun v hv => reachFrame_errorValue v hv
  | tryCatch body param handler =>
    let next : Except Val Flow → M W Flow
      | .ok flow => pure flow
      | .error v => do
        let e ← match param with
          | some x => declare env [(x, { value := some v, mutable := true })]
          | none => pure env
        execStmt n e handler
    have run : ReachFrame r R cells A (FlowSafe A) (attempt (execStmt n env body) >>= next) := by
      apply (step env body h).attempt.bind
      intro result safe
      cases result with
      | ok flow => exact reachFrame_pure r R cells A flow safe
      | error v =>
        simp only [next]
        cases param with
        | none => exact step env handler h
        | some x =>
          have args : ∀ p ∈ [(x, { value := some v, mutable := true : Binding })],
              ∀ node ∈ p.2.nodes, A node := by
            intro p hp node hn
            rcases List.mem_singleton.mp hp with rfl
            exact safe node (by simpa [Binding.nodes] using hn)
          exact (reachFrame_declare away outside env _ h args).bind fun e he => step e handler he
    refine Eq.mpr (congrArg (ReachFrame r R cells A (FlowSafe A)) ?_) run
    funext s
    simp only [execStmt, attempt, M.bind_apply]
    cases execStmt n env body s <;> rfl
  | tryFinally body finalizer =>
    let next (result : Except Val Flow) : M W Flow := do
      match ← execStmt n env finalizer with
      | .normal =>
        match result with
        | .ok flow => pure flow
        | .error v => throwVal v
      | jump => pure jump
    have run : ReachFrame r R cells A (FlowSafe A) (attempt (execStmt n env body) >>= next) := by
      apply (step env body h).attempt.bind
      intro result safe
      simp only [next]
      apply (step env finalizer h).bind
      intro flow ended
      cases flow
      case normal =>
        cases result with
        | ok flow => exact reachFrame_pure r R cells A flow safe
        | error v => exact reachFrame_errorValue v safe
      all_goals exact reachFrame_pure r R cells A _ ended
    refine Eq.mpr (congrArg (ReachFrame r R cells A (FlowSafe A)) ?_) run
    funext s
    simp only [execStmt, attempt, M.bind_apply, next]
    cases execStmt n env body s <;> rfl

end CoreJS
