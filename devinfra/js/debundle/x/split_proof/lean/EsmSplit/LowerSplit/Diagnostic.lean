import EsmSplit.Cli
import EsmSplit.CoreJS.Declaration.Certificate
import EsmSplit.CoreJS.Decode
import EsmSplit.CoreJS.FromESTree
import EsmSplit.CoreJS.HeritageCertificate
import EsmSplit.CoreJS.Move
import EsmSplit.ESTreeEmit
import EsmSplit.LowerSplit.Input
import Std.Data.HashMap

/-! Report plan checks and model diagnostics and optionally emit an uncertified preview. -/

namespace EsmSplit.LowerSplit

open Lean EsmSplit.SplitFiles

/-- The footprints `lower` printed for this bundle (one JSON object per statement). The analysis takes minutes on a
large bundle and does not depend on the split, so a run of many splits computes it once. The scan of the statements is
cheap: a file that does not agree with it was made for another bundle. -/
private def loadFootprints (path : String) (asts : Array Json) : IO (Array Lower.Footprint) := do
  let lines := ((← IO.FS.readFile path).splitOn "\n").filter (· != "")
  unless lines.length == asts.size do
    throw (IO.userError s!"{path}: {lines.length} footprints for {asts.size} statements")
  let mut out := #[]
  for (line, ast) in lines.zip asts.toList do
    let f ← IO.ofExcept (Lower.Footprint.ofJson (← IO.ofExcept (Json.parse line)))
    let t := Lower.analyzeTop ast
    unless f.defs == t.defs && f.hoisted == t.hoisted && f.rawEager == (Effects.dedupeArr t.rawEager).toList
        && f.rawLazy == (Effects.dedupeArr t.rawLazy).toList && f.fallback == (Effects.dedupeArr t.fallback).toList do
      throw (IO.userError s!"{path}: statement {out.size} does not match its footprint: another bundle's file?")
    out := out.push f
  return out

/-! ## CoreJS model diagnostics -/

private partial def anyNode (p : Json → Bool) (j : Json) : Bool := p j || (Lower.children j).any (anyNode p)

/-- Does `j` read `import.meta` other than in `new URL("/absolute", import.meta.url)`? That one resolves against the
origin, whichever file the statement is in (as `translate` reads it). -/
private partial def readsMeta (j : Json) : Bool :=
  let args := Lower.items (Lower.fld j "arguments")
  let callee := Lower.fld j "callee"
  if Lower.typ j == "NewExpression" && Lower.typ callee == "Identifier" && Lower.str (Lower.fld callee "name") == "URL"
      && Lower.str (Lower.fld callee "k") == "G" && args.size == 2 && Lower.typ args[0]! == "Literal"
      && (Lower.str (Lower.fld args[0]! "lit")).startsWith "s:/" && Lower.typ args[1]! == "MemberExpression"
      && Lower.fld args[1]! "computed" == Json.bool false && Lower.typ (Lower.fld args[1]! "object") == "MetaProperty"
      && Lower.str (Lower.fld (Lower.fld args[1]! "property") "name") == "url" then false
  else (Lower.typ j == "MetaProperty" && Lower.str (Lower.fld (Lower.fld j "meta") "name") == "import")
    || (Lower.children j).any readsMeta

/-- The bundle as the order-kept slice reads it: without its `import`s of outside modules. Such an `import` binds names
of the world before any statement runs, wherever it stands, and is an empty statement to the analysis
(`Effects.footprintUnits`): it declares nothing and nothing mentions it. Leaving it out of the bundle, of the spec and so
of the plan changes no other statement, module, import or export, which `withoutImports` checks; then the split it
writes is the split of the bundle without them. -/
private structure WithoutImports where
  /-- the new number of each statement, `none` for an `import` -/
  rank : Array (Option Nat)
  /-- the number in the bundle of each new number -/
  kept : Array Nat
  B : Bundle
  S : Spec
  P : Plan

private def withoutImports (asts : Array Json) (B : Bundle) (S : Spec) : WithoutImports :=
  let kept := (List.range asts.size).filter fun i => Lower.typ (asts.getD i Json.null) != "ImportDeclaration"
  let rank := kept.zipIdx.foldl (fun (r : Array (Option Nat)) (i, k) => r.set! i (some k)) (Array.replicate asts.size none)
  let B' := kept.map B.stmt
  let S' := { S with modules := S.modules.map fun (m, sels) => (m, sels.filterMap fun
    | .stmt i => (rank.getD i none).map .stmt
    | .name u => some (.name u)) }
  { rank, kept := kept.toArray, B := B', S := S', P := mkPlan B' S' }

/-- What differs between the plan and the plan without the outside `import`s, other than those statements. -/
private def importsMatter (P : Plan) (w : WithoutImports) : Option String :=
  if P.evalOrder != w.P.evalOrder then some "the module order" else
  if P.mods.length != w.P.mods.length then some "the modules" else
  (P.mods.zip w.P.mods).findSome? fun (m, m') =>
    if m.name != m'.name then some "the modules"
    else if m.stmts.filterMap (w.rank.getD · none) != m'.stmts then some s!"the statements of {m.name}"
    else if m.imports != m'.imports then some s!"the imports of {m.name}"
    else if m.exports != m'.exports then some s!"the exports of {m.name}"
    else none

/-- Body of the modeled bundle after external imports have been removed, in source order.
Only syntactic no-ops contribute an empty block. -/
private structure ModelBody where
  stmts : List CoreJS.Stmt
  empty : Nat
  external : Nat

/-- Index a legacy frontend's statements by their original (pre-import-removal) identities. -/
private def externalStatements (asts : Array Json) (prog : CoreJS.Program) : Except String (Std.HashMap Nat CoreJS.Stmt) := do
  let [m] := prog.modules | throw s!"expected one module, found {prog.modules.length}"
  unless m.imports.isEmpty do throw "outside imports must be represented by the world"
  let mut byId := {}
  for (i, s) in m.body do
    let some ast := asts[i]? | throw s!"statement {i} outside the bundle"
    if byId.contains i then throw s!"duplicate statement {i}"
    if Lower.typ ast == "ImportDeclaration" then throw s!"unexpected model for import {i}"
    byId := byId.insert i s
  return byId

/-- Prefer the AST for every unit. Load the external program only on the first unsupported unit. -/
private def modelBody (asts : Array Json) (path : Option String) : IO (Except String ModelBody) := do
  let mut byId : Option (Std.HashMap Nat CoreJS.Stmt) := none
  let mut stmts := #[]
  let mut empty := 0
  let mut external := 0
  for (ast, i) in asts.zipIdx do
    if Lower.typ ast == "ImportDeclaration" then continue
    match CoreJS.FromESTree.unit ast with
    | .ok (some s, _) => stmts := stmts.push s
    | .ok (none, _) => stmts := stmts.push (.block []); empty := empty + 1
    | .error why =>
      let some file := path | return .error s!"statement {i}: {why}"
      if byId.isNone then
        match Json.parse (← IO.FS.readFile file) >>= CoreJS.Decode.program >>= externalStatements asts with
        | .error e => return .error s!"invalid external lowering {file}: {e}"
        | .ok t => byId := some t
      let some s := byId.bind (·[i]?) | return .error s!"external lowering omits statement {i}: {why}"
      stmts := stmts.push s
      external := external + 1
  return .ok ⟨stmts.toList, empty, external⟩

/-- The first statement whose footprint does not say what its text says, to explain a failed `FootprintsOfSyntax`. -/
private def footprintProblem (B : Bundle) (kept : Array Nat) (ss : List CoreJS.Stmt) : Option String :=
  let fps := B.toArray
  ss.zipIdx.findSome? fun (s, k) =>
    let f := fps.getD k default
    let i := kept.getD k k
    let declared := s.bindings.map (·.1)
    if !(f.defs.all declared.contains && declared.all f.defs.contains) then
      some s!"statement {i}: the footprint declares {f.defs}, the statement {declared}"
    else match s.free.filter fun x => !(f.defs.contains x || f.eager.contains x || f.lazy.contains x) with
      | [] => none
      | missing => some s!"statement {i} mentions {missing.eraseDups}, which its footprint does not list"

/-- Inverted pairs rejected by a move relation: how many, and the first. For verdict diagnostics only. -/
private def movesFailures (allows : Nat → Nat → Bool) (order : Array Nat) : Nat × Nat × Option (Nat × Nat) := Id.run do
  let mut inverted := 0
  let mut failed := 0
  let mut first := none
  for h : x in [0:order.size] do
    let a := order[x]
    for h' : y in [x+1:order.size] do
      let b := order[y]
      if b < a then
        inverted := inverted + 1
        unless allows a b do
          failed := failed + 1
          if first.isNone then first := some (a, b)
  return (inverted, failed, first)

/-- Diagnostic evidence only: pair permissions do not establish the frontend and plan premises. -/
private def moveReport (asts : Array Json) (B : Bundle) (S : Spec) (P : Plan)
    (body : ModelBody) : Except String Json := do
  let w := if asts.any (Lower.typ · == "ImportDeclaration") then withoutImports asts B S
    else { rank := (Array.range asts.size).map some, kept := Array.range asts.size, B, S, P }
  let ss := body.stmts
  let certs := CoreJS.bindingCertificateTab ss w.B
  let allows := CoreJS.certifiedMayMoveAt ss.toArray w.B.toArray w.B.readsTab certs
  let order := w.P.execOrder.toArray
  let (inverted, failed, _) := movesFailures allows order
  let mut examples : Array Json := #[]
  for x in [:order.size] do
    for y in [x+1:order.size] do
      let a := order[x]!
      let b := order[y]!
      if b < a && !allows a b && examples.size < 64 then
        examples := examples.push (toJson [w.kept.getD a a, w.kept.getD b b])
  let rows := ss.zipIdx.map fun (s, i) => Json.mkObj [
    ("id", toJson (w.kept.getD i i)),
    ("opaque", toJson (match s with | .opaque .. => true | _ => false)),
    ("inert", toJson s.inert), ("allocatesOnly", toJson s.allocs.isSome),
    ("bindingSummary", toJson s.bindingSummary.isSome),
    ("bindingCertificate", toJson (certs.getD i false))]
  let heritage := failed > 0 && CoreJS.heritageMovesOk ss w.B w.P.execOrder
  return Json.mkObj [("externallyLowered", toJson body.external), ("footprintsOfSyntax", toJson (footprintsOk ss w.B)),
    ("statements", toJson rows), ("invertedPairs", toJson inverted),
    ("pairwiseFailedPairs", toJson failed), ("pairwiseFailedPairExamples", toJson examples),
    ("certifiedClassHeritage", toJson heritage), ("failedPairs", toJson (if heritage then 0 else failed)),
    ("failedPairExamples", toJson (if heritage then #[] else examples))]

/-- How many statements are opaque, inert, and only allocate without being inert. -/
@[noinline] private def modelCounts (ss : List CoreJS.Stmt) : Nat × Nat × Nat :=
  (ss.countP fun | .opaque .. => true | _ => false, ss.countP (·.inert), ss.countP fun s => !s.inert && s.allocs.isSome)

/-- Report theorem premises for the diagnostic model of the proposed plan. These checks use
the bundle without its outside `import`s
(`WithoutImports`), after `Valid` and `FootprintsOfSyntax`:

- `CoreJS.valid_order_kept_preserves`, for a plan that keeps the bundle's order (`OrderKept`): every way the bundle
  ends, the split ends the same way.
- `CoreJS.moves_preserve`, for a plan whose every pair out of the bundle's order passes `MayMove` (`movesOk`, which
  `movesOk_iff` proves equal to that hypothesis): the bundle ends normally in a state exactly when the split does.
- `CoreJS.moves_preserve_of_certifiedMovesOk` adds supported binding statements whose actual summaries pass a
  total coverage check and do not conflict.
- `CoreJS.moves_preserve_of_heritageMovesOk` moves a derived class across a block after checking an earlier
  class declaration establishes its superclass and intervening steps preserve it. -/
private def modelDiagnostics (program : Json) (asts : Array Json) (B : Bundle) (S : Spec) (P : Plan) (checked : Bool)
    (model : Except String ModelBody) : List String :=
  let kept := "order kept (`CoreJS.valid_order_kept_preserves`)"
  let moves := "moves (`CoreJS.moves_preserve`)"
  let dynamicImport := asts.any (anyNode (Lower.typ · == "ImportExpression"))
  let metaUrl := asts.any (anyNode (Lower.typ · == "MetaProperty"))
  let covered : Except String (List String) := do
    unless checked do throw "the split to pass the checks above"
    if let some i := asts.findIdx? readsMeta then
      throw s!"a bundle that does not read `import.meta`, which names the file a statement is in (statement {i}), other than in `new URL(\"/absolute\", import.meta.url)`"
    if dynamicImport && P.mods.any (·.name.contains '/') then
      throw "every module in one directory, since the bundle has `import()`, which resolves against the file"
    let imports := (asts.filter (Lower.typ · == "ImportDeclaration")).size
    let w := if imports == 0 then { rank := (Array.range asts.size).map some, kept := Array.range asts.size, B, S, P }
      else withoutImports asts B S
    if imports > 0 then
      if let some what := importsMatter P w then
        throw s!"the outside `import`s to change nothing else when left out, and they change {what}"
      unless decide (Valid w.B w.S w.P) do throw "the split without the outside `import`s to pass the checks"
    let body ← model.mapError fun why => s!"a CoreJS model: {why}"
    let ss := body.stmts
    let nothing := body.empty
    let modelSource := if body.external == 0 then "Lean ESTree lowering"
      else s!"Lean ESTree lowering (externally lowered statements: {body.external})"
    unless footprintsOk ss w.B do
      throw s!"FootprintsOfSyntax: {(footprintProblem w.B w.kept ss).getD "it fails"}"
    let (nOpaque, inertN, allocN) := modelCounts ss
    let orderKept := decide (CoreJS.OrderKept ss w.P)
    let arr := ss.toArray
    let syntaxMoves := if orderKept then false else CoreJS.movesOk arr w.P.execOrder
    let certifiedMoves := if orderKept || syntaxMoves then false else CoreJS.certifiedMovesOk ss w.B w.P.execOrder
    let heritage := !orderKept && !syntaxMoves && !certifiedMoves && CoreJS.heritageMovesOk ss w.B w.P.execOrder
    unless orderKept || syntaxMoves || certifiedMoves || heritage do
      let firstOut := (w.P.execOrder.zip (List.range w.B.length)).find? fun (a, b) => a != b
      let allows := CoreJS.certifiedMayMoveAt arr w.B.toArray w.B.readsTab (CoreJS.bindingCertificateTab ss w.B)
      let (inverted, failed, first) := movesFailures allows w.P.execOrder.toArray
      let orig (k : Nat) := w.kept.getD k k
      throw s!"the plan to keep the order (OrderKept){match firstOut with
        | some (a, b) => s!": statement {orig a} runs where statement {orig b} does" | none => ""}; {moves} or certified bindings need every inverted pair to pass MayMove or checked nonconflicting binding summaries, and {failed} of the {inverted} do not{match first with
        | some (a, b) => s!", the first statement {orig a} before statement {orig b}" | none => ""}"
    let name := if orderKept then kept else if syntaxMoves then moves
      else if heritage then "moves (certified class heritage) (`CoreJS.moves_preserve_of_heritageMovesOk`)"
      else "moves (certified bindings) (`CoreJS.moves_preserve_of_certifiedMovesOk`)"
    let (inverted, _, _) := if orderKept then (0, 0, none) else movesFailures (CoreJS.MayMoveAt arr) w.P.execOrder.toArray
    let sourceProof := body.external == 0 && (orderKept || syntaxMoves || certifiedMoves)
      && CoreJS.DeclarationCertificate.covers program ss
    let lines := [if orderKept then s!"  model diagnostic: {name}: in the model, the split ends as the bundle does, for every world"
        else s!"  model diagnostic: {name}: in the model, the split ends normally in a state exactly when the bundle does, for every world"]
      ++ (if sourceProof then
        let outcomes := if orderKept then "all modeled outcomes" else "normally terminating executions"
        [s!"    Declaration extraction: unsplit declaration lists and the checked CoreJS plan agree on {outcomes}."]
        else [])
      ++ (if imports == 0 then [] else
        [s!"    of the bundle without its `import`s of outside modules ({imports}), which bind before any statement runs; the split without them is the same otherwise"])
      ++ [s!"    Valid: the nine checks{if imports == 0 then " above" else ", again"}"]
      ++ (if orderKept then [s!"    OrderKept: the modules run the {ss.length} statements in the bundle's order"]
        else if heritage then
          ["    Class heritage: the base declaration establishes the constructor and its own prototype. Intervening units preserve both."]
        else if certifiedMoves then
          [s!"    Certified moves: all {inverted} inverted pairs pass MayMove or have checked binding summaries with no footprint conflict"]
        else [s!"    MayMove: the modules run {inverted} pairs of the {ss.length} statements out of the bundle's order, and each passes ({inertN} statements are inert, {allocN} more only allocate)"])
      ++ [s!"    FootprintsOfSyntax: against {modelSource}: {ss.length - nOpaque - nothing} statements in the model's syntax, {nOpaque} opaque, {nothing} that do nothing (an `export` list, `;`, `debugger`)",
      s!"    trusted: the model; the front end's reading of the scope; outside modules are part of the world{if dynamicImport then "; `import()` is opaque, faithful since every module is in one directory" else ""}{if metaUrl then "; `new URL(\"/absolute\", import.meta.url)` resolves against the origin" else ""}"]
      ++ (if orderKept then [] else ["    not covered: a run that throws, runs out of fuel or does not end (premise P4); the split may end otherwise then"])
      ++ [if certifiedMoves || heritage then
        "    Footprint certificates: the actual supplied reads and writes cover the supported CoreJS statements; producer correctness is not assumed"
        else "    not among its hypotheses: the footprint analysis and its premises"]
    return lines
  match covered with
  | .ok lines => lines
  | .error why => [if why.startsWith "the plan to keep the order" then s!"  model diagnostic: none. {kept} needs {why}"
      else s!"  model diagnostic: none. {kept} and {moves} need {why}"]

private structure DiagnosticOptions where
  prem : Effects.Premises
  footprintPath : Option String
  planPath : Option String
  corejsPath : Option String
  moveReportPath : Option String
  force : Bool
  allConflicts : Bool

private def parseDiagnosticOptions (args : List String) : IO (System.FilePath × System.FilePath × DiagnosticOptions) := do
  let (premPath, args) := EsmSplit.takeFlag "--premises" args
  let (footprintPath, args) := EsmSplit.takeFlag "--footprints" args
  let (planPath, args) := EsmSplit.takeFlag "--plan" args
  let (corejsPath, args) := EsmSplit.takeFlag "--corejs" args
  let (moveReportPath, args) := EsmSplit.takeFlag "--move-report" args
  if premPath.isSome && footprintPath.isSome then
    throw (IO.userError "--footprints and --premises: the footprints rest on the premises `lower` ran under")
  let prem ← Effects.loadPremises premPath
  let force := args.contains "--force"
  let allConflicts := args.contains "--all-conflicts"
  if allConflicts && planPath.isNone then throw (IO.userError "--all-conflicts requires --plan")
  let args := args.filter fun a => a != "--force" && a != "--all-conflicts"
  let (manifest, out) ← match args with
    | [manifest, out] => pure (manifest, out)
    | _ => do
      IO.eprintln "usage: split_preview <deployment.json> <outdir> [--premises <file> | --footprints <file>] [--plan <file> [--all-conflicts]] [--corejs <program.json>] [--move-report <file>] [--force]"
      throw (IO.userError "usage")
  return (manifest, out, { prem, footprintPath, planPath, corejsPath, moveReportPath, force, allConflicts })

private def runDiagnostic (chunk : DeploymentChunk) (out : System.FilePath) (options : DiagnosticOptions)
    (work : System.FilePath) : IO UInt32 := do
  let tStart ← IO.monoMsNow
  let ⟨program, source, input⟩ ← readBundle chunk.source work false false (← IO.ofExcept chunk.sourceHash)
  let t1 ← reportElapsed "read + parse JSON" tStart
  let asts ← IO.ofExcept (ESTreeUnits.checkedNodes program source)
  if let some reason := asts.findSome? unsupportedStatement then
    IO.eprintln reason
    return 3
  let units := Effects.footprintUnits asts
  let fps ← match options.footprintPath with
    | some p => do
      IO.println s!"  footprints read from {p}: the premises printed by `lower` when it wrote them apply"
      loadFootprints p units
    | none => do
      let (fps, analysis) ← analyzeFootprints asts units options.prem false
      for line in Effects.premiseLines options.prem analysis do IO.println line
      pure fps
  let t2 ← reportElapsed "footprints (Lean analysis)" t1
  let B := bundleOfFootprints asts fps #[]
  let S ← IO.ofExcept (chunk.spec asts.size)
  let t3 ← reportElapsed "load spec" t2
  IO.println s!"{B.length} statements; footprints computed in Lean; {(fps.filter (·.effect)).size} touch the world; {(fps.filter (·.fallback ≠ [])).size} with fallback nodes"
  let mut bad := false
  for (ast, i) in asts.zipIdx do
    if let some reason := entryPlacementProblem B S ast i then
      IO.println s!"  FAIL  {reason}"
      bad := true
  let P ← planSplit B S false
  let t4 ← reportElapsed "mkPlan" t3
  let ok ← reportPlanChecks B S P
  let t5 ← reportElapsed "all checks" t4
  let model ← modelBody asts options.corejsPath
  if let some path := options.moveReportPath then
    let report := model >>= moveReport asts B S P
    IO.FS.writeFile path ((match report with
      | .ok value => value
      | .error reason => Json.mkObj [("error", toJson reason)]).compress ++ "\n")
  for line in modelDiagnostics program asts B S P (ok && !bad) model do IO.println line
  if options.force || (ok && !bad) then
    match ESTreeEmit.programs asts B P source with
    | .error message => IO.eprintln s!"cannot write output: {message}"; return 3
    | .ok programs => javascript #["--print"] #[← writeProgramManifest programs work, out, input]
    if let some planPath := options.planPath then
      let (count, sample, every) := if ok then (0, #[], #[]) else conflictInversions B P options.allConflicts
      let pairs := if options.allConflicts then
          [("pairs", toJson (every.map fun (later, earlier, _) => #[earlier, later]))]
        else []
      let examples := sample.map fun (later, earlier) =>
        Json.mkObj [("earlier", toJson earlier), ("later", toJson later)]
      IO.FS.writeFile planPath (Json.mkObj [
        ("order", Json.arr (P.evalOrder.toArray.map Json.str)),
        ("orderConflicts", Json.mkObj ([("count", toJson count), ("examples", Json.arr examples)] ++ pairs)),
        ("modules", Json.mkObj (P.mods.map fun m => (m.name, toJson m.stmts)))]).compress
    let _ ← reportElapsed "render + write" t5
    IO.println s!"  [time] total: {(← IO.monoMsNow) - tStart} ms"
  if ok && !bad then return 0
  IO.eprintln (if options.force then "rejected; written anyway (--force)" else "rejected; nothing written"); return 1

def diagnostic (args : List String) : IO UInt32 := do
  if args.contains "--require" || args.contains "--check-output" || args.contains "--structural" || args.contains "--deployment-origin" then
    IO.eprintln "certification options are not supported by split_preview"
    return 1
  IO.println "Diagnostic run. Any emitted files are an uncertified preview."
  IO.FS.withTempDir fun work => do
    let (manifestPath, out, options) ← parseDiagnosticOptions args
    let manifest ← readDeploymentManifest manifestPath
    let [chunk] := manifest.chunks | throw (IO.userError "diagnostic options require one original chunk")
    runDiagnostic chunk out options work

end EsmSplit.LowerSplit
