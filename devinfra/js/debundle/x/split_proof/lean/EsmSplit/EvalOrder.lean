import Lean.Data.Json
import EsmSplit.Plan

/-!
# The model's ES module evaluation order, on a bare import graph

    evalorder <graph.json>     {"entry": "a.mjs", "modules": {"a.mjs": ["b.mjs", "c.mjs"], "b.mjs": [], …}}

Prints `Plan.evalOrder`, one module per line. `es_order/check.mjs` compares it with the order
in which Node runs the module bodies of random import graphs, cycles included.
-/

open EsmSplit

def main (args : List String) : IO Unit := do
  let [path] := args | throw (IO.userError "usage: evalorder <graph.json>")
  let j ← IO.ofExcept (Lean.Json.parse (← IO.FS.readFile path))
  let entry ← IO.ofExcept (j.getObjVal? "entry" >>= Lean.Json.getStr?)
  let mods ← IO.ofExcept (j.getObjVal? "modules" >>= Lean.Json.getObj?)
  let modules ← mods.toList.mapM fun (name, v) => do
    let imports ← (← IO.ofExcept v.getArr?).toList.mapM fun x => do pure ((← IO.ofExcept x.getStr?), ([] : List Name))
    pure ({ name, stmts := [], imports, exports := [] } : Module)
  for m in ({ entry, mods := modules } : Plan).evalOrder do IO.println m
