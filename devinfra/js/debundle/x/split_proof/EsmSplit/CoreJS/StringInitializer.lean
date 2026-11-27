import EsmSplit.CoreJS.Scope

namespace CoreJS

/-- A view of initialized lexical cells at one point in one execution. The
caller must reestablish these reads before using the result at another point. -/
def ReadsStrings (known : Name → Option String) (env : Env) (s : State w) : Prop :=
  ∀ x text, known x = some text → readVar env x s = .ok (.str text) s

/-- The exact result, with the evaluator bound at which it becomes available.
Below the bound the expression runs out of fuel; otherwise the heap, cells,
output and host state are all unchanged. -/
structure StringInitializer (known : Name → Option String) (e : Expr) where
  text : String
  depth : Nat
  run : ∀ {w} (env : Env) (s : State w), ReadsStrings known env s → ∀ fuel,
    evalExpr fuel env e s = if depth ≤ fuel then .ok (.str text) s else .fuel

/-- Recognize the string additions and conversions produced by an untagged
template. A name contributes only the value supplied by a checked cell read.
This does not evaluate a getter or convert an object to a primitive. -/
def stringInitializer? (known : Name → Option String) : (e : Expr) → Option (StringInitializer known e)
  | .str text => some ⟨text, 1, by intro w env s h fuel; cases fuel <;> rfl⟩
  | .var x => do
    match h : known x with
    | none => none
    | some text =>
      return ⟨text, 1, by
        intro w env s reads fuel
        cases fuel with
        | zero => rfl
        | succ n => exact reads x text h⟩
  | .unary .toString e => do
    let a ← stringInitializer? known e
    return ⟨a.text, a.depth + 1, by
      intro w env s reads fuel
      cases fuel with
      | zero => rfl
      | succ n =>
        simp only [evalExpr, M.bind_apply, a.run env s reads n, Nat.add_le_add_iff_right]
        by_cases h : a.depth ≤ n <;> simp only [h, ite_true, ite_false] <;> rfl⟩
  | .binary .add left right => do
    let a ← stringInitializer? known left
    let b ← stringInitializer? known right
    return ⟨a.text ++ b.text, max a.depth b.depth + 1, by
      intro w env s reads fuel
      cases fuel with
      | zero => rfl
      | succ n =>
        simp only [evalExpr, M.bind_apply, a.run env s reads n, Nat.add_le_add_iff_right, Nat.max_le]
        by_cases ha : a.depth ≤ n <;> by_cases hb : b.depth ≤ n <;>
          simp only [ha, hb, b.run env s reads n, and_self, and_false, and_true,
            ite_true, ite_false] <;> rfl⟩
  | _ => none

theorem StringInitializer.closed {e : Expr} (a : StringInitializer (fun _ => none) e)
    (w : World) (fuel : Nat) (env : Env) :
    (evalExpr fuel env e : M w Val) = outOfFuel ∨ ∃ v, evalExpr (W := w) fuel env e = pure v := by
  have run (s : State w) := a.run env s (fun _ _ h => by contradiction) fuel
  by_cases h : a.depth ≤ fuel
  · refine Or.inr ⟨.str a.text, funext fun s => ?_⟩
    change evalExpr fuel env e s = .ok (.str a.text) s
    simpa only [h, ite_true] using run s
  · refine Or.inl (funext fun s => ?_)
    change evalExpr fuel env e s = .fuel
    simpa only [h, ite_false] using run s

end CoreJS
