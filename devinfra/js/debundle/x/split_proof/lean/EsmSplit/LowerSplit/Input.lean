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

inductive Placement where
  | fixed
  | authored (spec : Spec)
  | resolved (chunkId : String) (placement : Json)

structure DeploymentChunk where
  path : String
  source : System.FilePath
  placement : Placement

structure DeploymentManifest where
  entry : String
  chunks : List DeploymentChunk

private def relativeTo (manifest : System.FilePath) (value : String) : System.FilePath :=
  let path : System.FilePath := value
  if path.isAbsolute then path else manifest.parent.getD "." / path

def readDeploymentManifest (path : System.FilePath) : IO DeploymentManifest := do
  let json ← readJson path
  let fields ← IO.ofExcept json.getObj?
  unless fields.toList.all (fun (field, _) => ["entry", "chunks"].contains field) do
    throw (IO.userError "unexpected deployment manifest field")
  let entry ← IO.ofExcept (json.getObjValAs? String "entry")
  let chunks ← (← IO.ofExcept (json.getObjValAs? (List Json) "chunks")).mapM fun chunk => do
    let route ← IO.ofExcept (chunk.getObjValAs? String "path")
    let fields ← IO.ofExcept chunk.getObj?
    unless fields.toList.all (fun (field, _) => ["path", "source", "spec", "placement", "chunk_id"].contains field) do
      throw (IO.userError s!"{route}: unexpected deployment chunk field")
    let source ← IO.ofExcept (chunk.getObjValAs? String "source")
    if source.isEmpty then throw (IO.userError s!"{route}: expected a source path")
    let resolve := relativeTo path
    let chunkId ← IO.ofExcept <| (chunk.getObjVal? "chunk_id").toOption.mapM Json.getStr?
    let placement : Placement ← match (chunk.getObjVal? "spec").toOption, (chunk.getObjVal? "placement").toOption with
      | some specPath, none => do
        unless chunkId.isNone do throw (IO.userError s!"{route}: chunk_id requires placement")
        let spec ← loadAuthoredSpec (resolve (← IO.ofExcept specPath.getStr?))
        unless spec.entry == route do throw (IO.userError s!"{route}: spec entry differs from chunk path")
        pure (.authored spec)
      | none, some resolved => do
        let resolvedId ← IO.ofExcept (resolved.getObjValAs? String "chunk_id")
        if resolvedId.isEmpty then throw (IO.userError s!"{route}: expected a placement chunk_id")
        pure (.resolved (chunkId.getD resolvedId) resolved)
      | none, none =>
        if chunkId.isNone then pure .fixed
        else throw (IO.userError s!"{route}: chunk_id requires placement")
      | some _, some _ => throw (IO.userError s!"{route}: supply either spec or placement")
    return { path := route, source := resolve source, placement : DeploymentChunk }
  let routes := chunks.map (·.path)
  IO.ofExcept (validateModulePaths routes)
  unless routes.Nodup do throw (IO.userError "duplicate original chunk path")
  unless routes.contains entry do throw (IO.userError "deployment entry must name an original chunk")
  return { entry, chunks }

def DeploymentChunk.spec (chunk : DeploymentChunk) (unitCount : Nat) : Except String Spec := do
  match chunk.placement with
  | .fixed => throw s!"{chunk.path}: a fixed chunk has no placement"
  | .authored spec => pure spec
  | .resolved id placement => parseResolvedSpec placement id chunk.path unitCount

def DeploymentChunk.sourceHash (chunk : DeploymentChunk) : Except String (Option String) := do
  match chunk.placement with
  | .resolved _ placement => some <$> placement.getObjValAs? String "source_sha256"
  | _ => pure none

def javascriptSource (path : System.FilePath) : Bool :=
  path.extension == some "js" || path.extension == some "mjs"

/-- A deployment mirrors the original JS tree. Verify its declared files have
the named routes in that tree before collecting additional modules from it. -/
def deploymentRoot (manifest : DeploymentManifest) : IO System.FilePath := do
  let mut root := none
  for chunk in manifest.chunks do
    unless javascriptSource chunk.source do
      throw (IO.userError s!"{chunk.path}: a deployment requires the original JavaScript file")
    let mut dir ← IO.FS.realPath chunk.source
    for segment in (chunk.path.splitOn "/").reverse do
      unless dir.fileName == some segment do
        throw (IO.userError s!"{chunk.source} does not have the deployment path {chunk.path}")
      let some parent := dir.parent | throw (IO.userError s!"{chunk.source} has no deployment root")
      dir := parent
    if let some previous := root then
      unless dir == previous do throw (IO.userError "original chunks must belong to one deployment tree")
    root := some dir
  let some directory := root | throw (IO.userError "expected at least one original source")
  return directory

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
