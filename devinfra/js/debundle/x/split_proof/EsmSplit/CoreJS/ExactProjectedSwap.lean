import EsmSplit.CoreJS.CellFootprint
import EsmSplit.CoreJS.LinkedPivotCheck
import EsmSplit.CoreJS.PrefixProjection

/-! One adjacent swap of closed primitive initializers. Prefix projection can
prove that a linked read has the same bounded behavior as a closed expression.
No projection or swap evaluates a retained operation. -/

namespace CoreJS.ExactProjectedSwap

open LinkedBehavior

private structure Closed (expr : Expr) where
  value : Val
  result : ∀ (w : World) fuel env,
    (evalExpr (W := w) fuel env expr = outOfFuel) ∨
      evalExpr (W := w) fuel env expr = pure value

private def checkClosed : (expr : Expr) → Option (Closed expr)
  | .undef => some ⟨.undef, fun _ n _ => by cases n; exact .inl rfl; exact .inr rfl⟩
  | .null => some ⟨.null, fun _ n _ => by cases n; exact .inl rfl; exact .inr rfl⟩
  | .bool b => some ⟨.bool b, fun _ n _ => by cases n; exact .inl rfl; exact .inr rfl⟩
  | .num n => some ⟨.num n, fun _ f _ => by cases f; exact .inl rfl; exact .inr rfl⟩
  | .str s => some ⟨.str s, fun _ n _ => by cases n; exact .inl rfl; exact .inr rfl⟩
  | .binary op x y => do
    guard op.numberArithmetic
    let a ← checkClosed x
    let b ← checkClosed y
    match opResult : binop op a.value b.value with
    | .ok (.num v) =>
      return ⟨.num v, by
        intro w fuel env
        cases fuel with
        | zero => exact .inl rfl
        | succ fuel =>
          rw [evalExpr]
          rcases a.result w fuel env with exhausted | value
          · rw [exhausted]; exact .inl rfl
          rw [value, M.pure_bind]
          rcases b.result w fuel env with exhausted | value
          · rw [exhausted]; exact .inl rfl
          rw [value, M.pure_bind, opResult]; exact .inr rfl⟩
    | _ => none
  | _ => none

private theorem runDecl {w : World} {expr : Expr} (closed : Closed expr)
    {env : Env} {name : Name} {cell : Addr} (linked : env.vars.lookup name = some cell)
    (fuel id : Nat) :
    ((runUnit (W := w) fuel env id (.decl .const name expr)) = outOfFuel) ∨
      ∀ s : State w, runUnit fuel env id (.decl .const name expr) s =
        .ok .normal { s.assign cell closed.value with region := 0 } := by
  cases fuel with
  | zero => exact .inl rfl
  | succ fuel =>
    rcases closed.result w fuel env with exhausted | value
    · left
      funext s
      simp only [runUnit, inRegion, execStmt, exhausted, M.bind_apply]
      rfl
    · right
      intro s
      simp only [runUnit, inRegion, execStmt, value, M.pure_bind, initVar, linked]
      rfl

/-- The common prefix can end abruptly. When the swap is reached, each
initializer either exhausts its bound or writes its own distinct cell. If
either exhausts, CoreJS returns the same state-free `Res.fuel`. Otherwise
the suffix receives the same complete state in both schedules. -/
private theorem commute {w : World} {e f : Expr} {envX envY : Env} {x y : Name}
    {cx cy : Addr} (first : Closed e) (second : Closed f)
    (hx : envX.vars.lookup x = some cx) (hy : envY.vars.lookup y = some cy) (distinct : cx ≠ cy)
    (i j fuel : Nat) (pre tail : List LinkedUnit) (s : State w) :
    runUnits fuel (pre ++ (i, .decl .const x e, envX) :: (j, .decl .const y f, envY) :: tail) s =
      runUnits fuel (pre ++ (j, .decl .const y f, envY) :: (i, .decl .const x e, envX) :: tail) s := by
  simp only [runUnits_append, M.bind_apply]
  cases runUnits fuel pre s with
  | throw | unknown | fuel => rfl
  | ok _ state =>
    simp only [runUnits, M.bind_apply]
    rcases runDecl (w := w) first hx fuel i with stopX | runX <;>
      rcases runDecl (w := w) second hy fuel j with stopY | runY
    · rw [stopX, stopY]
    · rw [stopX, runY]; rfl
    · rw [stopY, runX]; rfl
    · simp only [runX, runY]
      have same :
          { ({ state.assign cx first.value with region := 0 }).assign cy second.value with region := 0 } =
            { ({ state.assign cy second.value with region := 0 }).assign cx first.value with region := 0 } := by
        simpa only [State.assign] using
          congrArg (fun t : State w => { t with region := 0 })
            (State.assign_comm state cx cy first.value second.value distinct)
      rw [same]

private def checkSwap (before after : List LinkedUnit) :
    Option (PLift (∀ w n (state : State w), runUnits n before state = runUnits n after state)) := do
  let index ← (before.zip after).findIdx? fun (u, v) => u.id != v.id
  let pre := before.take index
  match rest : before.drop index with
  | (i, .decl .const x e, envX) :: (j, .decl .const y f, envY) :: tail =>
    let a ← checkClosed e
    let b ← checkClosed f
    let some cx := envX.vars.lookup x | none
    let some cy := envY.vars.lookup y | none
    if hx : envX.vars.lookup x = some cx then
      if hy : envY.vars.lookup y = some cy then
        if distinct : cx ≠ cy then
          let ⟨same⟩ ← sameUnits?
            (pre ++ (j, .decl .const y f, envY) :: (i, .decl .const x e, envX) :: tail) after
          return ⟨by
            intro w fuel state
            have split := List.take_append_drop index before
            rw [rest] at split
            rw [← split, commute a b hx hy distinct i j fuel pre tail state, runUnits_same fuel same]⟩
        else none
      else none
    else none
  | _ => none

/-- Check the actual linked schedules after proof-only projection, then
transport equality back to the original units, including the opaque suffix. -/
def check (pivots : Std.HashSet Nat) (ready : State World.probe) (before after : List LinkedUnit) :
    Option (PLift (∀ w n, runUnits n before (linkState w ready) =
      runUnits n after (linkState w ready))) := do
  let ⟨left, ⟨source⟩⟩ := PrefixProjection.prepareExact pivots ready before
  let ⟨right, ⟨candidate⟩⟩ := PrefixProjection.prepareExact pivots ready after
  let ⟨swap⟩ ← checkSwap left right
  return ⟨fun w n => (source w n).trans ((swap w n _).trans (candidate w n).symm)⟩

end CoreJS.ExactProjectedSwap
