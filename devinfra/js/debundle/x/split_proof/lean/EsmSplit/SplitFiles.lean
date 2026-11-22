import EsmSplit.ESTreeEmit
import EsmSplit.CoreJS.ModuleInput

namespace EsmSplit.SplitFiles

open Lean

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

def loadProgram (path work : System.FilePath) (validateSnapshot : Bool) : IO Json := do
  if path.extension == some "js" || path.extension == some "mjs" then
    let parsed := work / "source.json"
    javascript #["--parse"] #[path, parsed]
    return ← readJson parsed
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

/-- Read the actual provider graph. Lean resolves each edge from the parsed import declaration;
the shared JavaScript frontend reads only the next batch of filenames. -/
partial def loadProviders (source : Json) (bundlePath work : System.FilePath) : IO Json := do
  let mut providers := #[]
  let mut seen : Std.HashSet String := {}
  let mut frontier := #[("bundle.mjs", source)]
  while !frontier.isEmpty do
    let mut requests := #[]
    for (name, ast) in frontier do
      for node in ← IO.ofExcept (ast.getObjValAs? (Array Json) "body") do
        if Lower.typ node == "ImportDeclaration" then
          let imp ← IO.ofExcept (CoreJS.ModuleInput.importDecl name node)
          unless imp.source.endsWith ".mjs" || imp.source.endsWith ".js" do
            throw (IO.userError s!"expected a JavaScript module dependency, got {imp.source}")
          unless seen.contains imp.source do
            seen := seen.insert imp.source
            requests := requests.push imp.source
    if requests.isEmpty then break
    unless bundlePath.extension == some "mjs" || bundlePath.extension == some "js" do
      throw (IO.userError "supply the original JavaScript file to resolve its relative imports")
    let names := work / "requests.json"
    let output := work / "providers.json"
    IO.FS.writeFile names (toJson requests).compress
    javascript #["--read-modules"] #[bundlePath, names, output]
    let parsed ← readJson output
    frontier := #[]
    for name in requests do
      let ast ← IO.ofExcept (parsed.getObjVal? name)
      providers := providers.push (name, ast)
      frontier := frontier.push (name, ast)
  return Json.mkObj providers.toList

/-- Certify the exact program map handed to the printer or compared with an existing directory. -/
def certifyOutput (providers : Json) (B : Bundle) (P : Plan)
    (out work input : System.FilePath) (checkOnly : Bool) (asts : Array Json) (source : String)
    (check : Json → IO Unit) : IO Unit := do
  let dependencies := (← IO.ofExcept providers.getObj?).toList
  for (name, _) in dependencies do
    if (P.find name).isSome then throw (IO.userError s!"provider {name} collides with a planned output module")
  let programs ← IO.ofExcept (ESTreeEmit.programs asts B P source)
  check programs
  let manifest ← writeProgramManifest programs work
  if checkOnly then
    let providerManifest := work / "provider-programs.json"
    IO.FS.writeFile providerManifest providers.compress
    javascript #["--check-output"] #[manifest, input, providerManifest, out]
  else
    let parent := out.parent.getD "."
    IO.FS.createDirAll parent
    -- A sibling directory makes publication a single same-filesystem rename.
    let stage := parent / (".split-proof-" ++ work.fileName.get!)
    IO.FS.createDir stage
    try
      javascript #["--print"] #[manifest, stage, input]
      for (name, ast) in dependencies do
        let dest := stage / name
        IO.FS.createDirAll (dest.parent.getD stage)
        IO.FS.writeFile dest (← IO.ofExcept (ast.getObjValAs? String "sourceText"))
      IO.FS.rename stage out
    finally
      if ← stage.pathExists then IO.FS.removeDirAll stage

end EsmSplit.SplitFiles
