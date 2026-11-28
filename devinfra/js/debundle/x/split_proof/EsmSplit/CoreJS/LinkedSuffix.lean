import EsmSplit.CoreJS.LinkedBehavior
import EsmSplit.CoreJS.LinkedPivotCheck

/-! A checked suffix can follow a separately proved rewrite of a prefix. The
suffix may invoke the world: it executes the same units with the same linked
captures, starting from the same result of the respective prefixes. -/

namespace CoreJS.LinkedSuffix

open LinkedBehavior

/-- Compare the actual linked schedules after the cut, including each unit's
identity, statement, receiver and captured cells. -/
def check (cut : Nat) (before after : List LinkedUnit) :
    Option (PLift (SameUnits (before.drop cut) (after.drop cut))) :=
  sameUnits? (before.drop cut) (after.drop cut)

/-- Take the prefix proof at the full graphs' post-link states. A proof about
separately linked prefixes is insufficient: the suffix might affect allocation. -/
theorem run_eq {source candidate : Program} {w : World} (fuel cut : Nat)
    (before : CheckedLink source w) (after : CheckedLink candidate w)
    (prefixRuns : runUnits fuel ((unitsOf before.linked).take cut) before.ready =
      runUnits fuel ((unitsOf after.linked).take cut) after.ready)
    (suffix : SameUnits ((unitsOf before.linked).drop cut) ((unitsOf after.linked).drop cut)) :
    runProgram w fuel source = runProgram w fuel candidate := by
  rw [run_after_link fuel before, run_after_link fuel after]
  have splitRun (us : List LinkedUnit) (ready : State w) :
      runUnits fuel us ready =
        ((runUnits fuel (us.take cut) >>= fun _ => runUnits fuel (us.drop cut)) : M w Unit) ready := by
    have append := congrArg (runUnits (W := w) fuel) (List.take_append_drop cut us)
    rw [runUnits_append] at append
    exact congrFun append.symm ready
  rw [splitRun (unitsOf before.linked), splitRun (unitsOf after.linked), M.bind_apply, M.bind_apply,
    prefixRuns, runUnits_same fuel suffix]

end CoreJS.LinkedSuffix
