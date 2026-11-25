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
  let _ ← CoreJS.LinkedBehavior.certifyJointStatic app provider
  return ()

def main : IO UInt32 := do
  let line ← (← IO.getStdin).getLine
  match Json.parse line >>= check with
  | .error why => IO.eprintln why; return 1
  | .ok () => IO.println "certified"; return 0
