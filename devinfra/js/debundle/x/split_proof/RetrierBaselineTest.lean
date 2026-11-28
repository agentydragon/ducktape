import EsmSplit.Cli
import EsmSplit.CoreJS.AuthenticatedSource
import EsmSplit.CoreJS.FromESTree
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.ESTreeEmit
import EsmSplit.ESTreeUnits
import EsmSplit.LowerSplit.Input
import EsmSplit.Split
import EsmSplit.SplitFiles

open Lean EsmSplit SplitFiles

private def refusal {α : Type} (result : Except String α) : Option String :=
  match result with
  | .ok _ => none
  | .error reason => some reason

/-- Print and reparse an authored layout for the Node comparison. Only the
ordinary CLI can issue a publication verdict. -/
def main (args : List String) : IO Unit := do
  let [input, specPath, stagedPath] := args | throw (IO.userError "expected source, spec, and scratch stage")
  let staged : System.FilePath := stagedPath
  if ← staged.pathExists then throw (IO.userError "scratch stage already exists")
  IO.FS.withTempDir fun work => do
    let source ← IO.FS.readFile input
    let ast ← loadProgram input work true
    let nodes ← IO.ofExcept (ESTreeUnits.checkedNodes ast source)
    let spec ← IO.ofExcept (parseAuthoredSpec (← IO.FS.readFile specPath))
    let bundle := LowerSplit.sourceBundle nodes
    let plan := mkPlan bundle spec
    unless plan.execOrder == List.range nodes.size do throw (IO.userError "expected the complete source order")
    let programs ← IO.ofExcept (ESTreeEmit.programs nodes bundle plan source spec.entry)
    let manifest ← writeProgramManifest programs work
    IO.FS.createDir staged
    javascript #["--print"] #[manifest, staged, input]
    let part : OutputPart := ⟨programs, source, input, work⟩
    let .ok files ← certifyOutput [part] (Json.mkObj []) staged work true false (fun files => pure (.ok files)) |
      throw (IO.userError "scratch ESTree recheck refused")
    let units := nodes.toList.zipIdx.filterMap fun (node, ordinal) => do
      -- This deliberately checks only direct executable lowering. The linked
      -- classifier can retain deferred function bodies and handles export rows.
      guard ((node.getObjValAs? String "type").toOption != some "ExportNamedDeclaration")
      (refusal (CoreJS.FromESTree.linkedUnit [] node)).map fun reason => (ordinal, reason)
    let classification := CoreJS.AuthenticatedSource.decodeSourceClassification ast source spec.entry
    let indexed := classification >>= CoreJS.AuthenticatedSource.checkIndexedSource
    let linked := CoreJS.LinkedInput.decodePair ast (Json.mkObj []) files plan
    IO.println (Json.mkObj [
      ("order", toJson plan.execOrder), ("classification", toJson (refusal classification)),
      ("indexed", toJson (refusal indexed)), ("linked", toJson (refusal linked)),
      ("directUnitRefusals", toJson units)]).compress
