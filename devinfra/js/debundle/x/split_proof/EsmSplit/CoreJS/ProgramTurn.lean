import EsmSplit.CoreJS.Eval
import EsmSplit.CoreJS.Scope
import EsmSplit.CoreJS.Turn
import EsmSplit.CoreJS.TurnUnits

/-! The program turn boundary. Evaluation still uses the existing CoreJS
linker and statement evaluator. No certificate is an input to either. -/

namespace CoreJS

/-- Share initialization, linking, and propagation of linking failures. This
combinator is also used by the explicit await model during its migration. -/
def withLinkedTurn (w : World) (p : Program)
    (next : List (Module × Env) → Turn w → TurnResult w α) : TurnResult w α :=
  TurnResult.andThen (link p) ⟨initialState w p.regions, []⟩ next

/-- The fixed evaluator runs the linked schedule and the executable bodies
carried by the lowered program. It takes no certificate or evaluation mode. -/
def runProgramTurn (w : World) (fuel : Nat) (p : TurnProgram) : TurnResult w Unit :=
  withLinkedTurn w p.program fun linked turn =>
    TurnUnits.runLinked fuel p.cuts (unitsOf linked) turn

/-- An ordinary program embeds without any suspension bodies, including on
failed links. -/
theorem runProgramTurn_sync (w : World) (fuel : Nat) (p : Program) :
    runProgramTurn w fuel (p : TurnProgram) = TurnResult.withJobs [] (runProgram w fuel p) := by
  simp only [runProgramTurn, withLinkedTurn, TurnResult.andThen, runProgram, M.bind_apply, TurnUnits.runLinked]
  cases link p (initialState w p.regions)
  case ok linked ready => cases runUnits fuel (unitsOf linked) ready <;> rfl
  all_goals rfl

/-- Recover the legacy observation for ordinary programs. -/
theorem runProgram_eq_erase (w : World) (fuel : Nat) (p : Program) :
    (runProgramTurn w fuel (p : TurnProgram)).erase = runProgram w fuel p := by
  rw [runProgramTurn_sync, TurnResult.erase_withJobs]

end CoreJS
