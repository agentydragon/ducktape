import Lean.Data.Json
import EsmSplit.CoreJS.AsyncPrefix
import EsmSplit.CoreJS.Eval

open CoreJS Lean

private def writeTrace (text : String) : Stmt :=
  .expr (.assign (.var "trace") (.str text))

private def body (env : Env) (stmts : List Stmt) (fuel : Nat) : M World.probe Val := do
  match ← execStmts fuel env stmts with
  | .ret value => return value
  | _ => return .undef

private def observations : M World.probe Json := do
  let trace ← newCell ⟨some (.str ""), true⟩
  let env := { vars := [("trace", trace)], this := .undef, home := none }
  -- A prototype is provided by the caller at this boundary. Use a fresh
  -- stand-in; none of these probes claims to implement Promise methods.
  let prototype ← alloc { proto := some objectProto, props := [] }
  let program := body env [writeTrace "s", .ret (.num 7)]
  let .obj first ← AsyncPrefix.invoke prototype program 32 | unknown "expected a reference"
  let .str atReturn ← readVar env "trace" | unknown "expected a trace"
  let .obj second ← AsyncPrefix.invoke prototype program 32 | unknown "expected a second reference"
  let .obj rejected ← AsyncPrefix.invoke prototype
    (body env [writeTrace "x", .throw (.str "err")]) 32 | unknown "expected a rejection reference"
  let .str afterReject ← readVar env "trace" | unknown "expected a trace"
  let some (.fulfilled (.num value)) := (← getObj first).promise | unknown "expected primitive fulfillment"
  let some (.rejected (.str reason)) := (← getObj rejected).promise | unknown "lost rejection"
  let thrown ← alloc { proto := some objectProto, props := [] }
  let .obj p ← AsyncPrefix.invoke prototype (fun _ => throwVal (.obj thrown)) 32 |
    unknown "expected an object rejection"
  let some (.rejected (.obj payload)) := (← getObj p).promise | unknown "expected a stored object"
  return Json.mkObj [
    ("atReturn", toJson atReturn), ("afterReject", toJson afterReject),
    ("value", toJson value), ("reason", toJson reason),
    ("fresh", toJson (first != second)),
    ("objectRejectionIdentity", toJson (thrown == payload)),
    ("ordinaryProperties", toJson (← getObj first).props.length)
  ]

def main : IO Unit := do
  let state := initialState World.probe 1
  let state := { state with region := 1 }
  let .ok observed _ := observations state | throw (IO.userError "ordinary completion failed")
  let .fuel := AsyncPrefix.invoke objectProto (fun _ => pure .null) 0 state |
    throw (IO.userError "zero fuel starts the body")
  let awaitCut (_ : Nat) : M World.probe Val := do
    setCell ⟨1, 0⟩ (.str "s")
    unknown "await continuation is not implemented"
  let .ok trace entered := newCell ⟨some (.str ""), true⟩ state |
    throw (IO.userError "trace allocation failed")
  let .unknown "await continuation is not implemented" interrupted :=
    AsyncPrefix.invoke objectProto awaitCut 32 entered |
    throw (IO.userError "unsupported suspension was settled")
  unless (interrupted.cellAt trace).map (fun (b : Binding) => b.value) == some (some (.str "s")) do
    throw (IO.userError "the prefix effect was discarded")
  let .unknown "async object resolution requires thenable semantics" _ :=
    AsyncPrefix.invoke objectProto (fun _ => pure (.obj objectProto)) 32 state |
    throw (IO.userError "unsupported object resolution was settled")
  IO.println observed.compress
