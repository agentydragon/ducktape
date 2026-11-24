import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.LinkedPivotCertificate

namespace CoreJS.LinkedBehavior

/-- Identify the statement to lower concretely, if the placement suggests a reorder. The
certificate separately checks the actual linked schedules, unit syntax and resolved cells. -/
private def pivotHint (source : Lean.Json) (plan : EsmSplit.Plan) : Except String (Option Nat) := do
  let nodes ← ESTreeUnits.nodes source
  let body := nodes.map fun j =>
    match (j.getObjValAs? String "type").toOption with
    | some "ImportDeclaration" => false
    | some "ExportNamedDeclaration" => (j.getObjVal? "declaration").toOption != some .null
    | _ => true
  let isBody (ordinal : Nat) := body[ordinal]?.getD false
  let before := (List.range nodes.size).filter isBody
  let after := plan.execOrder.filter isBody
  return ((after.zip before).find? fun (a, b) => a != b).map Prod.fst

/-- The checked executions are exactly the original and generated graphs.
Unchanged units run against the same opaque-code world in both graphs. -/
def StructuralASTEquivalent (profile : HostProfile) (source providers files : Lean.Json)
    (plan : EsmSplit.Plan)
    (deployment : Option LinkedDeployment.Input) : Prop :=
  ∃ pivot original candidate,
    LinkedInput.decodeStructuralPair source providers files plan pivot profile deployment = .ok (original, candidate) ∧
    NormalEquivalent original candidate ∧ SameExports original candidate

/-- Compute the certificate directly with the executable, proof-carrying
checks. The proof relates the same concrete ASTs later sent to the printer. -/
def certifyStructuralASTs (profile : HostProfile) (source providers files : Lean.Json)
    (plan : EsmSplit.Plan)
    (deployment : Option LinkedDeployment.Input) :
    Except String (StructuralProof × PLift (StructuralASTEquivalent profile source providers files plan deployment)) := do
  let pivot ← pivotHint source plan
  match decoded : LinkedInput.decodeStructuralPair source providers files plan pivot profile deployment with
  | .error reason => throw reason
  | .ok (original, candidate) =>
    let (method, ⟨certified⟩) ← checkStructuralSchedule original candidate
    return (method, ⟨⟨pivot, original, candidate, decoded, certified⟩⟩)

end CoreJS.LinkedBehavior
