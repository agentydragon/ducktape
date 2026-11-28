import EsmSplit.CoreJS.ConstructedOutput
import EsmSplit.CoreJS.LinkedBehavior
import EsmSplit.CoreJS.PrimitiveAwait
import EsmSplit.CoreJS.ProgramTurn

/-! A proof interface for a publishing rule, indexed by the actual programs
and by the fixed program evaluator. -/

namespace CoreJS

/-- A language completion is observable; a model refusal or exhausted fuel
is insufficient as evidence of support. -/
def TurnResult.completedTurn : TurnResult W Unit → Option (Turn W)
  | .ok _ turn | .throw _ turn => some turn
  | .unknown .. | .fuel => none

/-- Strong all-outcomes publication proof. Equality is over the entire
immediate turn. In addition, in every world there is a supported language
completion with a successfully drained queue. The witness is not a policy
for selecting host checkpoints or a substitute for the all-bounds proof. -/
structure TurnPublication (source actual : TurnProgram) : Prop where
  exports : LinkedBehavior.SameExports source.program actual.program
  immediate : ∀ w fuel, runProgramTurn w fuel source = runProgramTurn w fuel actual
  supported : ∀ w, ∃ (fuel jobFuel quota : Nat) (turn finished : Turn w),
    (runProgramTurn w fuel source).completedTurn = some turn ∧
      PrimitiveAwait.drain jobFuel quota turn = .ok .empty finished

theorem TurnPublication.ordinary {source actual : Program} (h : TurnPublication source actual) :
    LinkedBehavior.ScheduleEquivalent .allOutcomes (fun _ => True) source actual := by
  intro w _ fuel
  simpa only [runProgram_eq_erase] using congrArg TurnResult.erase (h.immediate w fuel)

theorem TurnPublication.drains (h : TurnPublication source actual) (w : World) (fuel jobFuel quota : Nat) :
    (runProgramTurn w fuel source).checkpoint jobFuel quota =
      (runProgramTurn w fuel actual).checkpoint jobFuel quota :=
  congrArg (TurnResult.checkpoint jobFuel quota) (h.immediate w fuel)

/-- The old observation can identify two turns whose checkpoints disagree. -/
theorem erase_does_not_determine_checkpoint (s : State W) (job : Job) :
    (TurnResult.ok () ⟨s, []⟩).erase = (TurnResult.ok () ⟨s, [job]⟩).erase ∧
      (TurnResult.ok () ⟨s, []⟩).checkpoint 0 0 ≠
        (TurnResult.ok () ⟨s, [job]⟩).checkpoint 0 0 := by
  refine ⟨rfl, ?_⟩
  simp [TurnResult.checkpoint, PrimitiveAwait.drain]

/-- Require the proof on the exact independently decoded output, retaining
the existing file identity check and carrying its equation into the verdict. -/
def checkTurnPublication (files : Lean.Json) (source constructed : Program)
    (h : TurnPublication source constructed) :
    Except String (PLift (∃ actual, ConstructedOutput.decodeConstructedOutput files constructed = .ok actual ∧
      TurnPublication source actual)) := do
  let ⟨reread⟩ ← ConstructedOutput.checkConstructedOutput files constructed
  return ⟨⟨constructed, reread, h⟩⟩

/-- Attaching the semantic proof does not change which staged files pass
the identity check. The proof itself is not built from that check. -/
theorem checkTurnPublication_sameAcceptance (files : Lean.Json) (source constructed : Program)
    (h : TurnPublication source constructed) :
    (checkTurnPublication files source constructed h).isOk =
      (ConstructedOutput.checkConstructedOutput files constructed).isOk := by
  unfold checkTurnPublication
  cases ConstructedOutput.checkConstructedOutput files constructed <;> rfl

end CoreJS
