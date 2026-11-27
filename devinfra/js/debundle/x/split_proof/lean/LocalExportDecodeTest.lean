import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.TerminalConstructorInput
import EsmSplit.CoreJS.TerminalConstructor
import EsmSplit.CoreJS.TerminalFootprints
import EsmSplit.CoreJS.IndexedSourceSchedule
import EsmSplit.CoreJS.IndexedCheckedSplit
import EsmSplit.LowerSplit.Input
import EsmSplit.Cli

open Lean (Json)
open CoreJS

private def onlyModule (p : Program) : IO Module := do
  let [m] := p.modules | throw (IO.userError "expected exactly one source module")
  unless m.name == p.entry do throw (IO.userError "source entry changed")
  return m

private def decode (j : Json) : IO (Program × List EsmSplit.LocalExportUnit) :=
  IO.ofExcept (LinkedInput.decodeSingletonSourceWithLocalExports j "entry.mjs")

private def decodeIndexed (j : Json) : IO LinkedInput.IndexedSource :=
  IO.ofExcept do
    let sourceText ← j.getObjValAs? String "sourceText"
    LinkedInput.decodeIndexedSource j sourceText "entry.mjs"

private def require (ok : Bool) (why : String) : IO Unit :=
  unless ok do throw (IO.userError why)

private def checkGraph (j : Json) (specText sourceText : String) : IO Unit := do
  let decoded ← IO.ofExcept (LinkedInput.decodeIndexedSource j sourceText "entry.mjs")
  let p := decoded.program
  let rows := decoded.localExports
  let m ← onlyModule p
  let [row] := rows | throw (IO.userError "expected one standalone export list")
  let pairs := (List.range 16).map fun i => (s!"reader{i}", s!"reader{i}")
  require (row.ordinal == 32 && row.names == pairs) "lost #32 or its ordered reader exports"
  require (m.body.map Prod.fst == List.range 32) "lost original executable identities"
  require (m.exports == pairs) "module exports differ from the original export list"
  let checked ← IO.ofExcept (TerminalConstructorInput.check (p, rows))
  let _ := TerminalConstructorInput.all_const checked
  let _ := TerminalConstructorInput.source_shape checked
  let bundle := EsmSplit.LowerSplit.sourceBundle decoded.nodes
  let spec ← IO.ofExcept (EsmSplit.parseAuthoredSpec specText)
  let plan := EsmSplit.mkPlan bundle spec
  let statements := checked.terminal.source.body.map Prod.snd
  unless (decide (MetadataCells.Footprints statements bundle)) do
    throw (IO.userError "actual source bundle fails executable/metadata footprints")
  unless (decide (checked.terminal.row.FootprintMatches (bundle.stmt checked.terminal.row.ordinal))) do
    throw (IO.userError "actual original export row fails bundle footprint matching")
  unless (decide (MetadataCells.RowLocalsDefined statements bundle checked.terminal.row)) do
    throw (IO.userError "actual export row has no executable source declaration")
  unless (decide (ProjectedOrder.Kept statements plan)) do
    throw (IO.userError "actual plan changes executable order")
  unless (decide (EsmSplit.Valid bundle spec plan)) do
    throw (IO.userError "actual authored plan fails validity")
  let some entry := plan.find plan.entry | throw (IO.userError "actual plan has no entry")
  require (plan.evalOrder.contains plan.entry) "actual entry is not loaded"
  require (entry.stmts == [checked.terminal.row.ordinal]) "actual entry does not contain exactly the export row"
  require entry.exports.isEmpty "actual entry has generated glue exports"
  let built ← IO.ofExcept (TerminalConstructor.construct (p, rows) bundle spec)
  let _ := TerminalConstructor.sound built
  match spec.modules with
  | (owner, selector :: rest) :: tail =>
    let repeated := { spec with modules := (owner, selector :: selector :: rest) :: tail }
    match TerminalConstructor.construct (p, rows) bundle repeated with
    | .error "an original unit is selected more than once" => pure ()
    | _ => throw (IO.userError "duplicate source selector was not refused")
  | _ => throw (IO.userError "fixture has no source selector")
  let overlapping := { spec with modules := spec.modules.map fun (owner, selectors) =>
    (owner, if owner == "provider0.mjs" then .name "data0" :: selectors else selectors) }
  match TerminalConstructor.construct (p, rows) bundle overlapping with
  | .error "an original unit is selected more than once" => pure ()
  | _ => throw (IO.userError "overlapping name and ordinal selectors were not refused")
  match TerminalConstructor.construct (p, rows) (bundle ++ [default]) spec with
  | .error "terminal export row must be the final original source unit" => pure ()
  | _ => throw (IO.userError "extra omitted source unit was not refused")
  let badImport : Program := { p with modules := [{ m with imports := [⟨"provider.mjs", []⟩] }] }
  match TerminalConstructorInput.check (badImport, rows) with
  | .error "terminal source must not import modules" => pure ()
  | _ => throw (IO.userError "a directly supplied source with an import was not refused")
  let badBody := m.body.map fun (id, stmt) =>
    if id == 0 then (id, .decl .let_ "data0" (.str "value0")) else (id, stmt)
  let badDeclaration : Program := { p with modules := [{ m with body := badBody }] }
  match TerminalConstructorInput.check (badDeclaration, rows) with
  | .error "expected unique const declarations with closed primitive initializers" => pure ()
  | _ => throw (IO.userError "a non-const original statement was not refused")

private def checkOmitted (j : Json) (specText sourceText : String) : IO Unit := do
  match LinkedInput.decodeIndexedSource j sourceText "entry.mjs" with
  | .error "unsupported omitted source node 33" => pure ()
  | _ => throw (IO.userError "the source decoder did not refuse the omitted debugger")
  let input ← decode j
  let nodes ← IO.ofExcept (ESTreeUnits.checkedNodes j sourceText)
  let spec ← IO.ofExcept (EsmSplit.parseAuthoredSpec specText)
  match TerminalConstructor.construct input (EsmSplit.LowerSplit.sourceBundle nodes) spec with
  | .error "terminal export row must be the final original source unit" => pure ()
  | _ => throw (IO.userError "a real omitted debugger statement was not refused")

private def checkSparse (j : Json) (specText : String) : IO Unit := do
  let decoded ← decodeIndexed j
  let p := decoded.program
  let rows := decoded.localExports
  let m ← onlyModule p
  let [row] := rows | throw (IO.userError "expected one interior export list")
  require (row.ordinal == 1 && row.names == [("exposed", "seed")]) "interior export or alias changed"
  require (m.body.map Prod.fst == [0, 2]) "interior export shifted executable identities"
  require (m.exports == [("exposed", "seed")]) "interior public binding changed"
  let bundle := EsmSplit.LowerSplit.sourceBundle decoded.nodes
  let request ← IO.ofExcept (EsmSplit.parseAuthoredSpec specText)
  let evidence ← IO.ofExcept (IndexedSourceSchedule.check decoded bundle request)
  let _sourceFacts := IndexedSourceSchedule.sourceInductionFacts evidence.sequence
  require (evidence.sequence.units.map IndexedSourceSchedule.SourceUnit.ordinal == [0, 1, 2])
    "complete source schedule dropped or shifted an original ordinal"
  require (evidence.sequence.units.map IndexedSourceSchedule.SourceUnit.nodeEncoding ==
      decoded.nodes.toList.map Lean.Json.compress)
    "source classification lost or changed an original AST node"
  require (evidence.sequence.units.filterMap IndexedSourceSchedule.SourceUnit.executable? == [0, 2])
    "executable projection differs from the original source IDs"
  require (evidence.sequence.units.filterMap IndexedSourceSchedule.SourceUnit.localExport? ==
      [(1, row.names)])
    "metadata projection differs from the decoder's ordered export row"
  require (evidence.plan.execOrder == [0, 1, 2])
    "actual authored planner does not preserve the source-unit schedule"
  let constructed := splitProgramIndexed m.body bundle evidence.plan
  let constructedRows : List (Nat × Stmt) := constructed.loaded.flatMap (·.body)
  match constructedRows with
  | [(0, .decl .const "seed" (.num 42)), (2, .decl .const "result" (.var "seed"))] => pure ()
  | _ => throw (IO.userError "actual indexed constructor changed a statement or its original identity")
  -- The plan's placement list follows authored module order; the loaded body
  -- checked above follows execution order. Both preserve the original IDs.
  require (constructed.fileUnitPlacements == [(2, "entry.mjs"), (0, "worker.mjs")])
    "actual indexed placements differ from the existing authored owners"
  require ((1, "entry.mjs") ∈ CoreJS.PlanPlacement.assignments evidence.plan)
    "actual planner did not retain the metadata ordinal's authored owner"
  require (request.claimants bundle 1 == ["entry.mjs"])
    "metadata owner differs from the parsed authored request"
  let built ← IO.ofExcept (IndexedCheckedSplit.construct p bundle request rows .allOutcomes)
  let _samePlan : built.plan = evidence.plan := built.planEq.trans evidence.planEq.symm
  let _allWorldsAndFuels : ∀ world fuel,
      runProgram world fuel p = runProgram world fuel built.program := built.allOutcomes
  let _publicNamesAndLiveCells : LinkedBehavior.SameExports p built.program := built.sameExports
  let _authoredOwners : ∀ id owner, (id, owner) ∈ built.assignment →
      request.claimants bundle id = [owner] := built.authored
  let _constructorSound := IndexedCheckedSplit.construct_sound built

private def checkAliases (j : Json) : IO Unit := do
  let decoded ← decodeIndexed j
  let m ← onlyModule decoded.program
  let [aliases, empty] := decoded.localExports | throw (IO.userError "expected alias and empty export lists")
  let pairs := [("publicName", "seed"), ("with-dash", "seed")]
  require (aliases.ordinal == 1 && aliases.names == pairs) "ordered public/local aliases changed"
  require (empty.ordinal == 2 && empty.names.isEmpty) "empty export list was omitted"
  require (m.body.map Prod.fst == [0, 3]) "export metadata became executable"
  require (m.exports == pairs) "alias export table changed"
  let bundle := EsmSplit.LowerSplit.sourceBundle decoded.nodes
  let sequence ← IO.ofExcept (IndexedSourceSchedule.checkSequence decoded bundle)
  let _sourceFacts := IndexedSourceSchedule.sourceInductionFacts sequence
  require (sequence.units.map IndexedSourceSchedule.SourceUnit.ordinal == [0, 1, 2, 3])
    "source classification changed ordinals around an empty export row"
  require (sequence.units.map IndexedSourceSchedule.SourceUnit.nodeEncoding ==
      decoded.nodes.toList.map Lean.Json.compress)
    "multi-row classification lost or changed an original AST node"
  require (sequence.units.filterMap IndexedSourceSchedule.SourceUnit.executable? ==
      m.body.map Prod.fst)
    "multi-row classification changed executable source ordinals"
  require (sequence.units.filterMap IndexedSourceSchedule.SourceUnit.localExport? ==
      [(1, aliases.names), (2, [])])
    "an empty metadata row was not retained by the complete source sequence"
  let classifiedRows := sequence.units.filterMap IndexedSourceSchedule.SourceUnit.localExportRow?
  require (classifiedRows.map (·.ordinal) == decoded.localExports.map (·.ordinal) &&
      classifiedRows.map (·.names) == decoded.localExports.map (·.names))
    "source induction changed metadata row order, aliases, or empty rows"
  require (decoded.localExports.map (·.ordinal) == [1, 2])
    "source induction did not establish the source order of metadata row ordinals"
  let _allOriginalOrdinals :
      ((sequence.source.body.map Prod.fst) ++ decoded.localExports.map (·.ordinal)).Perm
        (List.range bundle.length) := sequence.sourceCoverage
  -- Removing the empty row leaves the public aliases above unchanged.
  match LinkedInput.checkSourceUnitCoverage decoded.nodes.size decoded.program [aliases] with
  | .error "unsupported omitted source node 2" => pure ()
  | _ => throw (IO.userError "dropping an empty row passed original ordinal coverage")

private def checkInline (j : Json) : IO Unit := do
  let (p, rows) ← decode j
  let m ← onlyModule p
  require rows.isEmpty "inline export became standalone metadata"
  require (m.body.map Prod.fst == [0] && m.exports == [("declared", "declared")])
    "inline export changed"

private def checkDefault (j : Json) : IO Unit := do
  let (p, rows) ← decode j
  let m ← onlyModule p
  require rows.isEmpty "default export became standalone metadata"
  require (m.body.map Prod.fst == [0] && m.exports == [("default", "default")])
    "default export changed"

private def checkMixed (j : Json) : IO Unit := do
  let (p, rows) ← decode j
  let m ← onlyModule p
  let [row] := rows | throw (IO.userError "expected only the standalone export row")
  require (row.ordinal == 1 && row.names == [("alias", "declared")])
    "inline export was incorrectly added to the standalone row"
  require (m.exports == [("declared", "declared"), ("alias", "declared")])
    "mixed inline and local export table changed"

private def checkRefused (j : Json) (expected : String) : IO Unit := do
  match LinkedInput.decodeSingletonSourceWithLocalExports j "entry.mjs" with
  | .ok _ => throw (IO.userError s!"expected refusal: {expected}")
  | .error why => require (why == expected) s!"expected {expected}; got {why}"

def main : IO UInt32 := do
  let line ← (← IO.getStdin).getLine
  let cases ← IO.ofExcept (Json.parse line >>= Json.getArr?)
  unless cases.size == 12 do throw (IO.userError "expected parsed sources, the authored specs and source text")
  let specText ← IO.ofExcept cases[9]!.getStr?
  let sourceText ← IO.ofExcept cases[10]!.getStr?
  let sparseSpecText ← IO.ofExcept cases[11]!.getStr?
  checkGraph cases[0]! specText sourceText
  checkSparse cases[1]! sparseSpecText
  checkAliases cases[2]!
  checkInline cases[3]!
  checkDefault cases[4]!
  checkRefused cases[5]! "unsupported re-export from another module"
  checkRefused cases[6]! "checked construction requires an import-free original"
  checkMixed cases[7]!
  checkOmitted cases[8]! specText (sourceText ++ "\ndebugger;")
  return 0
