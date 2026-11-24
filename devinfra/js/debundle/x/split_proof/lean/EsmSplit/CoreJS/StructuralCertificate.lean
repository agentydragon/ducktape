import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.LinkedPivotCertificate

namespace CoreJS.LinkedBehavior

/-- The checked executions are exactly the original and generated graphs.
Unchanged units run against the same opaque-code world in both graphs. -/
def StructuralASTEquivalent (profile : HostProfile) (source providers files : Lean.Json)
    (plan : EsmSplit.Plan) (pivot : Option Nat)
    (deployment : Option LinkedDeployment.Input) : Prop :=
  ∃ original candidate,
    LinkedInput.decodeStructuralPair source providers files plan pivot profile deployment = .ok (original, candidate) ∧
    NormalEquivalent original candidate ∧ SameExports original candidate

/-- Compute the certificate directly with the executable, proof-carrying
checks. The proof relates the same concrete ASTs later sent to the printer. -/
def certifyStructuralASTs (profile : HostProfile) (source providers files : Lean.Json)
    (plan : EsmSplit.Plan) (pivot : Option Nat)
    (deployment : Option LinkedDeployment.Input) : Except String (PLift (StructuralASTEquivalent profile source providers files plan pivot deployment)) := do
  match decoded : LinkedInput.decodeStructuralPair source providers files plan pivot profile deployment with
  | .error reason => throw reason
  | .ok (original, candidate) =>
    let ⟨certified⟩ ← checkStructuralSchedule pivot original candidate
    return ⟨⟨original, candidate, decoded, certified⟩⟩

end CoreJS.LinkedBehavior
