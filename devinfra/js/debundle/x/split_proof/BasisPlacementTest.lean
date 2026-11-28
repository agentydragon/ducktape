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

private def contextMethod (env : Env) (code : Nat) (fails : Bool) : M World.probe Val := do
  let trace := Expr.get .this (.str "trace")
  let digit := Expr.num (Float.ofNat code)
  let next := Expr.binary .add (.binary .mul trace (.num 10)) digit
  makeFunc env (.mk "" .normal false []
    ([.expr (.assign (.member .this (.str "trace")) next)] ++
      (if fails then [.throw (.var "sentinel")] else [])))

private def draw (basis : Addr) (fails : Bool) : M World.probe Json := do
  let sentinel ← alloc { proto := some objectProto, props := [] }
  let sentinelCell ← newCell ⟨some (.obj sentinel), false⟩
  let env : Env := { vars := [("sentinel", sentinelCell)], this := .undef }
  let names := [("moveTo", 1), ("lineTo", 2), ("bezierCurveTo", 3), ("closePath", 4)]
  let props ← names.mapM fun (k, code) => do
    let method ← contextMethod env code (fails && code == 3)
    return (PropertyKey.str k, .data method true true true)
  let context ← alloc { proto := some objectProto, props := ("trace", .data (.num 0) true true true) :: props }
  let curve ← CoreJS.construct 256 (← readCell basis) [.obj context]
  let get (receiver : Val) (k : String) := getProp (callFn 256) receiver (.str k)
  let call (method : String) (args : List Val) := do
    let _ ← callFn 256 (← get curve method) curve args
    pure ()
  let number (receiver : Val) (k : String) := do
    let .num n ← get receiver k | unknown s!"expected a number at {k}"
    pure n.toUInt64.toNat
  call "lineStart" []
  call "point" [.num 0, .num 0]
  call "point" [.num 6, .num 6]
  let third ← attempt (call "point" [.num 12, .num 12])
  let threw ← match third with
    | .error (.obj a) =>
      if a == sentinel then pure true else unknown "a different object was thrown"
    | .error _ => unknown "expected the supplied sentinel"
    | .ok () =>
      call "lineEnd" []
      pure false
  let sameContext := decide ((← get curve "_context") = .obj context)
  let fields ← ["_point", "_x0", "_x1"].mapM (number curve)
  return Json.mkObj [("trace", toJson (← number (.obj context) "trace")),
    ("threw", toJson threw), ("sameContext", toJson sameContext), ("fields", toJson fields)]

private def observe (p : Program) (basis : Addr) : List Json :=
  [false, true].map fun fails =>
    match runProgram World.probe 256 p with
    | .ok () ready =>
      match draw basis fails ready with
      | .ok report _ => report
      | result => toJson (status result)
    | result => toJson (status result)

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
  let runs := [decoded.program, candidate].map (observe · basis)
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
