import EsmSplit.CoreJS.CallTurn

namespace CoreJS

variable {W : World}
variable {call : Val → Val → List Val → TurnM W Val} {legacy : Call W}

private theorem getFrom_projects (h : ∀ f r args, TurnM.Projects (call f r args) (legacy f r args))
    (a : Addr) (base : Val) (key : PropertyKey) :
    TurnM.Projects (getFromWith TurnM.lift call a base key) (getFrom legacy a base key) := by
  unfold getFrom getFromWith
  apply (TurnM.Projects.lift _).bind
  intro p
  cases p with
  | none => exact .pure _
  | some p =>
    cases p with
    | data => exact .pure _
    | accessor fn _ _ _ =>
      cases fn <;>
        first
        | exact .pure _
        | exact h _ _ _

private theorem protoProp_projects
    (h : ∀ f r args, TurnM.Projects (call f r args) (legacy f r args)) (owner : String)
    (names : List String) (base : Val) (key : PropertyKey) :
    TurnM.Projects (protoPropWith TurnM.lift call owner names base key)
      (protoProp legacy owner names base key) := by
  cases key <;> simp only [protoProp, protoPropWith] <;> try split
  all_goals
    first
    | exact .pure _
    | exact .lift _
    | exact getFrom_projects h _ _ _

/-- A getter may change the FIFO while keeping the exact ordinary outcome. -/
theorem getProp_projects (h : ∀ f r args, TurnM.Projects (call f r args) (legacy f r args))
    (base key : Val) :
    TurnM.Projects (getPropWith TurnM.lift call base key) (getProp legacy base key) := by
  cases base <;> simp only [getProp, getPropWith] <;> try exact .lift _
  all_goals apply (TurnM.Projects.lift _).bind
  all_goals intro k
  all_goals
    first
    | exact getFrom_projects h _ _ _
    | exact protoProp_projects h _ _ _ _
    |
      split <;>
        first
        | exact .lift _
        | exact protoProp_projects h _ _ _ _

/-- The source read evaluates both operands at the original sub-fuel. -/
theorem evalGet_projects (n : Nat) (env : Env) (receiver key : Expr)
    (h : ∀ f r args, TurnM.Projects (call f r args) (callFn n f r args)) :
    TurnM.Projects
      (readProperty (getPropWith TurnM.lift call) (TurnM.lift (evalExpr n env receiver))
        (TurnM.lift (evalExpr n env key)))
      (evalExpr (n + 1) env (.get receiver key)) := by
  rw [evalExpr]
  exact
    (TurnM.Projects.lift _).bind fun base =>
      (TurnM.Projects.lift _).bind fun k => getProp_projects h base k

/-- The actual host request uses the same read rule. A successful read returns
the getter's updated turn, including after encoding the reply for the world. -/
theorem performGet_projects (n code : Nat) (cells : List (Option Addr)) (receiver key : WVal)
    (h : ∀ f r args, TurnM.Projects (call f r args) (callFn n f r args)) :
    TurnM.Projects (requestGet TurnM.lift (getPropWith TurnM.lift call) receiver key)
      (perform (callFn n) (fun f args => constructTarget n f f args) code cells
        (.get receiver key)) := by
  rw [perform]
  exact
    (TurnM.Projects.lift _).bind fun base =>
      (TurnM.Projects.lift _).bind fun k =>
        (getProp_projects h base k).bind fun value =>
          (TurnM.Projects.lift (toW value)).bind fun reply => .pure (Told.value reply)

end CoreJS
