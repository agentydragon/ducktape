import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.LinkedPivotCheck
import EsmSplit.CoreJS.LinkWorld
import EsmSplit.CoreJS.ProbeReplay
import EsmSplit.CoreJS.StateMatch

/-! Verify class creation after reread. The instance fields stay in the class
closure. The positive witness does not instantiate it or admit publication. -/

namespace CoreJS.DeferredClassCreation

open Lean LinkedBehavior

structure Checked (source files : Json) (plan : EsmSplit.Plan) (ordinal fuel : Nat) where
  before : Program
  after : Program
  decoded : LinkedInput.decodeDeferredClassPair source files plan ordinal = .ok (before, after)
  exports : SameExports before after
  allFuel : ∀ w n, runProgram w n before = runProgram w n after
  final : State World.probe
  created : ∀ w, runProgram w fuel before = .ok () (replaceWorld w final)

def check (source files : Json) (plan : EsmSplit.Plan) (ordinal fuel : Nat) :
    Except String (Checked source files plan ordinal fuel) := do
  match decoded : LinkedInput.decodeDeferredClassPair source files plan ordinal with
  | .error reason => throw reason
  | .ok (before, after) =>
    let pair ← checkProbePair before after
    let some ⟨initial⟩ := StateMatch.probe pair.before.ready pair.after.ready |
      throw "different initial linked state"
    let some ⟨units⟩ := sameUnits? (unitsOf pair.before.linked) (unitsOf pair.after.linked) |
      throw "different linked units or capture addresses"
    let h ← if h : exportsMatchFrom pair.before pair.after = true then pure (PLift.up h)
      else throw "different live export cells"
    let some ⟨final, ⟨created⟩⟩ := checkInstanceClassCreation pair.before fuel |
      throw "instance-only class creation did not complete"
    return {
      before, after, decoded, final, created,
      exports := pair.sameExports h.down
      allFuel := fun w n => by
        rw [run_after_link n (pair.before.forWorld w), run_after_link n (pair.after.forWorld w)]
        change runUnits n (unitsOf pair.before.linked) (linkState w pair.before.ready) =
          runUnits n (unitsOf pair.after.linked) (linkState w pair.after.ready)
        rw [initial, runUnits_same n units]
    }

end CoreJS.DeferredClassCreation
