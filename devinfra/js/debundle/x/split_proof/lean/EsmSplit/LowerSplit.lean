import EsmSplit.CoreJS.StructuralCertificate
import EsmSplit.ReadDeployment
import EsmSplit.SplitFiles
import EsmSplit.ESTreeUnits
import EsmSplit.InstanceSource
import EsmSplit.Cli
import EsmSplit.Effects
import EsmSplit.CoreJS.FromESTree
import EsmSplit.CoreJS.DeclarationArtifacts
import EsmSplit.CoreJS.Decode
import EsmSplit.CoreJS.Split
import EsmSplit.CoreJS.Move
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.LinkedCertificate
import EsmSplit.CoreJS.HeritageCertificate
import Std.Data.HashMap
import Std.Data.HashSet

/-!
# AST + spec in; modules, verdict and a kernel certificate out.

    lowersplit <bundle.mjs|bundle.json> <spec.json> <outdir> [--require <all-outcomes|normal-completion>] [--check-output] [--host-contract PROFILE.json] [--structural [--deployment-origin ORIGIN]]
    lowersplit --diagnostic <bundle.mjs|bundle.json> <spec.json> <outdir> [--lean <Generated.lean> [--namespace NAME] [--variant SPEC]... [--control SPEC]...] [--replicate <K>] [--premises <file> | --footprints <file>] [--plan <file> [--all-conflicts]] [--corejs <program.json>] [--move-report <file>] [--force]

JavaScript source and annotated ESTree dumps share the Acorn parser and scope/literal metadata.
Certified mode checks the requested CoreJS guarantee before publication.
With --check-output, it reparses the existing tree before running the proof.
Lean computes the footprints
(`Effects.lean`, under the premises in the file, if any), plans the split, runs the checks, writes the modules, and with `--lean` writes a
Lean file whose `by decide +kernel` makes the kernel certify the same facts.
The bundle may import from outside modules; those are loaded first, in their order, as in the bundle.
`--force` writes the modules even when a check rejects the split, to see what Node does with them.
`--plan` writes the plan beside the modules, as JSON: `{"order": […], "modules": {name: [statement, …]}}` has the order
in which the modules run (`Plan.evalOrder`) and the statements of each module, by their numbers in the bundle.
With `--all-conflicts`, the plan includes every inverted structural conflict as an [earlier, later] pair.
Lean lowers supported annotated ASTs directly to CoreJS. For bundles outside that subset,
`--corejs` supplies the external lowering. The verdict reports which input it used.
`--move-report` writes diagnostic statement categories and up to 64 failing inverted pairs in original unit IDs.
It does not certify a split: the verdict still requires all frontend and plan premises.
`--replicate K` is for timing: K renamed copies of the bundle (the spec must cover K times as many statements).
-/

open Lean EsmSplit EsmSplit.SplitFiles

/-- What a top-level `export` statement publishes: each public name, with the local binding it stands for, if any. -/
def published (s : Json) : List (String × Option String) :=
  match Lower.typ s with
  | "ExportNamedDeclaration" =>
    if !Lower.isNull (Lower.fld s "declaration") then
      (Lower.topDefs (Lower.fld s "declaration")).map fun n => (n, some n)
    else (Lower.items (Lower.fld s "specifiers")).toList.map fun sp =>
      (Lower.specName (Lower.fld sp "exported"),
        if Lower.isNull (Lower.fld s "source") then some (Lower.specName (Lower.fld sp "local")) else none)
  | "ExportDefaultDeclaration" => [("default", none)]
  | _ => []

def toStmt (src : String) (ast : Json) (f : Lower.Footprint) : Stmt :=
  { src, defs := f.defs, writes := f.writes, assigns := f.assigns, deps := f.deps, eager := f.eager, lazy := f.lazy,
    exports := published ast, hoisted := f.hoisted, effect := f.effect }

/-- The linked execution theorem checks the schedules themselves. Its planner needs only the
bindings each unit declares, mentions, assigns and exports. Promoting lazy reads to eager in
the effect analysis leaves their first-occurrence order in `eager ++ lazy` unchanged. -/
private def linkedLayoutBundle (asts units : Array Json) : Bundle :=
  let tops := units.map Lower.analyzeTop
  let declared := Effects.declaredNames tops
  (asts.zip tops).toList.map fun (ast, t) =>
    { src := "", defs := t.defs, eager := (Effects.dedupeArr t.rawEager).toList,
      lazy := (Effects.dedupeArr t.rawLazy).toList, assigns := Effects.assignedNames t declared,
      exports := published ast, hoisted := t.hoisted }

/-- Timing only: `K` copies of the bundle, each declaring its own renamed copy of every declared name.
The statement text is not renamed, so the modules written from this are meaningless. -/
def replicateBundle (K : Nat) (B : Bundle) : Bundle :=
  let declared : Std.HashSet String := Std.HashSet.ofList (B.flatMap (·.defs))
  (List.range K).flatMap fun k =>
    let r (n : String) := if declared.contains n then s!"{n}${k}" else n
    B.map fun s => { s with defs := s.defs.map r, eager := s.eager.map r, lazy := s.lazy.map r,
                            exports := s.exports.map fun (p, l) => (s!"{p}${k}", l.map r) }

/-- The footprints `lower` printed for this bundle (one JSON object per statement). The analysis takes minutes on a
large bundle and does not depend on the split, so a run of many splits computes it once. The scan of the statements is
cheap: a file that does not agree with it was made for another bundle. -/
def loadFootprints (path : String) (asts : Array Json) : IO (Array Lower.Footprint) := do
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

partial def anyNode (p : Json → Bool) (j : Json) : Bool := p j || (Lower.children j).any (anyNode p)

/-- Does `j` read `import.meta` other than in `new URL("/absolute", import.meta.url)`? That one resolves against the
origin, whichever file the statement is in (as `translate` reads it). -/
partial def readsMeta (j : Json) : Bool :=
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
structure WithoutImports where
  /-- the new number of each statement, `none` for an `import` -/
  rank : Array (Option Nat)
  /-- the number in the bundle of each new number -/
  kept : Array Nat
  B : Bundle
  S : Spec
  P : Plan

def withoutImports (asts : Array Json) (B : Bundle) (S : Spec) : WithoutImports :=
  let kept := (List.range asts.size).filter fun i => Lower.typ (asts.getD i Json.null) != "ImportDeclaration"
  let rank := kept.zipIdx.foldl (fun (r : Array (Option Nat)) (i, k) => r.set! i (some k)) (Array.replicate asts.size none)
  let B' := kept.map B.stmt
  let S' := { S with modules := S.modules.map fun (m, sels) => (m, sels.filterMap fun
    | .stmt i => (rank.getD i none).map .stmt
    | .name u => some (.name u)) }
  { rank, kept := kept.toArray, B := B', S := S', P := mkPlan B' S' }

/-- What differs between the plan and the plan without the outside `import`s, other than those statements. -/
def importsMatter (P : Plan) (w : WithoutImports) : Option String :=
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
structure ModelBody where
  stmts : List CoreJS.Stmt
  empty : Nat
  external : Nat

/-- Index a legacy frontend's statements by their original (pre-import-removal) identities. -/
def externalStatements (asts : Array Json) (prog : CoreJS.Program) : Except String (Std.HashMap Nat CoreJS.Stmt) := do
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
def modelBody (asts : Array Json) (path : Option String) : IO (Except String ModelBody) := do
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
def footprintProblem (B : Bundle) (kept : Array Nat) (ss : List CoreJS.Stmt) : Option String :=
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
def movesFailures (allows : Nat → Nat → Bool) (order : Array Nat) : Nat × Nat × Option (Nat × Nat) := Id.run do
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

/-- `FootprintsOfSyntax`, decided once. Inlined into the verdict, a stack sample on the large bundle found it re-run
inside the counts below, and the verdict did not finish in 10 minutes. -/
@[noinline] def footprintsOk (ss : List CoreJS.Stmt) (B : Bundle) : Bool := decide (CoreJS.FootprintsOfSyntax ss B)

/-- Diagnostic evidence only: pair permissions do not establish the frontend and plan premises. -/
def moveReport (asts : Array Json) (B : Bundle) (S : Spec) (P : Plan)
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
@[noinline] def modelCounts (ss : List CoreJS.Stmt) : Nat × Nat × Nat :=
  (ss.countP fun | .opaque .. => true | _ => false, ss.countP (·.inert), ss.countP fun s => !s.inert && s.allocs.isSome)

inductive Guarantee where
  | allOutcomes | normalCompletion
  deriving DecidableEq

def Guarantee.label : Guarantee → String
  | .allOutcomes => "all-outcomes"
  | .normalCompletion => "normal-completion"

/-- Premises for the closed-declaration artifact theorems. The caller checks Valid
and certifies the output ASTs against these same extracted statements. -/
private def declarationProof? (groups : List CoreJS.DeclarationGroup) (B : Bundle) (P : Plan) :
    Option (Guarantee × List CoreJS.Stmt) := do
  let ss := CoreJS.DeclarationProgram.statements groups
  guard (footprintsOk ss B)
  if decide (CoreJS.OrderKept ss P) then return (.allOutcomes, ss)
  guard (CoreJS.movesOk ss.toArray P.execOrder || CoreJS.certifiedMovesOk ss B P.execOrder)
  return (.normalCompletion, ss)

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
def modelDiagnostics (program : Json) (asts : Array Json) (B : Bundle) (S : Spec) (P : Plan) (checked : Bool) (replicated : Bool)
    (model : Except String ModelBody) : List String :=
  let kept := "order kept (`CoreJS.valid_order_kept_preserves`)"
  let moves := "moves (`CoreJS.moves_preserve`)"
  let dynamicImport := asts.any (anyNode (Lower.typ · == "ImportExpression"))
  let metaUrl := asts.any (anyNode (Lower.typ · == "MetaProperty"))
  let covered : Except String (List String) := do
    if replicated then throw "the bundle as it is, not `--replicate`"
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

/-- Check the generated proof with the same toolchain and model as the splitter. -/
private def checkLean (work : System.FilePath) (args : Array String) : IO Unit := do
  let exe ← IO.appPath
  let args := #["-DwarningAsError=true", s!"--root={work}"] ++ args
  let proc ← if exe.fileName == some "lean" then
      IO.Process.output { cmd := exe.toString, args }
    else do
      -- The native Lake executable is <project>/.lake/build/bin/lowersplit.
      let mut project := exe
      for _ in [:4] do project := project.parent.getD "."
      IO.Process.output { cmd := "lake", args := #["env", "lean"] ++ args, cwd := some project }
  unless proc.exitCode == 0 do
    let detail := (proc.stdout ++ proc.stderr).trimAscii.take 1200
    let reason := s!"could not prove equivalent linked executions (Lean exited with status {proc.exitCode})"
    throw (IO.userError (if detail.isEmpty then reason else s!"{reason}: {detail}"))

private def checkProofModule (work : System.FilePath) (setup : Lean.ModuleSetup)
    (code : String) (args : Array String) : IO Unit := do
  let stem := setup.name.toString
  let file := work / (stem ++ ".lean")
  let config := work / (stem ++ ".setup.json")
  IO.FS.writeFile file code
  IO.FS.writeFile config (toJson setup).compress
  checkLean work (#[s!"--setup={config}"] ++ args ++ #[file.toString])

private def checkCheckpoint (timings : Bool) (work : System.FilePath) (text : CoreJS.LinkedBehavior.CheckpointText)
    (dataArts : Lean.NameMap Lean.ImportArtifacts) : IO Unit := do
  let stages := #[(`LinkedReplayInput, text.input), (`LinkedReplaySource, text.source), (`LinkedReplayProof, text.execution)]
  let mut importArts := dataArts
  for (name, code) in stages do
    let compiled := work / (name.toString ++ ".olean")
    timePhase timings name.toString fun _ => do
      checkProofModule work { name, importArts } code #["-o", compiled.toString]
    importArts := importArts.insert name (.ofArrays #[#[compiled]])
  timePhase timings "ast_correspondence" fun _ => do
    checkProofModule work { name := `LinkedReplayGate, importArts } text.gate #[]

/-- The ordinary execution proof can also be imported into a separate AST gate. -/
private def checkOrdinaryExecution (timings : Bool) (work : System.FilePath)
    (cert : CoreJS.LinkedBehavior.Certificate) (dataArts : Lean.NameMap Lean.ImportArtifacts) : IO Unit := do
  if !cert.stagedGate then
    timePhase timings "execution_and_correspondence" fun _ => do
      checkProofModule work { name := `LinkedExecution, importArts := dataArts }
        (cert.preamble ++ cert.execution ++ cert.correspondence) #[]
  else
    let name := `LinkedExecutionProof
    let compiled := work / "LinkedExecutionProof.olean"
    timePhase timings "execution_proof" fun _ => do
      checkProofModule work { name, importArts := dataArts } (cert.preamble ++ cert.execution) #["-o", compiled.toString]
    let importArts := dataArts.insert name (.ofArrays #[#[compiled]])
    timePhase timings "ast_correspondence" fun _ => do
      checkProofModule work { name := `LinkedExecution, importArts }
        ("import LinkedExecutionProof\n" ++ cert.preamble ++ cert.correspondence) #[]

private def writeCertificateData (work : System.FilePath) (terms : CertificateData.Entries) :
    IO (Lean.NameMap Lean.ImportArtifacts) := do
  let dataName := CertificateData.moduleName
  let dataFile := work / (dataName.toString ++ ".olean")
  CertificateData.write dataFile terms
  return ({} : Lean.NameMap Lean.ImportArtifacts).insert dataName (.ofArrays #[#[dataFile]])

/-- Publication waits for the kernel-checked executions and the exact AST gate. -/
private def verifyExecution (timings : Bool) (work : System.FilePath) (cert : CoreJS.LinkedBehavior.Certificate)
    (dataArts : Lean.NameMap Lean.ImportArtifacts) : IO CoreJS.HostPremises := do
  let accepted ← match cert.checkpoint with
    | none => pure false
    | some checkpoint => try
        checkCheckpoint timings work checkpoint dataArts
        pure true
      catch _ => pure false
  unless accepted do checkOrdinaryExecution timings work cert dataArts
  return cert.premises

private def emitVerdict (requested : String) (outcome : Except String Guarantee) (scope : Option String := none) (extra : List (String × Json) := []) : IO UInt32 := do
  let fields := match outcome with
    | .error why => [("status", toJson "rejected"), ("guarantee", Json.null), ("reason", toJson why)]
    | .ok level => [("status", toJson "certified"), ("guarantee", toJson level.label)]
  IO.println (Json.mkObj ([("requested", toJson requested), ("scope", toJson scope)] ++ fields ++ extra)).compress
  return if outcome.isOk then 0 else 1

private def profileVerdict (selected : CoreJS.HostProfile) (used : CoreJS.HostPremises) : List (String × Json) :=
  match selected with
  | .empty => []
  | .browserPage => [("hostProfile", toJson "browser-page"), ("assumptions", toJson used.assumptions)]

private def runInDir (profile : CoreJS.HostProfile) (structural : Bool) (deploymentOrigin : Option String) (work : System.FilePath) (required : Option Guarantee) (checkOnly : Bool) (args : List String) : IO UInt32 := do
  let (replicate?, args) := Effects.takeFlag "--replicate" args
  let (premPath?, args) := Effects.takeFlag "--premises" args
  let (fpPath?, args) := Effects.takeFlag "--footprints" args
  let (planPath?, args) := Effects.takeFlag "--plan" args
  let (corejsPath?, args) := Effects.takeFlag "--corejs" args
  let (moveReportPath?, args) := Effects.takeFlag "--move-report" args
  if premPath?.isSome && fpPath?.isSome then
    throw (IO.userError "--footprints and --premises: the footprints rest on the premises `lower` ran under")
  let prem ← Effects.loadPremises premPath?
  let force := args.contains "--force"
  let allConflicts := args.contains "--all-conflicts"
  if allConflicts && planPath?.isNone then throw (IO.userError "--all-conflicts requires --plan")
  let args := args.filter fun a => a != "--force" && a != "--all-conflicts"
  let (bundlePath, specPath, out, leanOut?) ← match args with
    | [b, s, o] => pure (b, s, o, none)
    | b :: s :: o :: "--lean" :: l :: options => do
      pure (b, s, o, some (l, ← InstanceSource.parseOptions {} options))
    | _ => do IO.eprintln "usage: lowersplit --diagnostic <bundle.mjs|bundle.json> <spec.json> <outdir> [--lean <file> [--namespace NAME] [--variant SPEC]... [--control SPEC]...] [--replicate <K>] [--premises <file> | --footprints <file>] [--plan <file> [--all-conflicts]] [--corejs <program.json>] [--move-report <file>] [--force]"; throw (IO.userError "usage")
  let timings := required.isSome && (← IO.getEnv "SPLIT_PROOF_TIMINGS") == some "1"
  let tStart ← IO.monoMsNow
  let lap (what : String) (t0 : Nat) : IO Nat := do
    let t ← IO.monoMsNow
    if required.isNone then
      IO.println s!"  [time] {what}: {t - t0} ms"; (← IO.getStdout).flush
    return t
  let program ← timePhase timings "input_parse" fun _ => do
    loadProgram bundlePath work required.isSome
  let source ← IO.ofExcept (program.getObjValAs? String "sourceText")
  let input := work / "source.mjs"
  IO.FS.writeFile input source
  let t1 ← lap "read + parse JSON" tStart
  let asts ← IO.ofExcept (ESTreeUnits.checkedNodes program source)
  let unsupported (reason : String) : IO UInt32 := do
    if required.isSome then throw (IO.userError reason)
    IO.eprintln reason
    return 3
  for s in asts do
    -- `export * from` publishes names that only the other module knows: a glue export could shadow one
    if Lower.typ s == "ExportAllDeclaration" then
      return ← unsupported "unsupported top-level statement: ExportAllDeclaration"
    if Lower.typ s == "ImportDeclaration" && Lower.isNull (Lower.fld (Lower.fld s "source") "lit") then
      return ← unsupported "unsupported top-level statement: an ImportDeclaration whose source has no literal annotation"
    if let some what := Lower.outsideModel false s then
      return ← unsupported s!"unsupported: {what}"
  let units := Effects.footprintUnits asts
  let declarations := required.bind fun _ => (CoreJS.DeclarationInput.decode program).toOption
  let fps? ← if required.isSome && declarations.isNone then pure none else
    match fpPath? with
    | some p => do
      IO.println s!"  footprints read from {p}: the premises printed by `lower` when it wrote them apply"
      some <$> loadFootprints p units
    | none => do
      let exported := units.toList.flatMap Lower.exportedBindings
      let (fps, analysis) ← timePhase timings "footprints" fun _ => do
        IO.lazyPure fun _ => Effects.footprints units (Effects.importsOf asts) prem exported.toArray
      if required.isNone then
        for line in Effects.premiseLines prem analysis do IO.println line
      pure (some fps)
  let t2 ← lap "footprints (Lean analysis)" t1
  -- The legacy standalone Lean instance embeds printable source for each unit.
  let texts ← if leanOut?.isSome then do
      let unitsFile := work / "units.json"
      let reply := work / "texts.json"
      IO.FS.writeFile unitsFile (Json.arr asts).compress
      javascript #["--print-units"] #[unitsFile, input, reply]
      IO.ofExcept (fromJson? (α := Array String) (← readJson reply))
    else pure #[]
  let B0 : Bundle ← match fps? with
    | some fps => pure ((asts.zip fps).toList.zipIdx.map fun ((ast, f), i) => toStmt (texts.getD i "") ast f)
    | none => timePhase timings "layout_scan" fun _ => IO.lazyPure fun _ => linkedLayoutBundle asts units
  let copies := replicate?.bind String.toNat?
  let B := match copies with
    | some K => replicateBundle K B0
    | none => B0
  -- Timing mode repeats the source syntax alongside the synthetic bundle.
  let outputAsts := match copies with
    | none => asts
    | some k => (Array.replicate k asts).flatten
  let S ← loadSpec specPath
  if required.isSome then
    for name in S.entry :: S.modules.map Prod.fst do
      let moduleExtension := name.endsWith ".mjs" || (profile == .browserPage && name.endsWith ".js")
      unless CoreJS.ModuleInput.supportedFilename name && moduleExtension do
        throw (IO.userError s!"certified output requires a safe relative .mjs path (.js is supported for browser-page modules), got {name}")
  let t3 ← lap "load spec" t2
  if required.isNone then
    if let some fps := fps? then
      IO.println s!"{B.length} statements; footprints computed in Lean; {(fps.filter (·.effect)).size} touch the world; {(fps.filter (·.fallback ≠ [])).size} with fallback nodes"
  -- A rule Lean does not check. It talks about the original statements, so `--replicate` skips it.
  let mut bad := false
  if replicate?.isNone then
    -- An `export` is the entry module's interface, and an `import` loads an outside module before any statement
    -- of the bundle runs: both stay in the entry module, which places the outside requests first.
    for (ast, i) in asts.toList.zipIdx do
      let t := Lower.typ ast
      if (t == "ImportDeclaration" || t == "ExportNamedDeclaration" || t == "ExportDefaultDeclaration")
          && S.moduleOf B i != some S.entry then
        if required.isSome then throw (IO.userError s!"statement {i} is an `{t}` outside the entry module")
        IO.println s!"  FAIL  statement {i} is an `{t}` outside the entry module"; bad := true
  let P ← timePhase timings "placement" fun _ => do
    IO.lazyPure fun _ => mkPlan B S
  let _ ← IO.lazyPure fun _ => P.mods.length + (P.mods.map (·.imports.length)).sum
  let t4 ← lap "mkPlan" t3
  if let some request := required then
    let layoutOk ← timePhase timings "plan_checks" fun _ => checkLayout B P (some S) false
    -- Only the closed-declaration theorem needs the footprint-based order check.
    let declaration := if !structural && layoutOk && !bad then declarations.bind (declarationProof? · B P) else none
    if let some (level, ss) := declaration.filter (fun _ => decide (OrderOk B P)) then
      try
        if request == .allOutcomes && level != .allOutcomes then
          throw (IO.userError "the checked reorder proves only normal-completion")
        certifyOutput (Json.mkObj []) B P out work input checkOnly timings outputAsts source fun files =>
          timePhase timings "closed_declarations" fun _ => do
            IO.ofExcept (CoreJS.DeclarationArtifacts.check program files ss B P)
        return ← emitVerdict request.label (.ok level) "closed-declarations" (profileVerdict profile {})
      catch e => return ← emitVerdict request.label (.error e.toString) "closed-declarations"
    if structural then
      try
        unless request == .normalCompletion do
          throw (IO.userError "the structural schedule certificate proves only normal-completion")
        unless layoutOk && !bad do
          throw (IO.userError "source placement or module linkage checks failed")
        let providers ← timePhase timings "provider_loading" fun _ => loadProviders program bundlePath work
        let deployment ← deploymentOrigin.mapM fun origin =>
          timePhase timings "deployment_loading" fun _ => readDeployment program providers bundlePath work origin P
        let proof ← certifyOutput providers B P out work input checkOnly timings outputAsts source fun files =>
          timePhase timings "structural_certificate" fun _ => do
            let result ← IO.lazyPure fun _ =>
              CoreJS.LinkedBehavior.certifyStructuralASTs profile program providers files P deployment
            IO.ofExcept (result.map Prod.fst)
        let scope := match proof with
          | .preservedOrder => "linked-structural-order"
          | .singlePivot => "linked-structural-pivot"
        let assumptions :=
          ["Retained source units use the same capability-closed opaque world in both layouts."] ++
          (deploymentOrigin.toList.flatMap fun origin => [
            s!"The static deployment at {origin} overlays only the planned module paths.",
            "The native ESM loader uses one module record per effective URL and resolves dynamic dependencies through the checked live cells."
          ])
        let host := if profile == .browserPage then [("hostProfile", toJson "browser-page")] else []
        return ← emitVerdict request.label (.ok .normalCompletion) scope (host ++ [("assumptions", toJson assumptions)])
      catch e => return ← emitVerdict request.label (.error e.toString) "linked-structural"
    let errorFields ← IO.mkRef ([] : List (String × Json))
    try
      unless layoutOk && !bad do throw (IO.userError "source placement or module linkage checks failed")
      let providers ← timePhase timings "provider_loading" fun _ => do
        loadProviders program bundlePath work
      let used ← IO.mkRef ({} : CoreJS.HostPremises)
      certifyOutput providers B P out work input checkOnly timings outputAsts source fun files => do
        let decoded ← timePhase timings "linked_lowering" fun _ =>
          IO.lazyPure fun _ => CoreJS.LinkedInput.decodePair program providers files P profile
        let (original, candidate) ← match decoded with
          | .ok pair => pure pair
          | .error reason =>
            errorFields.set [("reason_code", toJson "linked_input")]
            throw (IO.userError reason)
        unless request == .normalCompletion do
          throw (IO.userError "linked execution proves only normal-completion")
        let prepared ← timePhase timings "certificate_preparation" fun _ => do
          let result ← IO.lazyPure fun _ =>
            CoreJS.LinkedBehavior.certificate profile program providers files P original candidate
          match result with
          | .error refusal => pure (Except.error refusal)
          | .ok (cert, terms) =>
            try
              return .ok (cert, ← writeCertificateData work terms)
            catch err =>
              return .error { code := .proofPreparation, reason := err.toString }
        let (cert, dataArts) ← match prepared with
          | .ok result => pure result
          | .error refusal =>
            let detail := (refusal.observation.map fun value => ("observation", value)).toList
            errorFields.set (("reason_code", toJson refusal.code.label) :: detail)
            throw (IO.userError refusal.reason)
        used.set (← verifyExecution timings work cert dataArts)
      let active ← used.get
      return ← emitVerdict request.label (.ok .normalCompletion) "linked-programs" (profileVerdict profile active)
    catch e => return ← emitVerdict request.label (.error e.toString) "linked-programs" (← errorFields.get)
  let ok ← reportPlanChecks B S P
  let t5 ← lap "all checks" t4
  let model ← modelBody asts corejsPath?
  if let some path := moveReportPath? then
    let report := model >>= moveReport asts B S P
    IO.FS.writeFile path ((match report with
      | .ok value => value
      | .error reason => Json.mkObj [("error", toJson reason)]).compress ++ "\n")
  for line in modelDiagnostics program asts B S P (ok && !bad) replicate?.isSome model do IO.println line
  if let some (l, options) := leanOut? then
    if bad then
      IO.println "  skipped structural plan proof: source placement failed"
    else
      IO.FS.writeFile l (InstanceSource.render B S options)
      IO.println s!"  wrote structural plan proof source to {l}"
  if force || (ok && !bad) then
    match ESTreeEmit.programs outputAsts B P source with
    | .error message => IO.eprintln s!"cannot write output: {message}"; return 3
    | .ok programs => javascript #["--print"] #[← writeProgramManifest programs work, out, input]
    if let some planPath := planPath? then
      let (count, sample, every) := if ok then (0, #[], #[]) else conflictInversions B P allConflicts
      let pairs := if allConflicts then
          [("pairs", toJson (every.map fun (later, earlier, _) => #[earlier, later]))]
        else []
      let examples := sample.map fun (later, earlier) =>
        Json.mkObj [("earlier", toJson earlier), ("later", toJson later)]
      IO.FS.writeFile planPath (Json.mkObj [
        ("order", Json.arr (P.evalOrder.toArray.map Json.str)),
        ("orderConflicts", Json.mkObj ([("count", toJson count), ("examples", Json.arr examples)] ++ pairs)),
        ("modules", Json.mkObj (P.mods.map fun m => (m.name, toJson m.stmts)))]).compress
    let _ ← lap "render + write" t5
    IO.println s!"  [time] total: {(← IO.monoMsNow) - tStart} ms"
  if ok && !bad then return 0
  IO.eprintln (if force then "rejected; written anyway (--force)" else "rejected; nothing written"); return 1

private def runSplitter (profile : CoreJS.HostProfile) (structural : Bool) (deploymentOrigin : Option String) (required : Option Guarantee) (checkOnly : Bool) (args : List String) : IO UInt32 :=
  IO.FS.withTempDir fun work => runInDir profile structural deploymentOrigin work required checkOnly args

def EsmSplit.LowerSplit.main (args : List String) : IO UInt32 := do
  if let "--diagnostic" :: rest := args then
    if rest.contains "--require" || rest.contains "--check-output" || rest.contains "--structural" || rest.contains "--deployment-origin" then
      IO.eprintln "certification options cannot be combined with --diagnostic"
      return 1
    IO.println "Diagnostic run. Any emitted files are an uncertified preview."
    return ← runSplitter .empty false none none false rest
  let (level?, args) := Effects.takeFlag "--require" args
  let label := level?.getD Guarantee.normalCompletion.label
  try
    let level ← match label with
      | "all-outcomes" => pure Guarantee.allOutcomes
      | "normal-completion" => pure Guarantee.normalCompletion
      | _ => throw (IO.userError "--require expects all-outcomes or normal-completion")
    let (contract?, args) := Effects.takeFlag "--host-contract" args
    let profile ← match contract? with
      | none => pure CoreJS.HostProfile.empty
      | some path => do
        let j ← IO.ofExcept (Json.parse (← IO.FS.readFile path))
        IO.ofExcept (CoreJS.HostProfile.read j)
    let structural := args.contains "--structural"
    let args := args.filter (· != "--structural")
    let (deploymentOrigin, args) := Effects.takeFlag "--deployment-origin" args
    if deploymentOrigin.isSome && !structural then
      throw (IO.userError "--deployment-origin requires --structural")
    let checkOnly := args.contains "--check-output"
    let args := args.filter (· != "--check-output")
    unless args.length == 3 && !args.any (·.startsWith "--") do
      throw (IO.userError "usage: lowersplit <bundle.mjs|bundle.json> spec.json outdir [--require LEVEL] [--check-output] [--host-contract PROFILE.json] [--structural [--deployment-origin ORIGIN]]; use --diagnostic for plan and preview options")
    runSplitter profile structural deploymentOrigin (some level) checkOnly args
  catch e => emitVerdict label (.error e.toString)
