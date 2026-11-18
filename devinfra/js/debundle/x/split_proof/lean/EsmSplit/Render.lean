import EsmSplit.Plan

namespace EsmSplit

/-- Source text of a module: its `import`s, its statements verbatim, its `export`. -/
def Module.render (B : Bundle) (m : Module) : String :=
  let imps := m.imports.map fun (src, ns) =>
    if ns.isEmpty then s!"import \"./{src}\";"
    else s!"import \{ {", ".intercalate ns} } from \"./{src}\";"
  let body := m.stmts.map fun i => (B.stmt i).src
  let exps := if m.exports.isEmpty then [] else [s!"export \{ {", ".intercalate m.exports} };"]
  let blocks := [imps, body, exps].filter (!·.isEmpty)
  "\n\n".intercalate (blocks.map ("\n".intercalate ·)) ++ "\n"

end EsmSplit
