import EsmSplit.ESTreeUnits
import EsmSplit.CoreJS.ModuleInput
import EsmSplit.ModulePath

namespace EsmSplit.SplitFiles

open Lean

/-- A certification refusal keeps user-facing diagnostics and structured probe details together
until the CLI emits its verdict. -/
structure CertificationRefusal where
  reason : String
  reasonCode : Option String := none
  observation : Option Json := none
  details : List (String × Json) := []

/-- Optional stderr timing keeps stdout available for the certification verdict. -/
def timePhase (enabled : Bool) (phase : String) (action : Unit → IO α) : IO α := do
  unless enabled do return ← action ()
  let start ← IO.monoMsNow
  try
    action ()
  finally
    let elapsed := (← IO.monoMsNow) - start
    IO.eprintln (Json.mkObj [("phase", toJson phase), ("elapsed_ms", toJson elapsed)]).compress

/-- Run the shared Acorn frontend and ESTree printer. Bazel supplies the runfile and
the native Lake binary locates the same entry point beside the project. -/
def javascript (flags : Array String) (paths : Array System.FilePath) : IO Unit := do
  let cwd ← IO.currentDir
  let args := flags ++ paths.map fun p => (if p.isAbsolute then p else cwd / p).toString
  let frontend ← IO.getEnv "SPLIT_PROOF_MODULE_IO"
  let proc ← match frontend with
    | some exe => IO.Process.output { cmd := exe, args }
    | none => do
      let mut root ← IO.appPath
      for _ in [:5] do root := root.parent.getD "."
      IO.Process.output { cmd := "node", args := #[toString (root / "translation" / "module_io.mjs")] ++ args }
  unless proc.exitCode == 0 do throw (IO.userError s!"JavaScript frontend failed: {proc.stderr.trimAscii}")

def readJson (path : System.FilePath) : IO Json := do
  IO.ofExcept (Json.parse (← IO.FS.readFile path))

def loadProgram (path work : System.FilePath) (validateSnapshot : Bool) (sourceHash : Option String := none) : IO Json := do
  if path.extension == some "js" || path.extension == some "mjs" then
    let parsed := work / "source.json"
    let flags := sourceHash.elim #["--parse"] fun digest => #["--parse-sha256", digest]
    javascript flags #[path, parsed]
    return ← readJson parsed
  if sourceHash.isSome then throw (IO.userError "resolved placements require JavaScript source bytes")
  if validateSnapshot then
    let parsed := work / "source.json"
    javascript #["--load-input"] #[path, parsed]
    return ← readJson parsed
  let program ← readJson path
  match program.getObjVal? "annotationVersion" with
  | .ok v =>
    unless v == toJson 2 do throw (IO.userError s!"unsupported annotationVersion: {v}")
    return program
  | .error _ =>
    let upgraded := work / "upgraded.json"
    javascript #["--load-input"] #[path, upgraded]
    readJson upgraded

def writeProgramManifest (programs : Json) (work : System.FilePath) : IO System.FilePath := do
  let manifest := work / "output-programs.json"
  IO.FS.writeFile manifest programs.compress
  return manifest

def readModules (root work : System.FilePath) (requests : Array String) : IO Json := do
  let names := work / "requests.json"
  let output := work / "providers.json"
  IO.FS.writeFile names (toJson requests).compress
  javascript #["--read-modules"] #[root, names, output]
  readJson output

/-- Walk the root's static imports, reusing the programs already parsed for supplied chunks. -/
def loadProviders (entry : String) (originals : Std.HashMap String Json)
    (root : Option System.FilePath) (work : System.FilePath) (allowEntryImport : Bool := true) : IO (List (String × Json)) := do
  let some ast := originals[entry]? | throw (IO.userError s!"missing entry program: {entry}")
  let mut providers := #[]
  let mut seen := Std.HashSet.ofList [entry]
  let mut frontier := #[(entry, ast)]
  while !frontier.isEmpty do
    let mut requests := #[]
    for (name, ast) in frontier do
      for node in ← IO.ofExcept (ast.getObjValAs? (Array Json) "body") do
        if (node.getObjValAs? String "type").toOption == some "ImportDeclaration" then
          let source ← IO.ofExcept (CoreJS.ModuleInput.importSource name node)
          unless source.endsWith ".mjs" || source.endsWith ".js" do
            throw (IO.userError s!"expected a JavaScript module dependency, got {source}")
          if !allowEntryImport && source == entry then
            throw (IO.userError s!"unsupported request back to the source entry: {entry}")
          unless seen.contains source do
            seen := seen.insert source
            requests := requests.push source
    if requests.isEmpty then break
    let missing := requests.filter (!originals.contains ·)
    let parsed ← if missing.isEmpty then pure (Json.mkObj []) else do
      let some root := root | throw (IO.userError "supply the original JavaScript file to resolve its relative imports")
      readModules root work missing
    frontier ← requests.mapM fun name => do
      let ast ← match originals[name]? with
        | some ast => pure ast
        | none => IO.ofExcept (parsed.getObjVal? name)
      pure (name, ast)
    providers := providers ++ frontier
  return providers.toList

/-- Planned modules may replace their own original entry only. The remaining
paths must be fresh even if an original asset is outside the static graph. -/
def checkNewRoutes (root work : System.FilePath) (paths : List String) : IO Unit := do
  let names := work / "new-routes.json"
  IO.FS.writeFile names (toJson paths).compress
  javascript #["--check-new-routes"] #[root, names]

/-- Preserve module headers and compare the same ordered declaration units used by the linker. -/
private def unitProgram (ast : Json) : Except String Json := do
  let units ← ESTreeUnits.nodes ast
  return .obj ((← ast.getObj?).insert "body" (.arr units))

/-- Publication renames a sibling staging directory onto this path. Trailing separators and `.`
segments name the same directory. A path without a final directory name has no parent to stage in. -/
def outputDirectory (out : System.FilePath) : Except String System.FilePath := do
  let parts := ((out.toString.splitOn "/").reverse.dropWhile fun p => p.isEmpty || p == ".").reverse
  let dir : System.FilePath := ⟨"/".intercalate parts⟩
  unless dir.fileName.isSome do throw s!"output path {out} must end in a directory name"
  return dir

structure OutputPart where
  programs : Json
  source : String
  input : System.FilePath
  work : System.FilePath

/-- Reparse the complete tree, comparing its syntax and preserving the
observed scope annotations for the certificate. Copied providers must have
the exact source text that supplied the original graph. -/
private def compareDirectory (parts : List OutputPart) (providers : List (String × String))
    (dir work : System.FilePath) : IO Json := do
  let manifest := work / "deployment-programs.json"
  let descriptors := parts.map fun part => Json.mkObj [("programs", part.programs), ("source", toJson part.source)]
  let copied := Json.mkObj (providers.map fun (name, source) => (name, .str source))
  IO.FS.writeFile manifest (Json.mkObj [("parts", toJson descriptors), ("providers", copied)]).compress
  let checked := work / "checked-output.json"
  javascript #["--check-output"] #[manifest, dir, checked]
  let observed ← readJson checked
  for pair in ← IO.ofExcept (observed.getObjValAs? (Array Json) "grouped") do
    let name ← IO.ofExcept (pair.getObjValAs? String "name")
    let planned ← IO.ofExcept (pair.getObjVal? "planned" >>= unitProgram)
    let actual ← IO.ofExcept (pair.getObjVal? "observed" >>= unitProgram)
    unless planned == actual do throw (IO.userError s!"output module {name} differs from its generated ESTree")
  let files ← IO.ofExcept (observed.getObjVal? "files" >>= Json.getObj?)
  return Json.mkObj (← files.toList.mapM fun (name, ast) => do
    return (name, ← IO.ofExcept (unitProgram ast)))

/-- Finish comparison and proof before publishing the complete tree by rename.
Existing trees use the same gate and are left untouched. -/
def certifyOutput {α : Type} (parts : List OutputPart) (providers : Json)
    (out work : System.FilePath) (checkOnly timings : Bool)
    (check : Json → IO (Except CertificationRefusal α)) : IO (Except CertificationRefusal α) := do
  let groups ← IO.ofExcept <| parts.mapM fun part => do
    return (← part.programs.getObj?).toList.map Prod.fst
  let dependencies ← IO.ofExcept (providers.getObj?)
  let names := groups.flatten ++ dependencies.toList.map Prod.fst
  IO.ofExcept (EsmSplit.validateModulePaths names)
  unless names.Nodup do throw (IO.userError "output module path collision")
  let routes := Std.HashSet.ofList names
  for name in names do
    let mut ancestor := ""
    for segment in (name.splitOn "/").dropLast do
      ancestor := if ancestor.isEmpty then segment else ancestor ++ "/" ++ segment
      if routes.contains ancestor then throw (IO.userError s!"output module {name} crosses output file {ancestor}")
  let sources ← dependencies.toList.mapM fun (name, ast) => do
    return (name, ← IO.ofExcept (ast.getObjValAs? String "sourceText"))
  let verify dir := do
    let observed ← timePhase timings "output_comparison" fun _ => compareDirectory parts sources dir work
    check observed
  if checkOnly then return ← verify out
  let parent := out.parent.getD "."
  IO.FS.createDirAll parent
  let stage := parent / (".split-proof-" ++ work.fileName.get!)
  IO.FS.createDir stage
  try
    timePhase timings "output_staging" fun _ => do
      for part in parts do
        let manifest ← writeProgramManifest part.programs part.work
        javascript #["--print"] #[manifest, stage, part.input]
      for (name, source) in sources do
        let dest := stage / name
        IO.FS.createDirAll (dest.parent.getD stage)
        IO.FS.writeFile dest source
    let result ← verify stage
    if result.isOk then timePhase timings "output_publication" fun _ => IO.FS.rename stage out
    return result
  finally
    if ← stage.pathExists then IO.FS.removeDirAll stage

end EsmSplit.SplitFiles
