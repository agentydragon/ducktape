import EsmSplit.CoreJS.Scope

namespace CoreJS.SwitchFallthrough

variable {w : World}

private theorem continue_assoc (a b c : M w Flow) :
    continueBody (continueBody a b) c = continueBody a (continueBody b c) := by
  funext state
  simp only [continueBody, M.bind_apply]
  cases h : a state with
  | ok flow next => cases flow <;> rfl
  | _ => rfl

/-- The lowered fallthrough uses an ordinary statement list. A suffix runs
only after the prefix completes normally, with exactly the remaining fuel.
Thrown and abrupt completions retain the effects of the executed prefix. -/
theorem execStmts_append (fuel : Nat) (env : Env) (first rest : List Stmt) :
    (execStmts fuel env (first ++ rest) : M w Flow) =
      continueBody (execStmts fuel env first) (execStmts (fuel - first.length) env rest) := by
  induction first generalizing fuel with
  | nil =>
    cases fuel <;> rfl
  | cons head tail ih =>
    cases fuel with
    | zero => rfl
    | succ n =>
      simp only [List.cons_append, List.length_cons, Nat.succ_sub_succ_eq_sub, execStmts]
      rw [ih, continue_assoc]

/-- The enclosing switch's break scope consumes only its own unlabeled break;
return, continue, throw and exhausted fuel are not converted to normal flow. -/
theorem breakScope_result (fuel : Nat) (env : Env) (body : Stmt) (state : State w) :
    execStmt (fuel + 1) env (.breakScope body) state =
      match execStmt fuel env body state with
      | .ok .brk next => .ok .normal next
      | result => result := by
  simp only [execStmt, M.bind_apply]
  cases execStmt fuel env body state with
  | ok flow next => cases flow <;> rfl
  | _ => rfl

end CoreJS.SwitchFallthrough
