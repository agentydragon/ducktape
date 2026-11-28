import EsmSplit.Cli
import EsmSplit.CoreJS.ConstructedOutput
import EsmSplit.CoreJS.IndexedHoistedSwap
import EsmSplit.CoreJS.IndexedInputChecks
import EsmSplit.CoreJS.IndexedSourceSchedule
import EsmSplit.CoreJS.LinkWorld
import EsmSplit.LowerSplit.Input

open Lean (Json toJson)
open EsmSplit CoreJS CoreJS.LinkedBehavior

private def expect (pass : Bool) (reason : String) : Except String Unit :=
  unless pass do
    throw reason

private def nameAt (linked : List (CoreJS.Module × Env)) (path name : String) :
    Except String Addr := do
  let some (_, env) := linked.find? (fun (m, _) => m.name == path) | throw s!"missing module {path}"
  let some addr := env.vars.lookup name | throw s!"missing binding {path}:{name}"
  return addr

private def closureAt (ready : State World.probe) (cell : Addr) : Except String Env := do
  let some (.obj obj) :=
    (ready.cellAt cell).bind (·.value) | throw "expected an initialized function cell"
  let some (.closure _ closed _) :=
    (ready.objAt obj).bind (·.code) | throw "expected a linked closure"
  return closed

private def addrJson (a : Addr) : Json :=
  toJson [a.region, a.index]

/-- Audit the dependency edges in the actual scopes and in the initialized
closures. The suffix statement installs the offset method during execution. -/
private def audit {p : Program} (checked : CheckedLink p World.probe) (scanner : String) :
    Except String Json := do
  let newline ← nameAt checked.linked "entry.mjs" "isNewLine"
  let scan ← nameAt checked.linked scanner "nextLineBreak"
  let position ← nameAt checked.linked "entry.mjs" "Position"
  let info ← nameAt checked.linked "entry.mjs" "getLineInfo"
  expect (decide (newline = ⟨1, 0⟩ ∧ scan = ⟨2, 0⟩ ∧ position = ⟨3, 0⟩ ∧ info = ⟨5, 0⟩))
      "unexpected source allocation regions"
  expect
      (decide
        ((← nameAt checked.linked scanner "isNewLine") = newline ∧
          (← nameAt checked.linked "entry.mjs" "nextLineBreak") = scan))
      "imports point at the wrong live cells"
  let scanEnv ← closureAt checked.ready scan
  let infoEnv ← closureAt checked.ready info
  let _ ← closureAt checked.ready newline
  expect (decide (scanEnv.vars.lookup "isNewLine" = some newline))
      "scanner closed over the wrong cell"
  expect
      (decide
        (infoEnv.vars.lookup "nextLineBreak" = some scan ∧
          infoEnv.vars.lookup "Position" = some position))
      "getLineInfo closed over the wrong scanner or Position cell"
  expect (decide ((checked.ready.cellAt position).bind (·.value) = some .undef))
      "Position should still be undefined after instantiation"
  let some offset :=
    (unitsOf checked.linked).find? (fun u => u.id == 3) | throw "missing offset assignment"
  expect
      (offset.stmt.free.contains "Position" &&
        decide (offset.env.vars.lookup "Position" = some position))
      "offset assignment cannot capture the original Position cell"
  return Json.mkObj
      [("isNewLine", addrJson newline), ("nextLineBreak", addrJson scan),
        ("Position", addrJson position), ("getLineInfo", addrJson info)]

/-- Read the actual eager control files with the original call ordinal.
This layout deliberately puts the call in the scanner before the entry's
Position initializer. It is a counterexample to allowing eager fall-through. -/
private def checkEager (input : Json) (candidate : Program) : Except String Json := do
  let source ←
    AuthenticatedSource.decodeIndexedSource (← input.getObjVal? "eagerSource")
        (← input.getObjValAs? String "eagerText") candidate.entry
  let some (6, stmt) :=
    source.source.body.getLast? | throw "missing original eager call at ordinal 6"
  let layout : Program :=
    { candidate with
      modules :=
        candidate.modules.map fun m =>
          if m.name == "scanner.mjs" then { m with body := m.body ++ [(6, stmt)] } else m }
  let eager ← ConstructedOutput.decodeConstructedOutput (← input.getObjVal? "eagerFiles") layout
  let reason ←
    match IndexedHoistedSwap.checkBehavior source.program eager with
    | .error reason =>
      pure reason
    | .ok _ =>
      throw "accepted an eager call across Position initialization"
  let fuel := 128
  match runProgram World.probe fuel source.program, runProgram World.probe fuel eager with
  | .ok () _, .throw (.error .type "not a constructor") _ =>
    return Json.mkObj
        [("fuel", toJson fuel), ("flat", toJson "ok"),
          ("split", toJson "TypeError: not a constructor"), ("refusal", toJson reason)]
  | _, _ =>
    throw "expected the eager control to complete flat and throw in the split at fuel 128"

private def check (input : Json) : Except String Json := do
  let spec ← parseSpec (← input.getObjVal? "spec")
  let decoded ←
    AuthenticatedSource.decodeIndexedSource (← input.getObjVal? "source")
        (← input.getObjValAs? String "sourceText") spec.entry
  let bundle := LowerSplit.sourceBundle decoded.nodes
  let seq ← IndexedSourceSchedule.checkSequence decoded bundle
  let plan := mkPlan bundle spec
  expect (decide (Valid bundle spec plan)) "invalid authored plan"
  expect (plan.mods.all fun m => safeModulePath m.name) "unsafe path in authored plan"
  expect
      (decide
        (IndexedInputChecks.FootprintChecks decoded.source.body bundle ∧
          IndexedInputChecks.DefinitionChecks decoded.source.body bundle))
      "source and bundle footprints differ"
  expect
      (decide
        (seq.units.map IndexedSourceSchedule.SourceUnit.ordinal = [0, 1, 2, 3, 4, 5] ∧
          plan.execOrder = [1, 0, 2, 3, 4, 5]))
      "lost source ordinals (including publication)"
  let candidate := splitProgramIndexed decoded.source.body bundle plan
  expect
      (decide
        (plan.evalOrder = ["scanner.mjs", "entry.mjs"] ∧
          candidate.evalOrder = spec.order ++ [spec.entry]))
      "evaluation order differs from authored order"
  expect
      (decide
        (PlanPlacement.assignments plan =
          [(0, "entry.mjs"), (2, "entry.mjs"), (3, "entry.mjs"), (4, "entry.mjs"), (5, "entry.mjs"),
            (1, "scanner.mjs")]))
      "unexpected ownership in the authored plan"
  expect (decide (candidate.regions = decoded.program.regions)) "allocation regions differ"
  let _ ← ConstructedOutput.checkConstructedOutput (← input.getObjVal? "files") candidate
  let pair ← checkProbePair decoded.program candidate
  let _ ← IndexedHoistedSwap.checkBehavior decoded.program candidate
  let sourceCells ← audit pair.before "entry.mjs"
  let candidateCells ← audit pair.after "scanner.mjs"
  let wrong ← ConstructedOutput.decodeConstructedOutput (← input.getObjVal? "wrongFiles") candidate
  let wrongPair ← checkProbePair decoded.program wrong
  let wrongCapture ← nameAt wrongPair.after.linked "scanner.mjs" "isNewLine"
  let scan ← nameAt wrongPair.after.linked "scanner.mjs" "nextLineBreak"
  let wrongClosure ← closureAt wrongPair.after.ready scan
  expect (decide (wrongCapture = ⟨5, 0⟩ ∧ wrongClosure.vars.lookup "isNewLine" = some wrongCapture))
      "wrong-cell control did not capture getLineInfo"
  let wrongReason ←
    match IndexedHoistedSwap.checkBehavior decoded.program wrong with
    | .error reason =>
      pure reason
    | .ok _ =>
      throw "accepted the wrong-cell control"
  let guardReason ←
    match IndexedSourceSchedule.check decoded bundle spec with
    | .error reason =>
      pure reason
    | .ok _ =>
      throw "strict source order unexpectedly accepted the swap"
  let eager ← checkEager input candidate
  return Json.mkObj
      [("eager", eager), ("physicalOrder", toJson candidate.evalOrder),
        ("sourceRows", toJson (decoded.source.body.map Prod.fst)),
        ("authoredRows", toJson plan.execOrder),
        ("executableRows", toJson ((unitsOf pair.after.linked).map LinkedUnit.id)),
        ("sourceCells", sourceCells), ("candidateCells", candidateCells),
        ("wrongCapture", addrJson wrongCapture), ("wrongReason", toJson wrongReason),
        ("guardReason", toJson guardReason)]

def main : IO UInt32 := do
  let line ← (← IO.getStdin).getLine
  match Json.parse line >>= check with
  | .ok output =>
    IO.println output.compress
    return 0
  | .error reason =>
    IO.eprintln reason
    return 1
