import EsmSplit.Cli
import EsmSplit.CoreJS.DeploymentPlacement
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.LinkedPivotCheck
import EsmSplit.CoreJS.LinkWorld
import EsmSplit.CoreJS.StateMatch
import EsmSplit.ESTreeEmit
import EsmSplit.ESTreeUnits
import EsmSplit.LowerSplit.Input

/-! Test-only replay of the authored D3 allocation placements. The ordinary
CLI does not dispatch to this runner. -/

open Lean (Json toJson)
open EsmSplit CoreJS CoreJS.LinkedBehavior

private def sameSchedule (before after : Program) (links : CheckedProbePair before after) :
    Except String (PLift (∀ w fuel, runProgram w fuel before = runProgram w fuel after)) := do
  let some ⟨ready⟩ := StateMatch.probe links.before.ready links.after.ready |
    throw "different ready states"
  let some ⟨units⟩ := sameUnits? (unitsOf links.before.linked) (unitsOf links.after.linked) |
    throw "different ordered statements or linked captures"
  return ⟨fun w fuel => by
    rw [run_after_link fuel (links.before.forWorld w), run_after_link fuel (links.after.forWorld w)]
    change runUnits fuel (unitsOf links.before.linked) (linkState w links.before.ready) =
      runUnits fuel (unitsOf links.after.linked) (linkState w links.after.ready)
    rw [ready, runUnits_same fuel units]⟩

private def check (input : Json) : Except String Json := do
  let source ← input.getObjVal? "source"
  let spec ← parseSpec (← input.getObjVal? "spec")
  let sourceText ← source.getObjValAs? String "sourceText"
  let rows ← ESTreeUnits.checkedNodes source sourceText
  let bundle := LowerSplit.sourceBundle rows
  let plan := mkPlan bundle spec
  let planned := Json.mkObj [
    ("valid", toJson (decide (Valid bundle spec plan))),
    ("orderOk", toJson (decide (OrderOk bundle plan))),
    ("plannedRows", toJson (plan.mods.flatMap fun m => m.stmts.map (·, m.name))),
    ("entryGlueExports", toJson (((plan.find plan.entry).map (·.exports)).getD []))]
  if let .ok files := input.getObjVal? "files" then
    let fixed ← input.getObjVal? "fixed"
    match LinkedInput.decodeDeployment spec.entry [⟨source, files, plan⟩] fixed none .empty (fun _ => []) with
    | .error refusal => return Json.mkObj [("plan", planned), ("refusal", toJson refusal.reason)]
    | .ok decoded =>
      let ⟨_places⟩ ← checkPlaces decoded.assignment decoded.before decoded.after
      let links ← checkProbePair decoded.before decoded.after
      let .isTrue exports := (inferInstance : Decidable (exportsMatchFrom links.before links.after = true)) |
        throw "live public cells differ"
      let _exports := links.sameExports exports
      let ⟨_outcomes⟩ ← sameSchedule decoded.before decoded.after links
      let .ok () _ := runProgram World.probe 256 decoded.before |
        throw "original does not complete in the probe world"
      return Json.mkObj [
        ("plan", planned), ("publicCells", toJson "same"), ("completeOutcomes", toJson "same"),
        ("sourceOrder", toJson decoded.before.evalOrder), ("outputOrder", toJson decoded.after.evalOrder),
        ("sourceUnits", toJson ((unitsOf links.before.linked).map LinkedUnit.id)),
        ("outputUnits", toJson ((unitsOf links.after.linked).map LinkedUnit.id)),
        ("executableOwners", toJson decoded.after.fileUnitPlacements)]
  else
    let programs ← ESTreeEmit.programs rows bundle plan sourceText spec.entry
    return Json.mkObj [("plan", planned), ("programs", programs)]

def main : IO UInt32 := do
  let input ← (← IO.getStdin).getLine
  match Json.parse input >>= check with
  | .ok report => IO.println report.compress; return 0
  | .error why => IO.eprintln why; return 1
