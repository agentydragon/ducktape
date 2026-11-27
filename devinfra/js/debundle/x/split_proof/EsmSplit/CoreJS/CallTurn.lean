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
  simp only [evalExpr, callFnTurn_overlap, TurnM.lift_bind]

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
  simp only [perform, callFnTurn_overlap, TurnM.lift_bind]

/-- A finalizer gets the post-body turn on both language return and throw.
A finalizer's own jump or throw wins. Refusal and fuel cannot run it. -/
theorem execFinally_turn (n : Nat) (env : Env) (body finalizer : Stmt) :
    (TurnM.lift (execStmt (n + 1) env (.tryFinally body finalizer)) : TurnM W Flow) = (do
      let result ← TurnM.attempt (TurnM.lift (execStmt n env body))
      let flow ← TurnM.lift (execStmt n env finalizer)
      TurnM.lift (match flow with
        | .normal => match result with
          | .ok original => pure original
          | .error reason => throwVal reason
        | jump => pure jump)) := by
  rw [execStmt]
  funext turn
  obtain ⟨state, jobs⟩ := turn
  simp only [TurnM.bind_apply, TurnM.attempt, TurnM.lift, M.bind_apply]
  cases execStmt n env body state with
  | ok flow post =>
    simp only [TurnResult.withJobs]
    cases execStmt n env finalizer post <;> rfl
  | throw reason post =>
    simp only [TurnResult.withJobs]
    cases execStmt n env finalizer post <;> rfl
  | unknown => rfl
  | fuel => rfl

end CoreJS
