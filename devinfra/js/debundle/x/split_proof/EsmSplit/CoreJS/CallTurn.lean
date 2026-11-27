import EsmSplit.CoreJS.Eval
import EsmSplit.CoreJS.Scope
import EsmSplit.CoreJS.Turn

/-! One dispatch table, with an ordinary and a turn-valued observation.
The existing local bodies and host conversations do not enqueue in this
slice. A future queue-producing body must use a turn-valued handler. -/

namespace CoreJS

variable {W : World}

private theorem dispatchCall_lift
    (localBody : Func → Env → Val → List Val → M W Val)
    (opaqueBody : Nat → List (Option Addr) → Told → M W Val)
    (callee this : Val) (args : List Val) :
    dispatchCall TurnM.lift (fun f closed r argv => TurnM.lift (localBody f closed r argv))
      (fun code cells told => TurnM.lift (opaqueBody code cells told)) callee this args =
    TurnM.lift (dispatchCall (fun run => run) localBody opaqueBody callee this args) := by
  cases callee <;> simp only [dispatchCall, TurnM.lift_bind]
  congr
  funext obj
  cases obj.code with
  | none => rfl
  | some code =>
    cases code <;> simp only [TurnM.lift_bind]
    split
    · rfl
    · split <;> simp only [TurnM.lift_bind]

/-- Run the same dispatch with an existing turn. Neither the call site
nor a certificate can select a different dispatch table. -/
def callFnTurn (fuel : Nat) (callee this : Val) (args : List Val) : TurnM W Val :=
  match fuel with
  | 0 => TurnM.lift outOfFuel
  | n + 1 =>
    dispatchCall TurnM.lift
      (fun f closed r argv => TurnM.lift (runLocalCall (execStmts n) f closed r argv))
      (fun code cells told => TurnM.lift (converse n code cells told)) callee this args

/-- Full same-fuel overlap, including any changes to the heap, cells, host,
output and handed-out identities. Existing jobs survive every stateful outcome. -/
theorem callFnTurn_overlap (fuel : Nat) (callee this : Val) (args : List Val) :
    (callFnTurn fuel callee this args : TurnM W Val) = TurnM.lift (callFn fuel callee this args) := by
  cases fuel with
  | zero => rw [callFn]; rfl
  | succ n => rw [callFn]; exact dispatchCall_lift _ _ _ _ _

/-- Ordinary callee and argument evaluation hand their current turn to the
resolved call. In particular, callee expressions, aliases and nested
argument calls retain their evaluation order and the original bound. -/
theorem evalCall_turn (n : Nat) (env : Env) (f : Expr) (args : List Expr) :
    (TurnM.lift (evalExpr (n + 1) env (.call f args)) : TurnM W Val) = (do
      let callee ← TurnM.lift (evalExpr n env f)
      let argv ← TurnM.lift (evalArgs n env args)
      callFnTurn n callee .undef argv) := by
  simp only [evalExpr, directExprCall, callFnTurn_overlap, TurnM.lift_bind]

/-- An opaque host request uses the same resolved call, after the existing
host-to-CoreJS conversions and before handing the result back to the host. -/
theorem performCall_turn (n code : Nat) (cells : List (Option Addr)) (f r : WVal) (args : List WVal) :
    (TurnM.lift (perform (callFn n)
      (fun callee argv => constructTarget n callee callee argv) code cells (.call f r args)) : TurnM W Told) =
      (do
        let callee ← TurnM.lift (fromW f)
        let receiver ← TurnM.lift (fromW r)
        let argv ← TurnM.lift (args.mapM fromW)
        let result ← callFnTurn n callee receiver argv
        TurnM.lift (do pure (.value (← toW result)))) := by
  simp only [perform, requestCall, callFnTurn_overlap, TurnM.lift_bind]

/-- Run cleanup from the body's resulting turn. A model refusal or fuel
does not run it. -/
def finallyTurn (body cleanup : TurnM W Flow) : TurnM W Flow := do
  let result ← TurnM.attempt body
  finishFinally cleanup (fun reason => TurnM.lift (throwVal reason)) result

/-- A finalizer gets the post-body turn on both language return and throw. -/
theorem execFinally_turn (n : Nat) (env : Env) (body finalizer : Stmt) :
    (TurnM.lift (execStmt (n + 1) env (.tryFinally body finalizer)) : TurnM W Flow) =
      finallyTurn (TurnM.lift (execStmt n env body)) (TurnM.lift (execStmt n env finalizer)) := by
  rw [execStmt]
  funext turn
  obtain ⟨state, jobs⟩ := turn
  simp only [finallyTurn, finishFinally, TurnM.bind_apply, TurnM.attempt, TurnM.lift, M.bind_apply]
  cases execStmt n env body state with
  | ok flow post =>
    simp only [TurnResult.withJobs]
    cases execStmt n env finalizer post <;> try rfl
    next cleanup _ => cases cleanup <;> rfl
  | throw reason post =>
    simp only [TurnResult.withJobs]
    cases execStmt n env finalizer post <;> try rfl
    next cleanup _ => cases cleanup <;> rfl
  | unknown => rfl
  | fuel => rfl

/-- The body is supplied as a turn-valued recursion edge, at the caller's
chosen fuel. Lifting its ordinary instantiation preserves the incoming FIFO. -/
theorem ordinaryCallBody_lift (body : Env → List Stmt → M W Flow)
    (f : Func) (closed : Env) (this : Val) (args : List Val) :
    ordinaryCallBody TurnM.lift (fun env ss => TurnM.lift (body env ss)) f closed this args =
      TurnM.lift (ordinaryCallBody (fun m => m) body f closed this args) := by
  simp only [ordinaryCallBody, TurnM.lift_bind]

theorem execStmts_turn (n : Nat) (env : Env) (stmt : Stmt) (rest : List Stmt) :
    (TurnM.lift (execStmts (n + 1) env (stmt :: rest)) : TurnM W Flow) =
      continueBody (TurnM.lift (execStmt n env stmt)) (TurnM.lift (execStmts n env rest)) := by
  simp only [execStmts, continueBody, TurnM.lift_bind]
  congr
  funext flow
  cases flow <;> rfl

/-- The ordinary conversation and the sidecar use the same host step, the
same bounds, and the same handling of replies and language exceptions. -/
theorem converse_turn (n code : Nat) (cells : List (Option Addr)) (told : Told) :
    (TurnM.lift (converse (n + 1) code cells told) : TurnM W Val) =
      conversationStep TurnM.lift TurnM.attempt
        (fun req => TurnM.lift (perform (callFn n) (fun callee args => constructTarget n callee callee args)
          code cells req))
        (fun answer => TurnM.lift (converse n code cells answer)) told := by
  simp only [converse, conversationStep, TurnM.lift_bind]
  congr
  funext req
  cases req <;> simp only [TurnM.lift_bind, TurnM.lift_attempt]

/-- A recursive computation may change the queue and still agree on the
ordinary observation. Quantify over arbitrary incoming turns. -/
def TurnM.Projects (run : TurnM W α) (ordinary : M W α) : Prop :=
  ∀ turn, (run turn).erase = ordinary turn.state

theorem TurnM.Projects.lift (ordinary : M W α) : Projects (TurnM.lift ordinary) ordinary :=
  fun _ => TurnResult.erase_withJobs _ _

theorem TurnM.Projects.pure (value : α) : Projects (pure value : TurnM W α) (pure value) :=
  fun _ => rfl

theorem TurnM.Projects.bind {run : TurnM W α} {ordinary : M W α}
    {next : α → TurnM W β} {legacyNext : α → M W β}
    (head : Projects run ordinary) (tail : ∀ a, Projects (next a) (legacyNext a)) :
    Projects (run >>= next) (ordinary >>= legacyNext) := by
  intro turn
  rw [bind_apply, M.bind_apply, ← head turn]
  cases run turn with
  | ok value after => exact tail value after
  | _ => rfl

theorem TurnM.Projects.attempt {run : TurnM W α} {ordinary : M W α}
    (h : Projects run ordinary) :
    Projects (TurnM.attempt run) (CoreJS.attempt ordinary) := by
  intro turn
  simp only [TurnM.attempt, CoreJS.attempt, ← h turn]
  cases run turn <;> rfl

/-- The local body may extend the FIFO without changing the projected result. -/
theorem ordinaryCallBody_projects
    {body : Env → List Stmt → TurnM W Flow} {legacy : Env → List Stmt → M W Flow}
    (overlap : ∀ env ss, TurnM.Projects (body env ss) (legacy env ss))
    (f : Func) (closed : Env) (this : Val) (args : List Val) :
    TurnM.Projects (ordinaryCallBody TurnM.lift body f closed this args)
      (ordinaryCallBody (fun m => m) legacy f closed this args) :=
  (TurnM.Projects.lift _).bind fun env => (overlap env f.body).bind fun _ => .lift _

theorem finallyTurn_projects {body cleanup : TurnM W Flow} {oldBody oldCleanup : M W Flow}
    (bodyOverlap : TurnM.Projects body oldBody) (cleanupOverlap : TurnM.Projects cleanup oldCleanup) :
    TurnM.Projects (finallyTurn body cleanup) (do
      let result ← attempt oldBody
      finishFinally oldCleanup throwVal result) :=
  bodyOverlap.attempt.bind fun result => cleanupOverlap.bind fun flow => by
    cases flow <;> try exact .pure _
    cases result with
    | ok value => exact .pure value
    | error reason => exact .lift (throwVal reason)

/-- Conversion and the value reply see the callback's updated turn. -/
theorem requestCall_projects {call : Val → Val → List Val → TurnM W Val} {legacy : Call W}
    (overlap : ∀ f receiver args, TurnM.Projects (call f receiver args) (legacy f receiver args))
    (f receiver : WVal) (args : List WVal) :
    TurnM.Projects (requestCall TurnM.lift call f receiver args)
      (requestCall (fun m => m) legacy f receiver args) :=
  (TurnM.Projects.lift _).bind fun callee =>
    (TurnM.Projects.lift _).bind fun this =>
      (TurnM.Projects.lift _).bind fun argv =>
        (overlap callee this argv).bind fun value =>
          (TurnM.Projects.lift (toW value)).bind fun result => .pure (Told.value result)

/-- The next host step receives the effects of the previous request, even
when that request changes the queue. -/
theorem conversationStep_projects {request : Request → TurnM W Told} {legacy : Request → M W Told}
    {next : Told → TurnM W Val} {legacyNext : Told → M W Val}
    (requests : ∀ req, TurnM.Projects (request req) (legacy req))
    (replies : ∀ answer, TurnM.Projects (next answer) (legacyNext answer)) (told : Told) :
    TurnM.Projects (conversationStep TurnM.lift TurnM.attempt request next told)
      (conversationStep (fun m => m) attempt legacy legacyNext told) := by
  apply (TurnM.Projects.lift _).bind
  intro req
  cases req <;> first
    | exact .lift _
    | exact (TurnM.Projects.lift _).bind fun _ => .lift _
    | exact (requests _).attempt.bind fun _ => (TurnM.Projects.lift _).bind replies

end CoreJS
