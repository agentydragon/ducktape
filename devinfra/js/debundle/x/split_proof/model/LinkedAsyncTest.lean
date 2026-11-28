import EsmSplit.CoreJS.Eval
import EsmSplit.CoreJS.LinkedInput
import Lean.Data.Json

open CoreJS Lean

private def observe (program : Program) : M World.probe Json := do
  let modules ← link program
  let [(_, env)] := modules | unknown "expected one linked module"
  runUnits 100 (unitsOf modules)
  let read (name : String) := readVar env name (W := World.probe)
  let .obj first ← read "first" | unknown "expected first promise"
  let .obj second ← read "second" | unknown "expected second promise"
  let .obj rejected ← read "rejected" | unknown "the caller did not continue after the rejection"
  let .obj payload ← read "payload" | unknown "expected the thrown object"
  let one ← getObj first
  let two ← getObj second
  let rejection ← getObj rejected
  let some (.fulfilled (.num v1)) := one.promise | unknown "expected first fulfillment"
  let some (.fulfilled (.num v2)) := two.promise | unknown "expected second fulfillment"
  let some (.rejected (.obj reason)) := rejection.promise | unknown "expected an object rejection"
  let traces ←
    ["afterFirst", "afterSecond", "afterRejection", "afterCaller"].mapM fun name => do
        let .str text ← read name | unknown s!"expected a string in {name}"
        pure (name, toJson text)
  return Json.mkObj
      (traces ++
        [("firstValue", toJson v1), ("secondValue", toJson v2),
          ("fresh", toJson (first != second && first != rejected && second != rejected)),
          ("rejectionIdentity", toJson (reason == payload)),
          ("noOwnKeys", toJson ([one, two, rejection].all (·.props.isEmpty))),
          ("sharedPrototype", toJson ([one, two, rejection].all (·.proto == some promiseProto)))])

private def observeArrows (program : Program) : M World.probe Json := do
  let linked ← link program
  let [(_, env)] := linked | unknown "expected one linked module"
  runUnits 120 (unitsOf linked)
  let read := readVar env (W := World.probe)
  let .obj fn ← read "run" | unknown "expected arrow closure"
  let callee ← getObj fn
  let values ← ["first", "second"].mapM fun name => do
    let .obj result ← read name | unknown "expected promise"
    let promise ← getObj result
    let some (.fulfilled (.str value)) := promise.promise | unknown "expected fulfilled string"
    return value
  return Json.mkObj [("values", toJson values),
    ("constructor", toJson (← isConstructor (.obj fn))),
    ("asyncPrototype", toJson (callee.proto == some asyncFunctionProto)),
    ("ownPrototype", toJson ((callee.props.lookup (.str "prototype")).isSome))]

private def probe (input : Json) : Except String Json := do
  let program ← LinkedInput.decodeSingletonSource (← input.getObjVal? "source") "fixture.mjs"
  let mode ← input.getObjValAs? String "mode"
  if mode == "arrows" then
    match observeArrows program (initialState World.probe program.regions) with
    | .ok result _ => return result
    | .unknown msg _ => throw msg
    | .throw _ _ => throw "arrow call threw synchronously"
    | .fuel => throw "ran out of fuel"
  else if mode == "normal" then
    match observe program (initialState World.probe program.regions) with
    | .ok result _ =>
      return result
    | .unknown msg _ =>
      throw msg
    | .throw _ _ =>
      throw "async call threw synchronously"
    | .fuel =>
      throw "ran out of fuel"
  else
    match runProgram World.probe 100 program with
    | .unknown msg _ =>
      return Json.mkObj [("unknown", toJson msg)]
    | _ =>
      throw "expected unsupported execution"

def main : IO Unit := do
  let input ← (← IO.getStdin).getLine
  match Json.parse input >>= probe with
  | .error msg =>
    throw (IO.userError msg)
  | .ok result =>
    IO.println result.compress
