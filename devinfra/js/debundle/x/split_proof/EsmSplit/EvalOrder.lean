import EsmSplit.Plan
import Lean.Data.Json

/-!
# The model's ES module evaluation order, on a bare import graph

    evalorder <graph.json>     {"entry": "a.mjs", "modules": {"a.mjs": ["b.mjs", "c.mjs"], "b.mjs": [], …}}

Prints `Plan.evalOrder`, one module per line. `es_order/check.mjs` compares it with the order
in which Node runs the module bodies of random import graphs, cycles included.
-/

open EsmSplit

private def graphOrder (j : Lean.Json) : IO (List String) := do
  let entry ← IO.ofExcept (j.getObjVal? "entry" >>= Lean.Json.getStr?)
  let mods ← IO.ofExcept (j.getObjVal? "modules" >>= Lean.Json.getObj?)
  let modules ←
    mods.toList.mapM fun (name, v) => do
        let imports ←
          (← IO.ofExcept v.getArr?).toList.mapM fun x => do
              pure ((← IO.ofExcept x.getStr?), ([] : List Name))
        pure ({ name, stmts := [], imports, exports := [] } : Module)
  return ({ entry, mods := modules } : Plan).evalOrder

def main (args : List String) : IO Unit := do
  match args with
  | ["--batch", file] =>
    let graphs ← IO.ofExcept (Lean.Json.parse (← IO.FS.readFile file) >>= Lean.Json.getArr?)
    IO.println (Lean.toJson (← graphs.mapM graphOrder)).compress
  | [file] =>
    for name in ← graphOrder (← IO.ofExcept (Lean.Json.parse (← IO.FS.readFile file))) do
      IO.println name
  | _ =>
    throw (IO.userError "usage: evalorder [--batch] <graph.json>")
