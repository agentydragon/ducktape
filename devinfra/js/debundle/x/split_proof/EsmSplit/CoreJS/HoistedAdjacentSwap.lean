import EsmSplit.CoreJS.LinkedBehavior
import EsmSplit.CoreJS.Split

/-! An adjacent transposition of two modeled function declaration steps.
Instantiation is separate: both declarations have already installed their
closures before the schedule starts. -/

namespace CoreJS.HoistedAdjacentSwap

open LinkedBehavior

variable {w : World}

/-- A function declaration's evaluation still checks fuel. At zero fuel
it fails; at positive fuel it leaves the state with the top-level region reset. -/
theorem runUnit_funDecl (fuel id : Nat) (f : Func) (env : Env) (state : State w) :
    runUnit fuel env id (.funDecl f) state =
      match fuel with
      | 0 => .fuel
      | _ + 1 => .ok .normal { state with region := 0 } := by
  cases fuel <;> rfl

/-- Swap two adjacent, already-instantiated declaration steps. Every other
step retains its ordinal, scope and place in the schedule. -/
theorem runUnits_swap (fuel : Nat) (pre tail : List LinkedUnit)
    (a b : Nat) (f g : Func) (envF envG : Env) :
    (runUnits fuel (pre ++ ((a, .funDecl f, envF) :: (b, .funDecl g, envG) :: tail)) : M w Unit) =
      runUnits fuel (pre ++ ((b, .funDecl g, envG) :: (a, .funDecl f, envF) :: tail)) := by
  rw [runUnits_append, runUnits_append]
  refine M.bind_congr rfl fun _ => ?_
  funext state
  cases fuel <;> simp only [runUnits, M.bind_apply, runUnit_funDecl]

/-- Compare the two actual links. The source schedule must contain the pair;
the candidate must match the exact transposition, including free cell
addresses for every retained step. Equality is at the same fuel and includes
all abrupt results and the complete state. -/
theorem runProgram_eq {source candidate : Program} (fuel : Nat)
    (before : CheckedLink source w) (after : CheckedLink candidate w)
    (ready : before.ready = after.ready) (pre tail : List LinkedUnit)
    (a b : Nat) (f g : Func) (envF envG : Env)
    (original : unitsOf before.linked =
      pre ++ ((a, .funDecl f, envF) :: (b, .funDecl g, envG) :: tail))
    (swapped : SameUnits
      (pre ++ ((b, .funDecl g, envG) :: (a, .funDecl f, envF) :: tail))
      (unitsOf after.linked)) :
    runProgram w fuel source = runProgram w fuel candidate := by
  rw [run_after_link fuel before, run_after_link fuel after, original, ready,
    runUnits_swap, runUnits_same fuel swapped]

end CoreJS.HoistedAdjacentSwap
