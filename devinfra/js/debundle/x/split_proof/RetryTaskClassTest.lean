import EsmSplit.CoreJS.DeferredClassCreation
import EsmSplit.ESTreeEmit
import EsmSplit.ESTreeUnits
import EsmSplit.LowerSplit.Input
import EsmSplit.SplitFiles

open Lean CoreJS EsmSplit SplitFiles

private def sourceSlice (text : String) (node : Json) : Except String String := do
  -- The authenticated fixture is ASCII. Acorn offsets also count characters here.
  unless text.length == text.utf8ByteSize do throw "expected ASCII class fixture"
  let start ← node.getObjValAs? Nat "start"
  let stop ← node.getObjValAs? Nat "end"
  return ((text.drop start).take (stop - start)).toString

private def plan : EsmSplit.Plan :=
  ⟨"entry.mjs", [⟨"entry.mjs", [1], [("task.mjs", ["RetryTask"])], []⟩,
    ⟨"task.mjs", [0], [], [("RetryTask", "RetryTask")]⟩]⟩

-- Identical code and export cell, different lexical input to the stored field.
private def checkFieldCapture : Except String Unit := do
  let stmt := Stmt.decl .let_ "C" (.cls "C" true (Func.mk "C" .classCtor false [] [])
    [.field false (.str "field") (.var "captured")])
  let unit (capture : Addr) : LinkedUnit :=
    (0, stmt, { vars := [("C", ⟨1, 0⟩), ("captured", capture)], this := .undef })
  let left := [unit ⟨2, 0⟩]
  unless (LinkedBehavior.sameUnits? left left).isSome do throw "same field capture refused"
  if (LinkedBehavior.sameUnits? left [unit ⟨3, 0⟩]).isSome then throw "different field capture accepted"

private def observe (checked : DeferredClassCreation.Checked source files plan 0 32) :
    Except String Unit := do
  let anchor : Addr := ⟨1, 0⟩
  let .ok () state := runProgram World.probe 32 checked.after | throw "class creation refused"
  let some (.obj cls) := (state.cellAt anchor).bind (·.value) | throw "missing class binding"
  let some (.closure _ closed fields) := (state.objAt cls).bind (·.code) |
    throw "the result is not a class constructor"
  unless closed.vars.isEmpty do throw "RetryTask should have no outer lexical captures"
  unless fields.map Prod.fst == (["id", "fn", "error", "timestamp", "lastAttempt", "resolve", "reject", "signal"] : List PropertyKey) do
    throw "instance fields were not retained in source order"
  let .unknown "ambient binding Math is reached" stopped := construct 32 (.obj cls) [] { state with region := 1 } |
    throw "expected the first instance initializer to reach its ambient lookup"
  unless stopped.world.handed.isEmpty do throw "unexpected host handle"

def main (args : List String) : IO Unit := do
  IO.ofExcept checkFieldCapture
  let [full, projected, out, mode] := args |
    throw (IO.userError "expected original library, projected class, staging directory and mode")
  IO.FS.withTempDir fun work => do
    let fullAst ← loadProgram full work true
    let fullText ← IO.FS.readFile full
    let fullNodes ← IO.ofExcept (ESTreeUnits.checkedNodes fullAst fullText)
    let some original := fullNodes[7]? | throw (IO.userError "missing original class ordinal")
    let ast ← loadProgram projected work true
    let text ← IO.FS.readFile projected
    let nodes ← IO.ofExcept (ESTreeUnits.checkedNodes ast text)
    let #[cls, _] := nodes | throw (IO.userError "expected one class and its export")
    let raw ← IO.ofExcept (sourceSlice text cls)
    let originalRaw ← IO.ofExcept (sourceSlice fullText original)
    if mode != "control" && raw != originalRaw then
      throw (IO.userError "projected class bytes differ from ordinal 7")
    let programs ← IO.ofExcept (ESTreeEmit.programs nodes (LowerSplit.sourceBundle nodes) plan text)
    let part : OutputPart := ⟨programs, text, projected, work⟩
    if mode != "check" then
      IO.FS.createDir out
      let manifest ← writeProgramManifest programs work
      javascript #["--print"] #[manifest, out, projected]
    let readback ← certifyOutput [part] (Json.mkObj []) out work true false (fun files => pure (.ok files))
    let .ok files := readback | throw (IO.userError "artifact readback refused")
    let checked ← IO.ofExcept (DeferredClassCreation.check ast files plan 0 32)
    IO.ofExcept (observe checked)
