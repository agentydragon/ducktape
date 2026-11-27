import EsmSplit.LowerSplit.Input
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.SyntaxMatch
import SplitDeclaratorSchedule

open Lean EsmSplit
open EsmSplit.LowerSplit EsmSplit.SplitFiles

private structure PrefixInput where
  declarations : CoreJS.Program
  retainedOrdinal : Nat
  retainedOperation : CoreJS.Stmt

/-- Drop only an explicitly retained, non-binding final operation, preserving
the full module graph and the export tables. A retained unit anywhere before
the cut, or one that would change allocation, cannot form a prefix here. -/
private def declarationPrefix (program : CoreJS.Program) (cut : Nat) : Except String PrefixInput := do
  let scheduled := program.loaded.flatMap (·.body)
  let [(ordinal, operation)] := scheduled.drop cut | throw "expected one retained final operation"
  unless ordinal == cut do throw "the retained operation moved across the declaration prefix"
  match operation with
  | .opaque id [] [] _ .world =>
    unless id == cut do throw "the retained operation does not carry its original identity"
  | _ => throw "the retained operation must not declare bindings"
  for (id, stmt) in scheduled.take cut do
    unless id < cut do throw "a non-prefix unit appears among the declarations"
    match stmt with
    | .decl .const _ _ => pure ()
    | _ => throw "the prefix contains a non-const declaration"
  let declarations := { program with modules := program.modules.map fun m =>
    { m with body := m.body.filter (fun row => row.1 < cut) } }
  return ⟨declarations, ordinal, operation⟩

private def check (sourceName : String) (source outputRoot : System.FilePath)
    (request : Spec) (work : System.FilePath) : IO (PrefixInput × PrefixInput) := do
  let original ← readBundle source work true false
  let nodes ← IO.ofExcept (ESTreeUnits.checkedNodes original.program original.source)
  let cut := (nodes.toList.takeWhile fun n => Lower.typ n == "VariableDeclaration").length
  unless nodes.size == cut + 2 && nodes[cut]?.any (fun n => Lower.typ n == "ExpressionStatement") do
    throw (IO.userError "expected a declaration prefix, one retained operation, and terminal exports")
  let some exports := nodes[cut + 1]? | throw (IO.userError "missing terminal exports")
  unless Lower.typ exports == "ExportNamedDeclaration" && Lower.isNull (Lower.fld exports "declaration") do
    throw (IO.userError "expected a standalone terminal export row")
  let paths := request.entry :: request.order
  unless paths.Nodup && paths.Perm (request.modules.map Prod.fst) && !request.sequential do
    throw (IO.userError "expected complete import priority, not an imposed evaluation order")
  -- Normalizing the ordering of these disjoint owner records does not change
  -- the submitted selectors or import priority.
  let modules ← paths.mapM fun name => do
    let some owner := request.modules.find? (·.1 == name) |
      throw (IO.userError "missing placement owner")
    pure owner
  let ids ← modules.flatMap Prod.snd |>.mapM fun
    | .stmt i => pure i
    | .name _ => throw (IO.userError "expected source ordinals in the translated plan")
  unless ids.Perm (List.range nodes.size) do
    throw (IO.userError "the translated plan must claim every source ordinal once")
  let plan := mkPlan (sourceBundle nodes) { request with modules }
  let files ← readModules outputRoot work paths.toArray
  let decoded ← IO.ofExcept <| (CoreJS.LinkedInput.decodeStructuralSingleton
    original.program (Json.mkObj []) files plan (List.range cut)).mapError
    CoreJS.LinkedBehavior.StructuralRefusal.reason
  let _ ← IO.ofExcept (CoreJS.checkPlaces decoded.assignment decoded.before decoded.after)
  let [sourceModule] := decoded.before.modules | throw (IO.userError "expected an import-free source")
  let originalGraph : CoreJS.Program :=
    { entry := sourceName, modules := [{ sourceModule with name := sourceName }] }
  return (← IO.ofExcept (declarationPrefix originalGraph cut),
    ← IO.ofExcept (declarationPrefix decoded.after cut))

/-- This is a regression binding the parsed and reread inputs to the existing
fixture theorem, not a CLI rule for admitting a new shape. -/
private def useProof (before after : CoreJS.Program) : Except String
    (PLift ((∀ w fuel, CoreJS.runProgram w fuel before = CoreJS.runProgram w fuel after) ∧
      CoreJS.LinkedBehavior.SameExports before after)) := do
  let some ⟨original⟩ := CoreJS.SyntaxMatch.programEq before CoreJS.SplitDeclaratorSchedule.originalPrefix |
    throw "the parsed source does not match the declaration proof inputs"
  let some ⟨reread⟩ := CoreJS.SyntaxMatch.programEq after CoreJS.SplitDeclaratorSchedule.splitPrefix |
    throw "the reread output does not match the declaration proof inputs"
  return ⟨by
    rw [original, reread]
    exact ⟨CoreJS.SplitDeclaratorSchedule.prefix_outcomes, fun w =>
      ⟨_, _, (CoreJS.SplitDeclaratorSchedule.terminal_exports w).1,
        (CoreJS.SplitDeclaratorSchedule.terminal_exports w).2, List.Perm.refl _⟩⟩⟩

def main (args : List String) : IO UInt32 := do
  try
    let [sourceName, sourceFile, requestFile, output] := args |
      throw (IO.userError "expected SOURCE_ROUTE ORIGINAL_JS AUTHORED_PLAN_JSON STAGED_OUTPUT")
    let request ← loadAuthoredSpec requestFile
    IO.FS.withTempDir fun work => do
      let (before, after) ← check sourceName sourceFile output request work
      let _ ← IO.ofExcept (useProof before.declarations after.declarations)
      let sameOperation := CoreJS.SyntaxMatch.stmtEq before.retainedOperation after.retainedOperation
      unless sameOperation.isSome && before.retainedOrdinal == after.retainedOrdinal do
        throw (IO.userError "the retained operation differs on readback")
      IO.println s!"declaration-prefix checked; semantics of retained unit {before.retainedOrdinal} unresolved"
    return 0
  catch error =>
    IO.eprintln error.toString
    return 1
