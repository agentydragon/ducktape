import EsmSplit.CoreJS.TerminalSource

open CoreJS CoreJS.TerminalSource

private def statements : List Stmt :=
  ((List.range 16).map fun i => .decl .const s!"data{i}" (.str s!"value{i}")) ++
  ((List.range 16).map fun i => .decl .const s!"reader{i}" (.var s!"data{i}"))
private def row : EsmSplit.LocalExportUnit :=
  ⟨32, (List.range 16).map fun i => (s!"reader{i}", s!"reader{i}")⟩
private def source : Module := ⟨"entry.mjs", [], (List.range 32).zip statements, row.names⟩
private def program : Program := ⟨"entry.mjs", [source]⟩

private def refused (input : Program × List EsmSplit.LocalExportUnit) (expected : String) : IO Unit := do
  match check input with
  | .ok _ => throw (IO.userError s!"unexpected acceptance: {expected}")
  | .error actual => unless actual == expected do throw (IO.userError s!"expected {expected}, got {actual}")

def main : IO Unit := do
  let .ok checked := check (program, [row]) | throw (IO.userError "terminal source refused")
  unless checked.row.ordinal == 32 && checked.row.names == row.names do
    throw (IO.userError "original row changed")
  let extra : Module := { source with exports := source.exports ++ [("inline", "inline")] }
  refused (⟨"entry.mjs", [extra]⟩, [row]) "local export row does not cover the module public surface"
  refused (program, [row, ⟨33, []⟩]) "expected one source module and exactly one local export row"
  refused (program, []) "expected one source module and exactly one local export row"
  refused (⟨"other.mjs", [source]⟩, [row]) "singleton source is not the entry module"
  let empty : Module := ⟨"entry.mjs", [], [], []⟩
  let .ok _ := check (⟨"entry.mjs", [empty]⟩, [⟨0, []⟩]) |
    throw (IO.userError "single empty export row refused")
  let sparse : Module := ⟨"entry.mjs", [], [(0, .decl .const "seed" (.num 42)),
    (2, .decl .const "result" (.var "seed"))], [("exposed", "seed")]⟩
  refused (⟨"entry.mjs", [sparse]⟩, [⟨1, sparse.exports⟩])
    "expected dense executable original IDs followed by one terminal metadata unit"
