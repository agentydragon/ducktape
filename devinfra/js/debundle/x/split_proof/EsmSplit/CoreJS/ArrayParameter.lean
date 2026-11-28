import EsmSplit.CoreJS.Eval
import EsmSplit.CoreJS.Scope

/-! A flat, fixed array parameter is lowered to a binding statement at the
start of the function. The next body statement runs only after binding and
IteratorClose finish normally. -/

namespace CoreJS

variable {W : World}

/-- The compiler's parameter initializer uses the same binding evaluator as
an array declaration, with the argument value read from its saved slot. -/
theorem flatArrayParameter_exec (fuel : Nat) (env : Env) (slot : Name)
    (elements : List (Option BindingPattern)) (body : List Stmt) :
    (execStmts (fuel + 3) env (.bind .var (.array elements none) (.var slot) :: body) : M W Flow) =
      continueBody
        (do
          let value ← readVar env slot
          bindArrayPattern (callFn fuel) fuel (bindPattern fuel env .var) elements none value
          pure .normal)
        (execStmts (fuel + 2) env body) :=
  rfl

/-- If closing the still-open iterator throws, the parameter initializer
propagates that throw and the complete state left by closing. In particular,
cleanup writes survive and no statement in the function body is run. -/
theorem flatArrayParameter_throwing_close (fuel : Nat) (env : Env) (slot : Name)
    (elements : List (Option BindingPattern)) (body : List Stmt)
    (initial read acquired bound closed : State W) (value error : Val)
    (cursor remaining : IteratorCursor) (slotRead : readVar env slot initial = .ok value read)
    (acquiredIterator : getIterator (callFn fuel) value read = .ok cursor acquired)
    (boundElements :
      bindArrayElements (callFn fuel) (bindPattern fuel env .var) elements (some cursor) acquired =
        .ok (some remaining) bound)
    (closeThrows : callIteratorReturn (callFn fuel) remaining bound = .throw error closed) :
    execStmts (fuel + 3) env (.bind .var (.array elements none) (.var slot) :: body) initial =
      .throw error closed := by
  rw [flatArrayParameter_exec]
  simp only [continueBody, M.bind_apply, slotRead, bindArrayPattern, acquiredIterator,
    boundElements, iteratorClose, attempt, closeThrows]
  rfl

end CoreJS
