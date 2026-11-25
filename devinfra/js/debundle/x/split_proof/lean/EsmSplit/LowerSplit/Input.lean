import EsmSplit.Cli
import EsmSplit.CoreJS.Split
import EsmSplit.Effects
import EsmSplit.SplitFiles

/-! Source, footprints and placement shared by the certified split and the diagnostic preview. -/

namespace EsmSplit.LowerSplit

open Lean EsmSplit.SplitFiles

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

private def toStmt (src : String) (ast : Json) (f : Lower.Footprint) : Stmt :=
  { src, defs := f.defs, writes := f.writes, assigns := f.assigns, deps := f.deps, eager := f.eager, lazy := f.lazy,
    exports := published ast, hoisted := f.hoisted, effect := f.effect }

/-- `FootprintsOfSyntax`, decided once. Inlined into the verdict, a stack sample on the large bundle found it re-run
inside the counts below, and the verdict did not finish in 10 minutes. -/
@[noinline] def footprintsOk (ss : List CoreJS.Stmt) (B : Bundle) : Bool := decide (CoreJS.FootprintsOfSyntax ss B)

structure SplitPaths where
  bundle : System.FilePath
  spec : System.FilePath
  out : System.FilePath

structure BundleInput where
  program : Json
  source : String
  input : System.FilePath

def readBundle (bundlePath work : System.FilePath) (validateSnapshot timings : Bool)
    (sourceHash : Option String := none) : IO BundleInput := do
  let program ← timePhase timings "input_parse" fun _ => loadProgram bundlePath work validateSnapshot sourceHash
  let source ← IO.ofExcept (program.getObjValAs? String "sourceText")
  let input := work / "source.mjs"
  IO.FS.writeFile input source
  return { program, source, input }

def unsupportedStatement (s : Json) : Option String :=
  -- `export * from` publishes names that only the other module knows: a glue export could shadow one.
  if Lower.typ s == "ExportAllDeclaration" then
    some "unsupported top-level statement: ExportAllDeclaration"
  else if Lower.typ s == "ImportDeclaration" && Lower.isNull (Lower.fld (Lower.fld s "source") "lit") then
    some "unsupported top-level statement: an ImportDeclaration whose source has no literal annotation"
  else (Lower.outsideModel false s).map fun what => s!"unsupported: {what}"

def analyzeFootprints (asts units : Array Json) (prem : Effects.Premises) (timings : Bool) :
    IO (Array Lower.Footprint × Effects.Result) :=
  let exported := units.toList.flatMap Lower.exportedBindings
  timePhase timings "footprints" fun _ =>
    IO.lazyPure fun _ => Effects.footprints units (Effects.importsOf asts) prem exported.toArray

def bundleOfFootprints (asts : Array Json) (fps : Array Lower.Footprint) (texts : Array String) : Bundle :=
  (asts.zip fps).toList.zipIdx.map fun ((ast, f), i) => toStmt (texts.getD i "") ast f

/-- The linked execution theorem checks the schedules themselves. Its planner needs only the
bindings each unit declares, mentions, assigns and exports. Promoting lazy reads to eager in
the effect analysis leaves their first-occurrence order in `eager ++ lazy` unchanged. -/
def linkedLayoutBundle (asts units : Array Json) : Bundle :=
  let tops := units.map Lower.analyzeTop
  let declared := Effects.declaredNames tops
  (asts.zip tops).toList.map fun (ast, t) =>
    { src := "", defs := t.defs, eager := (Effects.dedupeArr t.rawEager).toList,
      lazy := (Effects.dedupeArr t.rawLazy).toList, assigns := Effects.assignedNames t declared,
      exports := published ast, hoisted := t.hoisted }

/-- The entry module keeps the published interface and loads outside imports before the rest of the bundle. -/
def entryPlacementProblem (B : Bundle) (S : Spec) (ast : Json) (i : Nat) : Option String :=
  let t := Lower.typ ast
  if (t == "ImportDeclaration" || t == "ExportNamedDeclaration" || t == "ExportDefaultDeclaration")
      && S.moduleOf B i != some S.entry then
    some s!"statement {i} is an `{t}` outside the entry module"
  else none

def planSplit (B : Bundle) (S : Spec) (timings : Bool) : IO Plan := do
  let P ← timePhase timings "placement" fun _ => IO.lazyPure fun _ => mkPlan B S
  let _ ← IO.lazyPure fun _ => P.mods.length + (P.mods.map (·.imports.length)).sum
  return P

end EsmSplit.LowerSplit
