import Experiments.Lower

open Lean Lower

/-- `lower <bundle.json>`: one JSON footprint per top-level statement, one per line. -/
def main (args : List String) : IO UInt32 := do
  let [path] := args | IO.eprintln "usage: lower <bundle.json>"; return 2
  let j ← IO.ofExcept (Json.parse (← IO.FS.readFile path))
  let stmts := items (fld j "statements")
  let fps := finalize (stmts.map fun s => analyzeTop (fld s "ast"))
  for f in fps do IO.println f.toJson.compress
  return 0
