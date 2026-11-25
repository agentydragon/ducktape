import EsmSplit.CoreJS.Move

/-! # Evaluate expressions from their ordered property writes

An expression description records the result and every write in evaluation order.
A trace witnesses those writes for a particular state and receiver binding. -/

namespace CoreJS

/-- A sequence of setter calls that run normally for every call implementation.
The binding used as receiver still denotes the same object at each boundary. -/
inductive WriteTrace (env : Env) (x : Name) (a : Addr) : State W → List (String × Val) → State W → Prop where
  | nil (read : readVar env x s = .ok (.obj a) s) : WriteTrace env x a s [] s
  | cons (read : readVar env x s = .ok (.obj a) s)
      (put : ∀ call, setProp call (.obj a) (.str k) v s = .ok () next)
      (rest : WriteTrace env x a next fields t) : WriteTrace env x a s ((k, v) :: fields) t

theorem WriteTrace.readable (h : WriteTrace env x a s fields t) : readVar env x s = .ok (.obj a) s := by
  cases h <;> assumption

theorem WriteTrace.split {l r : List (String × Val)} (run : WriteTrace env x a s (l ++ r) t) :
    ∃ m, WriteTrace env x a s l m ∧ WriteTrace env x a m r t := by
  induction l generalizing s with
  | nil => exact ⟨s, .nil run.readable, run⟩
  | cons f tail ih =>
    cases run with
    | cons hr hp htail =>
      obtain ⟨m, left, right⟩ := ih htail
      exact ⟨m, .cons hr hp left, right⟩

/-- Result value and ordered writes of an expression. Computed keys and right-hand sides
may themselves write to the receiver. Fuel indices follow evalExpr and evalTarget. -/
inductive WriteExpr (receiver : Name) (a : Addr) : Nat → Expr → List (String × Val) → Val → Prop where
  | string : WriteExpr receiver a (n+1) (.str k) [] (.str k)
  | number : WriteExpr receiver a (n+1) (.num v) [] (.num v)
  | receiverRead : WriteExpr receiver a (n+1) (.var receiver) [] (.obj a)
  | seq (lhs : WriteExpr receiver a n l ls lv) (rhs : WriteExpr receiver a n r rs rv) :
      WriteExpr receiver a (n+1) (.seq l r) (ls ++ rs) rv
  | assign (key : WriteExpr receiver a (n+1) k ks kv)
      (rhs : WriteExpr receiver a (n+2) r rs rv) (convert : toStr kv = .ok name) :
      WriteExpr receiver a (n+3) (.assign (.member (.var receiver) k) r) (ks ++ (rs ++ [(name, rv)])) rv

private theorem setProp_key {call : Call W} {key : Val} (h : toStr key = .ok k) :
    setProp call (.obj a) key v = (setProp call (.obj a) (.str k) v : M W Unit) := by
  have convert : (toKey key : M W PropertyKey) = toKey (.str k) := by
    cases key <;> first | contradiction | simp only [toStr] at h; contradiction | simp only [toKey, h]; rfl
  simp only [setProp, convert]

/-- A description and a matching trace determine the expression's complete CoreJS result. -/
theorem WriteExpr.eval (desc : WriteExpr x a fuel e fields v) (trace : WriteTrace env x a s fields t) :
    evalExpr fuel env e s = .ok v t := by
  induction desc generalizing s t with
  | string => cases trace; rw [evalExpr]; rfl
  | number => cases trace; rw [evalExpr]; rfl
  | receiverRead => cases trace with | nil read => rw [evalExpr]; exact read
  | seq l r il ir =>
    obtain ⟨m, first, second⟩ := trace.split
    simp only [evalExpr]
    exact (bind_of_ok (il first)).trans (ir second)
  | @assign n ke ks kv re rs rv name kd rd conv ik ir =>
    obtain ⟨m, first, tail⟩ := trace.split
    obtain ⟨u, second, last⟩ := tail.split
    have read : evalExpr (n+1) env (.var x) s = .ok (.obj a) s := by rw [evalExpr]; exact trace.readable
    have target : evalTarget (n+2) env (.member (.var x) ke) s = .ok (.prop (.obj a) kv) m := by
      rw [evalTarget]
      rw [bind_of_ok read, bind_of_ok (ik first)]
      rfl
    cases last with
    | cons _ put rest =>
      cases rest
      have write : putRef (callFn (n+2)) env (.prop (.obj a) kv) rv u = .ok () t := by
        change setProp _ (.obj a) kv rv u = _
        rw [setProp_key conv]
        exact put _
      rw [evalExpr]
      rw [bind_of_ok target, bind_of_ok (ir second), bind_of_ok write]
      rfl

end CoreJS
