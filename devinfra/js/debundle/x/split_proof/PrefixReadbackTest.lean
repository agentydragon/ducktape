import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.LinkedSuffix
import EsmSplit.CoreJS.SyntaxMatch
import EsmSplit.LowerSplit.Input
import SplitDeclaratorSchedule

open Lean EsmSplit
open EsmSplit.LowerSplit EsmSplit.SplitFiles

private def check (sourceName : String) (source outputRoot : System.FilePath) (request : Spec)
    (work : System.FilePath) : IO (CoreJS.Program × CoreJS.Program) := do
  let original ← readBundle source work true false
  let nodes ← IO.ofExcept (ESTreeUnits.checkedNodes original.program original.source)
  let cut := (nodes.toList.takeWhile fun n => Lower.typ n == "VariableDeclaration").length
  unless nodes.size == cut + 2 && nodes[cut]?.any (fun n => Lower.typ n == "ExpressionStatement") do
    throw
        (IO.userError "expected a declaration prefix, one retained operation, and terminal exports")
  let some exports := nodes[cut + 1]? | throw (IO.userError "missing terminal exports")
  unless
    Lower.typ exports == "ExportNamedDeclaration" &&
      Lower.isNull (Lower.fld exports "declaration") do
    throw (IO.userError "expected a standalone terminal export row")
  let paths := request.entry :: request.order
  unless paths.Nodup && paths.Perm (request.modules.map Prod.fst) && !request.sequential do
    throw (IO.userError "expected complete import priority, not an imposed evaluation order")
  -- Normalizing the ordering of these disjoint owner records does not change
  -- the submitted selectors or import priority.
  let modules ←
    paths.mapM fun name => do
        let some owner :=
          request.modules.find? (·.1 == name) | throw (IO.userError "missing placement owner")
        pure owner
  let ids ←
    modules.flatMap Prod.snd |>.mapM fun
        | .stmt i => pure i
        | .name _ => throw (IO.userError "expected source ordinals in the translated plan")
  unless ids.Perm (List.range nodes.size) do
    throw (IO.userError "the translated plan must claim every source ordinal once")
  let plan := mkPlan (sourceBundle nodes) { request with modules }
  let files ← readModules outputRoot work paths.toArray
  let decoded ←
    IO.ofExcept <|
        (CoreJS.LinkedInput.decodeStructuralSingleton original.program (Json.mkObj []) files plan
              (List.range cut)).mapError
          CoreJS.LinkedBehavior.StructuralRefusal.reason
  let _ ← IO.ofExcept (CoreJS.checkPlaces decoded.assignment decoded.before decoded.after)
  let [sourceModule] :=
    decoded.before.modules | throw (IO.userError "expected an import-free source")
  let originalGraph : CoreJS.Program :=
    { entry := sourceName, modules := [{ sourceModule with name := sourceName }] }
  return (originalGraph, decoded.after)

/-- Bind the full parsed and reread programs to the theorem. Structural
readback checks the retained syntax and glue before this modeled proof. -/
private def useProof (before after : CoreJS.Program) :
    Except String
      (PLift
        ((∀ w fuel, CoreJS.runProgram w fuel before = CoreJS.runProgram w fuel after) ∧
          CoreJS.LinkedBehavior.SameExports before after)) :=
  do
  let some ⟨original⟩ :=
    CoreJS.SyntaxMatch.programEq before
      CoreJS.SplitDeclaratorSchedule.originalWithTail | throw "the parsed source does not match the retained-operation proof input"
  let some ⟨reread⟩ :=
    CoreJS.SyntaxMatch.programEq after
      CoreJS.SplitDeclaratorSchedule.splitWithTail | throw "the reread output does not match the retained-operation proof input"
  return ⟨by
      rw [original, reread]
      exact
        ⟨CoreJS.SplitDeclaratorSchedule.retained_outcomes,
          CoreJS.SplitDeclaratorSchedule.retained_same_exports⟩⟩

private def checkTail (before after : CoreJS.Program) : Except String Unit := do
  let w := CoreJS.World.probe
  let .ok left _ :=
    CoreJS.link before
      (CoreJS.initialState w before.regions) | throw "cannot link the original readback input"
  let .ok right _ :=
    CoreJS.link after
      (CoreJS.initialState w after.regions) | throw "cannot link the emitted readback input"
  let beforeUnits := CoreJS.unitsOf left
  let afterUnits := CoreJS.unitsOf right
  let cut := 4
  unless (CoreJS.LinkedSuffix.check cut beforeUnits afterUnits).isSome do
    throw "retained tail does not have the same linked captures"
  let [(id, stmt, env)] := afterUnits.drop cut | throw "expected the saved one-unit tail"
  let some wrong := env.vars.lookup "z" | throw "negative control requires the real z cell"
  let tampered := afterUnits.take cut ++ [(id, stmt, { env with vars := ("c", wrong) :: env.vars })]
  unless (CoreJS.LinkedSuffix.check cut beforeUnits tampered).isNone do
    throw "suffix checker accepted a retargeted live cell"

def main (args : List String) : IO UInt32 := do
  try
    let [sourceName, sourceFile, requestFile, output] :=
      args | throw (IO.userError "expected SOURCE_ROUTE ORIGINAL_JS AUTHORED_PLAN_JSON STAGED_OUTPUT")
    let request ← loadAuthoredSpec requestFile
    IO.FS.withTempDir fun work => do
        let (before, after) ← check sourceName sourceFile output request work
        let _ ← IO.ofExcept (useProof before after)
        IO.ofExcept (checkTail before after)
        IO.println "modeled prefix and retained suffix checked; same live export cells"
    return 0
  catch error =>
    IO.eprintln error.toString
    return 1
