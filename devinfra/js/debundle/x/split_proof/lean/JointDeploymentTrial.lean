import EsmSplit.CoreJS.JointStatic
import EsmSplit.LowerSplit.Input
import EsmSplit.ReadDeployment

/-! Artifact trial: two source-checked placements, one root and one original
resource inventory. Staging and disk comparison precede the joint proof. -/
open Lean CoreJS EsmSplit EsmSplit.LowerSplit EsmSplit.SplitFiles
open System (FilePath)

private structure Part where
  chunk : LinkedInput.StaticSplit
  work : FilePath
  input : FilePath
  manifest : FilePath

private def prepare (name : String) (path spec work : FilePath) : IO Part := do
  IO.FS.createDirAll work
  let placement ← readJson spec
  let digest ← IO.ofExcept (placement.getObjValAs? String "source_sha256")
  let src ← readBundle path work true false (some digest)
  let nodes ← IO.ofExcept (ESTreeUnits.checkedNodes src.program src.source)
  if let some reason := nodes.findSome? unsupportedStatement then throw (IO.userError reason)
  let some entry := path.fileName | throw (IO.userError "missing original filename")
  let s ← IO.ofExcept (parseResolvedSpec placement name entry nodes.size)
  let bundle := linkedLayoutBundle nodes (Effects.footprintUnits nodes)
  for (ast, i) in nodes.zipIdx do
    if let some why := entryPlacementProblem bundle s ast i then throw (IO.userError why)
  let plan ← planSplit bundle s false
  let files ← IO.ofExcept (ESTreeEmit.programs nodes bundle plan src.source)
  let manifest ← writeProgramManifest files work
  return ⟨⟨src.program, files, plan⟩, work, src.input, manifest⟩

private def comparePart (part other : Part) (shared : Json) (out : FilePath) : IO Unit := do
  -- The global directory contains both pieces. The other piece is checked
  -- against its own source manifest in the second call.
  let mut otherFiles := (← IO.ofExcept shared.getObj?).toList
  for m in other.chunk.plan.mods do
    let bytes ← IO.FS.readFile (out / m.name)
    otherFiles := (m.name, Json.mkObj [("sourceText", .str bytes)]) :: otherFiles
  compareDirectory part.manifest (Json.mkObj otherFiles) part.input out part.work

private def reparsed (part : Part) (out : FilePath) : IO LinkedInput.StaticSplit := do
  let mut files := []
  for m in part.chunk.plan.mods do
    let ast ← loadProgram (out / m.name) part.work true
    files := (m.name, ast) :: files
  return {part.chunk with files := Json.mkObj files}

private def inventory (app editor : Part) (shared : Json) (inputs : LinkedDeployment.Input)
    (access : LinkedDeployment.Access) : Except String Json := do
  let mut rows := []
  let mut base := 0
  let fixed := (← shared.getObj?).toList
  for (path, ast) in [(app.chunk.plan.entry, app.chunk.source), (editor.chunk.plan.entry, editor.chunk.source)] ++ fixed do
    let count := (← ESTreeUnits.nodes ast).size
    let referrer := if path == app.chunk.plan.entry then access.entryAccess else access.providers[path]?.getD {}
    let imports := referrer.imports.map fun i =>
      Json.mkObj [("path", toJson i.source), ("bindings", toJson i.names)]
    rows := (path, Json.mkObj [("firstUnit", toJson base), ("unitCount", toJson count),
      ("metadataUrl", toJson referrer.nativeUrl), ("loaderImports", toJson imports),
      ("unitCapabilities", toJson (referrer.unitNames.toList.mergeSort (fun a b => a.1 ≤ b.1)))]) :: rows
    base := base + count
  return Json.mkObj [("root", toJson app.chunk.plan.entry), ("linked", Json.mkObj rows),
    ("resources", Json.mkObj (inputs.before.toList.map fun (name,r) => (name, toJson r.effectiveUrl)))]

def main (args : List String) : IO Unit := do
  let [root, mainName, editorName, mainSpec, editorSpec, origin, work] := args
    | throw (IO.userError "expected bundle directory, two original filenames, two placements, origin and scratch directory")
  let root : FilePath := root
  let work : FilePath := work
  let app ← prepare "main" (root / mainName) mainSpec (work / "main")
  let editor ← prepare "editor" (root / editorName) editorSpec (work / "editor")
  let providers ← loadProviders app.chunk.source (root / mainName) work
  let table ← IO.ofExcept providers.getObj?
  unless table.get? editorName == some editor.chunk.source do throw (IO.userError "editor placement source differs from app dependency")
  let shared := Json.obj (table.erase editorName)
  let names := (app.chunk.plan.mods ++ editor.chunk.plan.mods).map (·.name)
  unless names.Nodup do throw (IO.userError "joint output path collision")
  for part in [app, editor] do
    for m in part.chunk.plan.mods do
      unless safeModulePath m.name do throw (IO.userError s!"unsafe output {m.name}")
      if m.name != part.chunk.plan.entry && (← (root / m.name).pathExists) then
        throw (IO.userError s!"output shadows original route: {m.name}")
  let inputs ← readDeployment app.chunk.source providers (root / mainName) work origin app.chunk.plan
    {nativeRecord := true, emptyMap := true}
  -- Hand the test the actual resource text the checker consumed, so its
  -- published SHA-256 inventory does not rely on a later filesystem read.
  for (name, resource) in inputs.before.toList do
    unless safeModulePath name do throw (IO.userError s!"unsafe resource {name}")
    let file := work / "snapshot" / name
    IO.FS.createDirAll (file.parent.getD work)
    IO.FS.writeFile file resource.source
  let access ← IO.ofExcept (LinkedDeployment.check inputs app.chunk.source providers app.chunk.plan)
  let inv ← IO.ofExcept (inventory app editor shared inputs access)
  IO.FS.writeFile (work / "inventory.json") inv.compress
  let out := work / "staged"
  IO.FS.createDir out
  for part in [app, editor] do javascript #["--print"] #[part.manifest, out, part.input]
  for (name, ast) in (← IO.ofExcept shared.getObj?).toList do
    IO.FS.createDirAll ((out / name).parent.getD out)
    IO.FS.writeFile (out / name) (← IO.ofExcept (ast.getObjValAs? String "sourceText"))
  comparePart app editor shared out
  comparePart editor app shared out
  let mainDisk ← reparsed app out
  let editorDisk ← reparsed editor out
  let _ ← IO.ofExcept (LinkedBehavior.certifyJointDeployment mainDisk editorDisk shared inputs .browserPage)
  IO.println "certified joint normal completion"
