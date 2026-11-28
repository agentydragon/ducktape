import EsmSplit.Cli
import EsmSplit.CoreJS.AuthenticatedSource
import EsmSplit.CoreJS.LinkWorld
import EsmSplit.LowerSplit.Input

open Lean (Json toJson)
open EsmSplit CoreJS CoreJS.LinkedBehavior

private def status {w : World} {α : Type} : Res w α → String
  | .ok .. => "ok"
  | .fuel => "fuel"
  | .unknown why _ => "unknown: " ++ why
  | .throw (.error _ why) _ => "throw: " ++ why
  | .throw .. => "throw"

private def draw (basis : Addr) : M World.probe Unit := do
  let noop ← makeFunc { vars := [], this := .undef } (.mk "" .normal false [] [])
  let props := ["moveTo", "lineTo", "bezierCurveTo", "closePath"].map fun k => (PropertyKey.str k, .data noop true true true)
  let context ← alloc { proto := some objectProto, props }
  let curve ← CoreJS.construct 256 (← readCell basis) [.obj context]
  let call (method : String) (args : List Val) := do
    let _ ← callFn 256 (← getProp (callFn 256) curve (.str method)) curve args
    pure ()
  call "lineStart" []
  for x in [0.0, 6.0, 12.0] do call "point" [.num x, .num x]
  call "lineEnd" []

private def check (input : Json) : Except String Json := do
  let ast ← input.getObjVal? "source"
  let text ← input.getObjValAs? String "sourceText"
  let spec ← parseSpec (← input.getObjVal? "spec")
  let decoded ← AuthenticatedSource.decodeIndexedSource ast text spec.entry
  let bundle := LowerSplit.sourceBundle decoded.nodes
  let plan := mkPlan bundle spec
  let candidate := splitProgramIndexed decoded.source.body bundle plan
  let links ← checkProbePair decoded.program candidate
  let some original := LinkedBehavior.exports World.probe decoded.program | throw "source public cells missing"
  let some emitted := LinkedBehavior.exports World.probe candidate | throw "candidate public cells missing"
  let some basis := original.lookup "Basis" | throw "source Basis missing"
  let runs := List.map (fun p => match runProgram World.probe 256 p with
    | .ok () ready => status (draw basis ready)
    | result => status result) [decoded.program, candidate]
  return Json.mkObj [
    ("valid", toJson (decide (Valid bundle spec plan))),
    ("assignsHome", toJson (decide (AssignsHome bundle plan))),
    ("orderOk", toJson (decide (OrderOk bundle plan))),
    ("order", toJson plan.execOrder),
    ("moduleOrder", toJson candidate.evalOrder),
    ("sourcePublic", toJson (original.map Prod.fst)),
    ("candidatePublic", toJson (emitted.map Prod.fst)),
    ("sameExports", toJson (exportsMatchFrom links.before links.after)),
    ("draw", toJson runs)]

def main : IO UInt32 := do
  let input ← (← IO.getStdin).getLine
  match Json.parse input >>= check with
  | .ok report => IO.println report.compress; return 0
  | .error why => IO.eprintln why; return 1
