import EsmSplit.CoreJS.BindingFrame

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

theorem execStmt_reachFrame_step {r R cells A n} (away : Away r R A)
    (outside : ∀ a, a.region ≠ r → A (.cell a))
    (eval : ∀ e env, (∀ node ∈ Env.nodes env, A node) →
      ReachFrame r R cells A (SafeVal A) (evalExpr n env e : M W Val))
    (step : ∀ env st, (∀ node ∈ Env.nodes env, A node) →
      ReachFrame r R cells A (FlowSafe A) (execStmt n env st : M W Flow))
    (many : ∀ env ss, (∀ node ∈ Env.nodes env, A node) →
      ReachFrame r R cells A (FlowSafe A) (execStmts n env ss : M W Flow))
    (repeatLoop : ∀ env fresh test update body, (∀ node ∈ Env.nodes env, A node) →
      ReachFrame r R cells A (FlowSafe A) (loop n env fresh test update body : M W Flow))
    (enter : ∀ env ss, (∀ node ∈ Env.nodes env, A node) →
      ReachFrame r R cells A (fun env' => ∀ node ∈ env'.nodes, A node) (enterBlock env ss : M W Env))
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
    apply (enter env [init] h).bind
    intro next hn
    apply (step next init hn).bind
    intro _ _
    exact (reachFrame_copyBindings away outside next _ hn).bind fun iteration hi =>
      repeatLoop iteration _ test update body hi
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
