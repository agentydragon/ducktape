import EsmSplit.Plan
import EsmSplit.ModulePath

/-! Render the statement text embedded in the kernel example and MiniJS proof fixture. -/
open EsmSplit

namespace FixtureRender

/-- Blocks of lines with an empty line between them; an empty block is left out. -/
private def renderBlocks (blocks : List (List String)) : String :=
  "\n\n".intercalate ((blocks.filter (!·.isEmpty)).map ("\n".intercalate ·)) ++ "\n"

/-- The glue `import`s of a module. A binding its source exports under another name is renamed back. -/
private def importLines (P : Plan) (m : Module) : List String :=
  m.imports.map fun (src, ns) =>
    -- Module destinations contain only URL-safe path segments, so no JS escapes are needed.
    let specifier := "\"" ++ relativeUrlSpecifier m.name src ++ "\""
    if ns.isEmpty then s!"import {specifier};"
    else
      let source := P.find src
      let spec (u : Name) :=
        let p := (source.map (·.publicName u)).getD u
        if p == u then u else s!"{p} as {u}"
      s!"import \{ {", ".intercalate (ns.map spec)} } from {specifier};"

/-- The glue `export` of a module. -/
private def exportLines (m : Module) : List String :=
  if m.exports.isEmpty then [] else
    [s!"export \{ {", ".intercalate (m.exports.map fun (p, u) => if p == u then u else s!"{u} as {p}")} };"]

/-- Source text of a module: its `import`s, its statements verbatim, its `export`. -/
def render (B : Bundle) (P : Plan) (m : Module) : String :=
  renderBlocks [importLines P m, m.stmts.map fun i => (B.stmt i).src, exportLines m]

def writeModules (B : Bundle) (P : Plan) (dir : System.FilePath) : IO Unit := do
  IO.ofExcept (validateModulePaths (P.mods.map (·.name)))
  IO.FS.createDirAll dir
  for m in P.mods do
    let output := dir / m.name
    let parent := "/".intercalate ((m.name.splitOn "/").dropLast)
    if !parent.isEmpty then IO.FS.createDirAll (dir / parent)
    IO.FS.writeFile output (render B P m)
  IO.println s!"  wrote {P.mods.length} modules to {dir}"

end FixtureRender
