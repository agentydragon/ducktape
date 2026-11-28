import EsmSplit.CoreJS.AuthenticatedSource
import EsmSplit.CoreJS.LinkedBehavior
import EsmSplit.SplitFiles

open Lean CoreJS EsmSplit SplitFiles

private def observe (program : Program) (state : State World.probe) : Except String Json := do
  let some exports := LinkedBehavior.exports World.probe program | throw "missing public cells"
  let some cell := exports.lookup "observations" | throw "missing observations"
  let read : M World.probe Json := do
    let value ← readCell cell
    let get (key : String) := getProp (callFn 256) value (.str key)
    let .num length ← get "length" | unknown "expected a result array"
    let mut items := #[]
    for i in [:length.toUInt64.toNat] do
      let item ← get (toString i)
      match item with
      | .num n => items := items.push (toJson n.toUInt64.toNat)
      | .bool b => items := items.push (toJson b)
      | .str s => items := items.push (toJson s)
      | _ => unknown "expected a scalar observation"
    return .arr items
  match read state with
  | .ok result _ => return result
  | .unknown why _ => throw why
  | _ => throw "observation did not finish"

def main (args : List String) : IO Unit := do
  let [input] := args | throw (IO.userError "expected one source path")
  IO.FS.withTempDir fun work => do
    let source ← IO.FS.readFile input
    let ast ← loadProgram input work true
    let decoded ← IO.ofExcept (AuthenticatedSource.decodeIndexedSource ast source "entry.mjs")
    match runProgram World.probe 256 decoded.program with
    | .ok () state => IO.println (← IO.ofExcept (observe decoded.program state)).compress
    | .unknown why _ => IO.println (Json.mkObj [("unknown", toJson why)]).compress
    | .throw (.error .type why) _ =>
      IO.println (Json.mkObj [("throw", toJson "TypeError"), ("detail", toJson why)]).compress
    | .throw (.error _ why) _ => throw (IO.userError why)
    | _ => throw (IO.userError "projection did not finish")
