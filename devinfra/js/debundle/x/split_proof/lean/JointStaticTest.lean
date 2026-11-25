import EsmSplit.CoreJS.JointStatic
import EsmSplit.LowerSplit.Input
import EsmSplit.Cli

open Lean (Json)
open EsmSplit

/-- The fixture hashes and parses both sources and reparses the assembled files.
Use the production resolved-placement planner for each half. -/
private def fragment (input : Json) : Except String CoreJS.LinkedInput.StaticSplit := do
  let entry ← input.getObjValAs? String "entry"
  let chunk ← input.getObjValAs? String "chunk"
  let source ← input.getObjVal? "source"
  let asts ← ESTreeUnits.checkedNodes source (← source.getObjValAs? String "sourceText")
  let spec ← parseResolvedSpec (← input.getObjVal? "placement") chunk entry asts.size
  let bundle := LowerSplit.linkedLayoutBundle asts (Effects.footprintUnits asts)
  return ⟨source, ← input.getObjVal? "files", mkPlan bundle spec⟩

private def check (input : Json) : Except String Unit := do
  let app ← fragment (← input.getObjVal? "app")
  let provider ← fragment (← input.getObjVal? "provider")
  let parts := [app, provider]
  let shared := Json.mkObj []
  let _ ← CoreJS.LinkedBehavior.certifyStaticDeployment app.plan.entry parts shared .empty []
  -- Test the placement checker on bodies lowered from the parsed fixture.
  -- Invalid assignments exercised here would be refused earlier by the decoder.
  let decoded ← CoreJS.LinkedInput.decodeDeployment app.plan.entry parts shared none .empty (fun _ => [])
  let unit :: rest := decoded.assignment | throw "fixture has no planned executable units"
  let check (assignment : List CoreJS.UnitPlacement) :=
    CoreJS.checkPlaces assignment decoded.before decoded.after
  if (check rest).isOk then throw "accepted an unassigned source unit"
  if (check (unit :: decoded.assignment)).isOk then throw "accepted a multiply assigned source unit"
  let wrongOwner := if unit.2 == app.plan.entry then provider.plan.entry else app.plan.entry
  if (check ((unit.1, wrongOwner) :: rest)).isOk then throw "accepted a unit assigned to a different module"
  let missing := { decoded.after with modules := decoded.after.modules.map fun m =>
    { m with body := m.body.filter fun (id, _) => id != unit.1 } }
  if (CoreJS.checkPlaces decoded.assignment decoded.before missing).isOk then
    throw "accepted output missing a planned source unit"

def main : IO UInt32 := do
  let line ← (← IO.getStdin).getLine
  match Json.parse line >>= check with
  | .error why => IO.eprintln why; return 1
  | .ok () => IO.println "certified"; return 0
