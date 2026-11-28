import EsmSplit.CoreJS.LinkedAsyncMethod
import EsmSplit.CoreJS.PrimitiveAwait
import Lean.Data.Json

open CoreJS Lean
open CoreJS.PrimitiveAwait

private def binding (env : Env) (name : String) (turn : Turn World.probe) : Except String Val :=
  match readVar env name turn.state with
  | .ok v _ => .ok v
  | _ => throw s!"could not read {name}"

private def observed (env : Env) (immediate : Turn World.probe) : Except String Json := do
  let .obj first ← binding env "first" immediate | throw "expected the first Promise"
  let .obj second ← binding env "second" immediate | throw "expected the second Promise"
  let .obj last ← binding env "last" immediate | throw "expected the return-await Promise"
  let .obj payload ← binding env "payload" immediate | throw "expected the payload"
  let .str beforeDrain ← binding env "trace" immediate | throw "expected the immediate trace"
  unless immediate.jobs.length == 3 && [first, second, last].all (isPending immediate.state) do
    throw "the module phase did not leave three pending Promises"
  let .ok .paused mid := drain 128 1 immediate | throw "expected to pause after one job"
  let some (.fulfilled (.num n)) := (mid.state.objAt first).bind (·.promise) |
    throw "first job did not fulfill"
  unless isPending mid.state second && isPending mid.state last do
    throw "the first drain ran more than one job"
  let .ok .empty done := drain 128 2 mid | throw "expected an empty queue"
  let some (.rejected (.obj rejection)) := (done.state.objAt second).bind (·.promise) |
    throw "second job did not reject with an object"
  let some (.fulfilled (.str awaited)) := (done.state.objAt last).bind (·.promise) |
    throw "return-await did not fulfill with the saved operand"
  let .str afterDrain ← binding env "trace" done | throw "expected the resumed trace"
  return Json.mkObj [("beforeDrain", toJson beforeDrain), ("afterDrain", toJson afterDrain),
    ("result", toJson n), ("sameRejection", toJson (rejection == payload)),
    ("awaited", toJson awaited)]

private def probe (request : Json) : Except String Json := do
  let source ← request.getObjVal? "source"
  let input ← LinkedAsyncMethod.decode source "fixture.mjs"
  match LinkedAsyncMethod.runImmediate World.probe 128 input with
  | .unknown why turn =>
    return Json.mkObj [("unknown", toJson why), ("jobs", toJson turn.jobs.length),
      ("bodyRefusals", toJson input.bodyRefusals)]
  | .ok env turn => observed env turn
  | .throw _ _ => throw "unexpected top-level throw"
  | .fuel => throw "unexpected fuel exhaustion"

def main : IO Unit := do
  let line ← (← IO.getStdin).getLine
  let result ← IO.ofExcept (Json.parse line >>= probe)
  IO.println result.compress
