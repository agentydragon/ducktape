import Lean.Data.Json
import EsmSplit.CoreJS.Eval
import EsmSplit.CoreJS.LinkedPrimitiveAwait
import EsmSplit.CoreJS.PrimitiveAwait

open CoreJS Lean
open CoreJS.PrimitiveAwait

private def binding (env : Env) (name : String) (turn : Turn World.probe) : Except String Val :=
  match readVar env name turn.state with
  | .ok v _ => .ok v
  | _ => .error s!"could not read {name}"

private def observed (env : Env) (immediate : Turn World.probe) : Except String Json := do
  let .obj first ← binding env "first" immediate | throw "expected a Promise from the first call"
  let .obj second ← binding env "second" immediate | throw "expected a Promise from the second call"
  let .obj payload ← binding env "payload" immediate | throw "expected a payload object"
  let .str atReturn ← binding env "atReturn" immediate | throw "expected a trace after return"
  let .str duringTurn ← binding env "duringTurn" immediate | throw "expected a trace after the caller"
  let .str immediateTrace ← binding env "trace" immediate | throw "expected the live trace"
  unless immediateTrace == duringTurn && isPending immediate.state first && isPending immediate.state second do
    throw "the module phase ran a continuation"
  let .ok .paused one := drain 100 1 immediate | throw "expected one continuation left"
  let .str firstJob ← binding env "trace" one | throw "expected a trace after one job"
  unless firstJob == duringTurn ++ "A" && isPending one.state second do
    throw "the drain did not run the first job alone"
  let some (.rejected (.obj reason)) := (one.state.objAt first).bind (·.promise) |
    throw "the first job did not reject on the original Promise"
  let .ok .empty done := drain 100 1 one | throw "expected the queue to be empty"
  let some (.fulfilled (.num value)) := (done.state.objAt second).bind (·.promise) |
    throw "the second job did not fulfill on the original Promise"
  let .str afterDrain ← binding env "trace" done | throw "expected a final trace"
  return Json.mkObj [("atReturn", toJson atReturn), ("duringTurn", toJson duringTurn),
    ("afterDrain", toJson afterDrain), ("fresh", toJson (first != second)),
    ("rejectionIdentity", toJson (reason == payload)), ("value", toJson value)]

private def probe (input : Json) : Except String Json := do
  let source ← input.getObjVal? "source"
  let fuel := (input.getObjValAs? Nat "fuel").toOption.getD 100
  let statusOnly := (input.getObjValAs? Bool "statusOnly").toOption.getD false
  let input ← LinkedPrimitiveAwait.decode source "fixture.mjs"
  let result := LinkedPrimitiveAwait.runImmediate World.probe fuel input
  match result with
  | .unknown why turn => return Json.mkObj [("unknown", toJson why), ("jobs", toJson turn.jobs.length)]
  | .throw (.str why) turn => return Json.mkObj [("throw", toJson why), ("jobs", toJson turn.jobs.length)]
  | .throw _ _ => throw "unexpected module throw"
  | .fuel => return Json.mkObj [("end", toJson "fuel")]
  | .ok env immediate =>
    if statusOnly then
      return Json.mkObj [("end", toJson "normal"), ("jobs", toJson immediate.jobs.length)]
    else observed env immediate

def main : IO Unit := do
  let line ← (← IO.getStdin).getLine
  match Json.parse line >>= probe with
  | .ok expected => IO.println expected.compress
  | .error why => throw (IO.userError why)
