import EsmSplit.Cli
import EsmSplit.CoreJS.AuthenticatedSource
import EsmSplit.CoreJS.IndexedInputChecks
import EsmSplit.CoreJS.IndexedReorder
import EsmSplit.CoreJS.IndexedSourceSchedule
import EsmSplit.CoreJS.LinkWorld
import EsmSplit.LowerSplit.Input

open CoreJS CoreJS.LinkedBehavior Lean

private def cell (linked : List (CoreJS.Module × Env)) (path name : String) : Except String Addr :=
  do
  let some (_, env) := linked.find? (fun (m, _) => m.name == path) | throw s!"missing module {path}"
  let some address := env.vars.lookup name | throw s!"{path}: missing cell {name}"
  return address

private def checkResult (program : Program) : Except String Unit := do
  let some [("result", address)] :=
    LinkedBehavior.exports World.probe program | throw "expected one public result cell"
  let .ok () state := runProgram World.probe 32 program | throw "expected normal completion"
  let some { value := some (.num result), .. } := state.cellAt address | throw "expected a number"
  unless result == 7 do
    throw "expected result 7"

private def check (input : Json) : Except String Json := do
  let spec ← input.getObjValAs? String "spec" >>= EsmSplit.parseAuthoredSpec
  let ast ← input.getObjVal? "ast"
  let decoded ←
    AuthenticatedSource.decodeIndexedSource ast (← ast.getObjValAs? String "sourceText") spec.entry
  let B := EsmSplit.LowerSplit.sourceBundle decoded.nodes
  let schedule ← IndexedSourceSchedule.check decoded B spec
  let P := schedule.plan
  let source := decoded.source
  let original := decoded.program
  let output := splitProgramIndexed source.body B P
  let .isTrue imports :=
    (inferInstance : Decidable (source.imports = [])) | throw "expected no source imports"
  let .isTrue sameEntry :=
    (inferInstance : Decidable (original.entry = source.name)) | throw "wrong source entry"
  let found : original.find original.entry = some source := by
    change decoded.program.find decoded.program.entry = some decoded.source
    have entryName : decoded.program.entry = decoded.source.name := sameEntry
    simp [Program.find, decoded.singleton, entryName]
  let .isTrue ordered :=
    (inferInstance :
      Decidable
        (source.body.Pairwise fun a b => a.1 ≤ b.1)) | throw "unexpected original body order"
  let kept : SparseProjectedOrder.OrderKept source.body P := by
    change SparseProjectedOrder.OrderKept decoded.source.body schedule.plan
    rw [← schedule.sequence.sourceEq]
    exact schedule.executableOrder
  let .isTrue fp :=
    (inferInstance :
      Decidable
        (IndexedInputChecks.FootprintChecks source.body B)) | throw "unexpected source footprints"
  let .isTrue defs :=
    (inferInstance :
      Decidable
        (IndexedInputChecks.DefinitionChecks source.body B)) | throw "missing source definitions"
  let ⟨entry, ⟨planEntry⟩⟩ ←
    (show Except String (Σ m, PLift (P.find P.entry = some m)) from
        match P.find P.entry with
        | none => .error "missing plan entry"
        | some m => .ok ⟨m, ⟨rfl⟩⟩)
  let .isTrue owned :=
    (inferInstance :
      Decidable
        (IndexedReorder.InlineExportsOwned source B entry)) | throw "wrong inline export owner"
  let .isTrue table :=
    (inferInstance :
      Decidable
        ((moduleOfIndexed source.body B P entry).exports =
          source.exports)) | throw "changed inline export table"
  let before ← checkLink original
  have fp' := IndexedInputChecks.checked_footprints fp
  have :=
    IndexedReorder.inline_links_of_order_kept schedule.valid decoded.singleton imports found fp'.1
      fp'.2 (IndexedInputChecks.checked_definitions defs) ordered kept planEntry owned table before
  let after ← checkLink output
  let helper ← cell before.linked spec.entry "read"
  let capture ← cell before.linked spec.entry "seed"
  unless
    (← cell after.linked "helper.mjs" "read") == helper &&
      (← cell after.linked output.entry "read") == helper &&
      (← cell after.linked "helper.mjs" "seed") == capture do
    throw "the authored owner, caller import or captured declaration lost its original address"
  let some { value := some (.obj _), .. } :=
    after.ready.cellAt helper | throw "helper was not hoisted"
  unless exportsMatchFrom before after do
    throw "public live cells changed"
  -- Retarget just the public name to the captured cell, whose value is also 7.
  let wrong :=
    { output with
      modules :=
        output.modules.map fun m =>
          if m.name == "helper.mjs" then { m with exports := m.exports ++ [("seed", "seed")] }
          else
            if m.name == output.entry then
              { m with
                imports := m.imports ++ [⟨"helper.mjs", [("seed", "seed")]⟩],
                exports := [("result", "seed")] }
            else m }
  let wrongLink ← checkLink wrong
  if exportsMatchFrom before wrongLink then
    throw "a same-value replacement cell passed the export check"
  for program in [original, output, wrong] do
    checkResult program
  return toJson (output.fileUnitPlacements, output.evalOrder)

def main : IO UInt32 := do
  match Json.parse (← (← IO.getStdin).getLine) >>= check with
  | .ok result =>
    IO.println result.compress;
    return 0
  | .error reason =>
    IO.eprintln reason;
    return 1
