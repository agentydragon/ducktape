import EsmSplit.CoreJS.DeploymentPlacement
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.LinkedPivotCertificate
import EsmSplit.CoreJS.PlanPlacement
import EsmSplit.CoreJS.SyntaxMatch
import EsmSplit.LowerSplit.Input

/-! A prefix extraction with two retained class declarations. The class bodies
need no intrinsic model here: the output must retain their exact syntax,
resolved cells, and order. Moving an initializer past another is a different
proof, even if both classes have retained bodies. -/

namespace CoreJS.RetainedClassConstruction

open Lean EsmSplit LinkedBehavior

private def isLocalExport (node : Json) : Bool :=
  Lower.typ node == "ExportNamedDeclaration" &&
    Lower.isNull (Lower.fld node "declaration") && Lower.isNull (Lower.fld node "source")

private def prefixDeclaration (node : Json) : Bool :=
  Lower.typ node == "FunctionDeclaration" ||
    (Lower.typ node == "VariableDeclaration" && Lower.str (Lower.fld node "kind") == "const" &&
      (Lower.items (Lower.fld node "declarations")).all
        (fun d => Lower.typ (Lower.fld d "id") == "Identifier" &&
          Lower.typ (Lower.fld d "init") == "Literal"))

structure Construction (nodes : Array Json) (request : Spec) where
  before : Program
  after : Program
  plan : Plan
  places : Places ((PlanPlacement.assignments plan).filter fun row => row.1 < nodes.size - 1) before after
  proof : ScheduleResult [] before after
  allOutcomes : proof.1 = .allOutcomes

def construct (nodes : Array Json) (request : Spec) : Except String (Construction nodes request) := do
  unless request.sequential && !request.privateEntryBody &&
      request.modules.all (fun row => row.1.endsWith ".mjs") do
    throw "expected an order-preserving two-file .mjs placement"
  let [helper] := request.order | throw "expected one helper"
  unless helper != request.entry && (request.modules.map Prod.fst).Perm [helper, request.entry] do
    throw "expected exactly the entry and helper"
  let exportRow :: publicClass :: internalClass :: prefixRev := nodes.toList.reverse |
    throw "expected an internal class followed by a public class and export row"
  unless isLocalExport exportRow && Lower.typ publicClass == "ClassDeclaration" &&
      Lower.typ internalClass == "ClassDeclaration" && prefixRev.all prefixDeclaration do
    throw "expected a declaration prefix and two classes"
  let k := prefixRev.length + 1
  let bundle := LowerSplit.sourceBundle nodes
  unless ((request.modules.flatMap Prod.snd).map (fun
      | .stmt id => some id | .name _ => none)).Perm ((List.range nodes.size).map some) do
    throw "select each source ordinal once"
  let source ← LinkedInput.decodeRetainedSource nodes request.entry []
  unless source.body.map Prod.fst == List.range (k + 1) &&
      source.exports == [(Lower.str (Lower.fld (Lower.fld publicClass "id") "name"),
        Lower.str (Lower.fld (Lower.fld publicClass "id") "name"))] do
    throw "expected only the original public class binding"
  let plan := mkPlan bundle request
  let some entry := plan.find request.entry | throw "missing entry"
  let some helperModule := plan.find helper | throw "missing helper"
  unless (decide (Valid bundle request plan)) && plan.execOrder == List.range nodes.size &&
      entry.stmts == [k, k + 1] && helperModule.stmts == List.range k &&
      entry.exports.isEmpty && entry.imports.map Prod.fst == [helper] && helperModule.imports.isEmpty do
    throw "expected unchanged source order, owners and public entry bindings"
  let before : Program := ⟨request.entry, [source]⟩
  let after := splitProgramIndexed source.body bundle plan
  let ⟨places⟩ ← checkPlaces ((PlanPlacement.assignments plan).filter fun row => row.1 < nodes.size - 1) before after
  let proof ← (checkStructuralSchedule [] before after {}).mapError StructuralRefusal.reason
  if full : proof.1 = .allOutcomes then return ⟨before, after, plan, places, proof, full⟩
  else throw "expected identical linked order and captures"

/-- The reparsed programs inherit the same-order result and public cells. -/
def Construction.checkOutput {nodes request} (built : Construction nodes request) (source files : Json) :
    Except String (PLift (∃ decoded,
      LinkedInput.decodeStructuralSingleton source (Json.mkObj []) files built.plan [] = .ok decoded ∧
      ScheduleEquivalent .allOutcomes (StableIntrinsic.Worlds []) decoded.before decoded.after ∧
      SameExports decoded.before decoded.after)) := do
  match checked : LinkedInput.decodeStructuralSingleton source (Json.mkObj []) files built.plan [] with
  | .error why => throw why.reason
  | .ok actual =>
    let some ⟨before⟩ := SyntaxMatch.programEq actual.before built.before |
      throw "retained-class readback differs from the checked source"
    let some ⟨after⟩ := SyntaxMatch.programEq actual.after built.after |
      throw "retained-class readback differs from the checked output"
    return ⟨actual, checked, by
      have behavior := built.proof.2.2.down
      rw [built.allOutcomes] at behavior
      simpa only [before, after] using behavior⟩

end CoreJS.RetainedClassConstruction
