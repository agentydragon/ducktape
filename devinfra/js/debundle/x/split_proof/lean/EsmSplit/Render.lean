import EsmSplit.Plan

namespace EsmSplit

/-- Blocks of lines with an empty line between them; an empty block is left out. -/
def renderBlocks (blocks : List (List String)) : String :=
  "\n\n".intercalate ((blocks.filter (!·.isEmpty)).map ("\n".intercalate ·)) ++ "\n"

/-- The glue `import`s of a module. A binding its source exports under another name is renamed back. -/
def Module.importLines (P : Plan) (m : Module) : List String :=
  m.imports.map fun (src, ns) =>
    if ns.isEmpty then s!"import \"./{src}\";"
    else
      let source := P.find src
      let spec (u : Name) :=
        let p := (source.map (·.publicName u)).getD u
        if p == u then u else s!"{p} as {u}"
      s!"import \{ {", ".intercalate (ns.map spec)} } from \"./{src}\";"

/-- The glue `export` of a module. -/
def Module.exportLines (m : Module) : List String :=
  if m.exports.isEmpty then [] else
    [s!"export \{ {", ".intercalate (m.exports.map fun (p, u) => if p == u then u else s!"{u} as {p}")} };"]

/-- Source text of a module: its `import`s, its statements verbatim, its `export`. -/
def Module.render (B : Bundle) (P : Plan) (m : Module) : String :=
  renderBlocks [m.importLines P, m.stmts.map fun i => (B.stmt i).src, m.exportLines]

end EsmSplit
