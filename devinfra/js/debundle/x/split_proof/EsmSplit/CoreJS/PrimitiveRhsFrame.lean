import EsmSplit.CoreJS.CellFootprint
import EsmSplit.CoreJS.PrimitiveRhs

/-! All-outcome state preservation for the checked primitive RHS grammar.
Unlike a replay footprint, this does not assert that results agree between
states: `typeof` can inspect the heap, although it does not change it. -/

namespace CoreJS.PrimitiveRhs

variable {W : World}

private theorem inert_outOfFuel {α : Type} : Inert (outOfFuel : M W α) := by
  intro s
  trivial

/-- An actual unary evaluation is inert, including the special lookup and TDZ
behavior of `typeof` applied directly to a variable. -/
private theorem inert_unary (fuel : Nat) (env : Env) (op : UnOp) (operand : Expr)
    (operandInert : Inert (evalExpr fuel env operand : M W Val)) :
    Inert (evalExpr (fuel + 1) env (.unary op operand) : M W Val) := by
  cases op <;> try (exact inert_bind operandInert (fun v => inert_bind inert_getState (fun s => inert_lift (unop s _ v))))
  case typeof =>
    cases operand <;> try (exact inert_bind operandInert (fun v => inert_bind inert_getState (fun s => inert_lift (unop s .typeof v))))
    case var x =>
      simp only [evalExpr]
      split
      · exact inert_pure _
      · exact inert_bind (inert_readVar env x) (fun v => inert_bind inert_getState (fun s => inert_lift (unop s .typeof v)))

/-- Short-circuit evaluation preserves state on either branch, including
when coercion or the selected branch ends abruptly. -/
theorem inert_logical (fuel : Nat) (env : Env) (op : LogOp) (left right : Expr)
    (leftInert : Inert (evalExpr fuel env left : M W Val))
    (rightInert : Inert (evalExpr fuel env right : M W Val)) :
    Inert (evalExpr (fuel + 1) env (.logical op left right) : M W Val) := by
  exact inert_bind leftInert fun value =>
    inert_bind (inert_lift (shortCircuits op value)) fun stop => by
      cases stop
      · exact rightInert
      · exact inert_pure value

/-- Every expression recognized by the source RHS grammar preserves the exact
CoreJS state for normal, throw and unknown results, at every fuel and world.
Fuel exhaustion has no returned state. Values and heap-dependent observations
are not equated across different starting states. -/
theorem eval_inert {allows : Name → Bool} {expr : Expr} (shape : Rhs allows expr) :
    ∀ fuel (env : Env), Inert (evalExpr fuel env expr : M W Val) := by
  intro fuel
  induction fuel generalizing expr with
  | zero => intro env; exact inert_outOfFuel
  | succ fuel ih =>
    intro env
    cases shape with
    | undef => exact inert_pure _
    | null => exact inert_pure _
    | bool b => exact inert_pure _
    | num n => exact inert_pure _
    | str text => exact inert_pure _
    | read name allowed => exact inert_readVar env name
    | unary allowed operand => exact inert_unary fuel env _ _ (ih operand env)
    | binary left right =>
      exact inert_bind (ih left env) (fun a => inert_bind (ih right env) (fun b => inert_lift (binop _ a b)))
    | logical left right => exact inert_logical fuel env _ _ _ (ih left env) (ih right env)
    | cond test yes no =>
      exact inert_bind (ih test env) fun value =>
        inert_bind (inert_lift (toBoolean value)) fun selected => by
          cases selected
          · exact ih no env
          · exact ih yes env

/-- A successful result from the existing source checker supplies the same
all-outcome frame fact; no additional syntax admission is introduced. -/
theorem checked_inert (allows : Name → Bool) (expr : Expr)
    (accepted : (check allows expr).isSome = true) (fuel : Nat) (env : Env) :
    Inert (evalExpr fuel env expr : M W Val) := by
  cases checked : check allows expr with
  | none => simp [checked] at accepted
  | some evidence => exact eval_inert evidence.down fuel env

/-- Actual declaration outcomes preserve the preexisting cell and its
mutability until the initializer completes; then only the resolved cell is
initialized. Abrupt outcomes retain the entire original state. -/
def LexicalOutcome (s : State W) (addr : Addr) (binding : Binding) : Res W Flow → Prop
  | .ok flow t => flow = .normal ∧ ∃ value, t = s.assign addr value ∧
      t.cellAt addr = some { binding with value := some value } ∧
      ∀ other, other ≠ addr → t.cellAt other = s.cellAt other
  | .throw _ t | .unknown _ t => t = s
  | .fuel => True

/-- Source-checked RHS evaluation and the actual lexical write establish the
initialized cell without assuming an evaluation result or successful link. -/
theorem lexical_outcome {allows : Name → Bool} {expr : Expr} (shape : Rhs allows expr)
    (fuel : Nat) (env : Env) (kind : DeclKind) (lexical : kind = .let_ ∨ kind = .const)
    (name : Name) (addr : Addr) (binding : Binding) (s : State W)
    (lookup : env.vars.lookup name = some addr) (present : s.cellAt addr = some binding) :
    LexicalOutcome s addr binding (execStmt (fuel + 1) env (.decl kind name expr) s) := by
  have execute : (execStmt (fuel + 1) env (.decl kind name expr) : M W Flow) =
      (evalExpr fuel env expr >>= fun value => initVar env name value >>= fun _ => pure .normal) := by
    rcases lexical with h | h <;> subst kind <;> rfl
  rw [execute]
  simp only [M.bind_apply]
  have inert := eval_inert shape fuel env s
  cases result : evalExpr fuel env expr s with
  | ok value t =>
    rw [result] at inert
    subst t
    have step : ((initVar env name value >>= fun _ => pure .normal) : M W Flow) s =
        .ok .normal (s.assign addr value) := by
      simp only [initVar, lookup]
      rfl
    change LexicalOutcome s addr binding
      (((initVar env name value >>= fun _ => pure .normal) : M W Flow) s)
    rw [step]
    exact ⟨rfl, value, rfl, by simp [State.cellAt_assign_self, present],
      fun other different => State.cellAt_assign_other s addr value other different⟩
  | throw value t => rw [result] at inert; exact inert
  | unknown why t => rw [result] at inert; exact inert
  | fuel => trivial

private def exampleEnv : Env := ⟨[("x", ⟨1, 0⟩)], .undef, none⟩
private def exampleState : State World.probe :=
  { initialState World.probe 1 with cells := #[#[], #[⟨none, true⟩]] }
private def objectState : State World.probe :=
  { initialState World.probe 1 with cells := #[#[], #[⟨some (.obj ⟨0, 0⟩), true⟩]] }

/-- The special unresolvable `typeof` rule does not evaluate its operand. -/
example (s : State W) :
    evalExpr 1 ⟨[], .undef, none⟩ (.unary .typeof (.var "missing")) s =
      .ok (.str "undefined") s := by rfl

/-- A resolved, uninitialized binding still throws, without a write. -/
example : evalExpr 1 exampleEnv (.unary .typeof (.var "x")) exampleState =
    .throw (.error .reference "cannot access 'x' before initialization") exampleState := by rfl

/-- Object-to-number coercion is unknown in CoreJS and preserves the state. -/
example : evalExpr 2 exampleEnv (.unary .pos (.var "x")) objectState =
    .unknown "an object is converted to a primitive" objectState := by rfl

example (s : State W) : evalExpr 0 exampleEnv (.var "x") s = .fuel := by rfl

/-- The skipped branch would throw if it were reached. -/
example (s : State W) :
    evalExpr 2 ⟨[], .undef, none⟩ (.logical .and (.bool false) (.var "missing")) s =
      .ok (.bool false) s := by rfl

/-- Conditional evaluation skips the unselected read, which would throw. -/
example (s : State W) :
    evalExpr 2 ⟨[], .undef, none⟩ (.cond (.bool true) (.num 2) (.var "missing")) s =
      .ok (.num 2) s := by rfl

example (s : State W) :
    evalExpr 2 ⟨[], .undef, none⟩ (.cond (.bool false) (.num 2) (.var "missing")) s =
      .throw (.error .reference "missing is not defined") s := by rfl

#guard (check (fun _ => true) (.assign (.var "x") (.bool true))).isNone


end CoreJS.PrimitiveRhs
