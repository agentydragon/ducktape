import EsmSplit.CoreJS.Eval
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.PropertyTurn
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

/-- Execute the exact saved member call at its original unit and sub-fuel.
The second scope retains just its checked free bindings. The replacement
getter makes the call boundary visible without adding Promise operations. -/
private def observeMemoCall (program : Program) : Except String Json := do
  let marker : Job := ⟨⟨1, 0⟩, 1, ⟨[], .undef, none⟩, []⟩
  let fuel := 120
  let setup : M World.probe (Expr × Expr × List Expr × Env × Nat) := do
    let linked ← link program
    let units := unitsOf linked
    let some unit := units.getLast? | unknown "missing saved call"
    let .expr (.callMethod receiver key args) := unit.stmt | unknown "expected saved member call"
    runUnits fuel (units.take (units.length - 1))
    let closed : Env := ⟨[], .undef, none⟩
    let logText (text : String) := Stmt.expr (.prim .log [.str text])
    let method ← makeFunc closed (.mk "replacement" .method false ["callback"] [
      logText "call", .expr (.assign (.member .this (.str "callback")) (.var "callback")),
      .throw (.str "stop")])
    let scope ← declare closed [("replacement", ⟨some method, false⟩)]
    let getter ← makeFunc scope (.mk "get then" .method false [] [
      logText "lookup", .expr (.assign (.member .this (.str "seen")) (.bool true)),
      .ret (.var "replacement")])
    modifyObj promiseProto fun obj =>
      { obj with props := (.str "then", .accessor getter .undef true true) ::
        obj.props.filter (·.1 != .str "then") }
    return (receiver, key, args, unit.env, unit.id)
  let .ok (receiver, key, args, env, ordinal) state :=
    setup (initialState World.probe program.regions) | throw "could not prepare the saved call"
  let needed := receiver.free ++ key.free ++ freeExprs args
  let captured := env.capture needed
  let op (scope : Env) := memberCallTurn (fuel - 2) scope receiver key args (callFnTurn (fuel - 2))
  have bound : Agree needed env captured := by
    refine ⟨rfl, rfl, fun x hx => ?_⟩
    symm
    show (needed.eraseDups.filterMap fun y => (env.vars.lookup y).map (y, ·)).lookup x = _
    simp only [lookup_filterMap, List.mem_eraseDups, hx, ite_true]
  have sameTurn : op env = op captured := memberCallTurn_agree _ _ _ _ bound
  let start : Turn World.probe := ⟨{ state with region := ordinal + 1 }, [marker]⟩
  let .throw (.str "stop") after := op env start | throw "replacement did not throw"
  unless (op captured start).erase matches .throw (.str "stop") _ do
    throw "captured scope changed the outcome"
  let _ := sameTurn
  let heap := (after.state.heap[ordinal + 1]?).getD #[]
  let some promise := heap.find? (fun obj => obj.promise.isSome) | throw "missing actual memo promise"
  let some (.fulfilled (.str value)) := promise.promise | throw "memo did not settle to a string"
  let receiverSeen := promise.props.lookup (.str "seen") matches some (.data (.bool true) ..)
  let callbackSeen := match promise.props.lookup (.str "callback") with
    | some (.data (.obj fn) ..) => ((after.state.objAt fn).bind (·.code)).isSome
    | _ => false
  return Json.mkObj [("value", toJson value), ("trace", toJson after.state.out),
    ("receiverSeen", toJson receiverSeen), ("callbackSeen", toJson callbackSeen),
    ("queue", toJson (after.jobs.map (·.promise.region)))]

private def probe (input : Json) : Except String Json := do
  let program ← LinkedInput.decodeSingletonSource (← input.getObjVal? "source") "fixture.mjs"
  let mode ← input.getObjValAs? String "mode"
  if mode == "memo-call" then
    observeMemoCall program
  else if mode == "arrows" then
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
