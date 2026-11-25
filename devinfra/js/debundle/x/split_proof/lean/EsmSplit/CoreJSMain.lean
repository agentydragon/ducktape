import EsmSplit.CoreJS.Run
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.Fragment

/-- Count the individual syntactic candidates for `MayMove`. `other` means that
neither inert nor allocation-only applies, not that execution has a known effect.
Opaque is an overlapping count; refused units stay in the denominator. -/
private def moveCensus (source : Lean.Json) : Except String Lean.Json := do
  let units ← CoreJS.LinkedInput.censusUnits source
  let mut inert : Nat := 0
  let mut allocate : Nat := 0
  let mut opaqueCount : Nat := 0
  let mut other : Nat := 0
  let mut refused : Nat := 0
  let mut problems : Array Lean.Json := #[]
  for (i, result) in units do
    match result with
    | .error why =>
      refused := refused + 1
      if problems.size < 20 then problems := problems.push (Lean.Json.str s!"{i}: {why}")
    | .ok stmt =>
      if let .opaque .. := stmt then opaqueCount := opaqueCount + 1
      if stmt.inert then inert := inert + 1
      else if stmt.allocs.isSome then allocate := allocate + 1
      else other := other + 1
  return Lean.Json.mkObj [("units", Lean.toJson units.size), ("inert", Lean.toJson inert),
    ("allocate", Lean.toJson allocate), ("opaque", Lean.toJson opaqueCount), ("other", Lean.toJson other),
    ("refused", Lean.toJson refused), ("may_move", Lean.toJson (inert + allocate)),
    ("may_move_denominator", Lean.toJson units.size), ("problems", Lean.Json.arr problems)]

/-- AST modes use the linked lowering under the recorded Node host. Other modes use `CoreJS.main`. -/
def main (args : List String) : IO UInt32 := do
  match args with
  | ["--moves-ast", file] =>
    match Lean.Json.parse (← IO.FS.readFile file) >>= moveCensus with
    | .ok report => IO.println report.compress; pure 0
    | .error why => IO.eprintln why; pure 1
  | ["--ast", file] =>
    for line in (← IO.FS.lines file) do
      IO.println (CoreJS.runLine 100000 line fun j scripts =>
        CoreJS.LinkedInput.program j ((j.getObjVal? "providers").toOption.getD (Lean.Json.mkObj []))
          (Std.HashSet.ofList (scripts.map Prod.fst)) ((j.getObjValAs? String "entry").toOption.getD "main"))
    pure 0
  | _ => CoreJS.main args
