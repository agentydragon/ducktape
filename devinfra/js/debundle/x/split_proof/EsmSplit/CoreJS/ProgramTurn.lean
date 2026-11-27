import EsmSplit.CoreJS.Eval
import EsmSplit.CoreJS.Scope
import EsmSplit.CoreJS.Turn

/-! The program turn boundary. Evaluation still uses the existing CoreJS
linker and statement evaluator. No certificate is an input to either. -/

namespace CoreJS

/-- Share initialization, linking, and propagation of linking failures. This
combinator is also used by the explicit await model during its migration. -/
def withLinkedTurn (w : World) (p : Program)
    (next : List (Module × Env) → Turn w → TurnResult w α) : TurnResult w α :=
  TurnResult.andThen (link p) ⟨initialState w p.regions, []⟩ next

/-- The fixed production semantics at the turn boundary. The current
statement evaluator has no enqueue rule. -/
def runProgramTurn (w : World) (fuel : Nat) (p : Program) : TurnResult w Unit :=
  withLinkedTurn w p fun linked turn =>
    TurnResult.withJobs turn.jobs (runUnits fuel (unitsOf linked) turn.state)

/-- The migration equation is exact even for unsuccessful linking, and
makes no assumption about the input or the evaluation fuel. -/
theorem runProgramTurn_sync (w : World) (fuel : Nat) (p : Program) :
    runProgramTurn w fuel p = TurnResult.withJobs [] (runProgram w fuel p) := by
  simp only [runProgramTurn, withLinkedTurn, TurnResult.andThen, runProgram, M.bind_apply]
  cases link p (initialState w p.regions) <;> rfl

/-- Recover the legacy observation from the same turn. -/
theorem runProgram_eq_erase (w : World) (fuel : Nat) (p : Program) :
    (runProgramTurn w fuel p).erase = runProgram w fuel p := by
  rw [runProgramTurn_sync, TurnResult.erase_withJobs]

end CoreJS
