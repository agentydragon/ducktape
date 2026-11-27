import EsmSplit.Effects
import EsmSplit.ESTreeUnits

open Lean

/-- Analyze each syntax witness as a separate module, with the same defaults as `lower`. -/
private def observe (program : Json) : Except String Json := do
  let asts ← ESTreeUnits.nodes program
  let units := Effects.footprintUnits asts
  let exported := asts.toList.flatMap Lower.exportedBindings
  let (fps, result) := Effects.footprints units (Effects.importsOf asts) {} exported.toArray
  let effectUnits := (fps.zipIdx.filter fun (f, _) => f.effect).map (·.2)
  return Json.mkObj [("effectUnits", toJson effectUnits), ("unknown", toJson result.unknown.toList.eraseDups),
    ("outOfBudget", toJson result.gaveUp)]

def main : IO Unit := do
  let input ← (← IO.getStdin).getLine
  let programs ← IO.ofExcept (Json.parse input >>= fromJson? (α := Array (String × Json)))
  let observations ← programs.toList.mapM fun (name, program) => do
    pure (name, ← IO.ofExcept ((observe program).mapError fun reason => s!"{name}: {reason}"))
  IO.println (Json.mkObj [("tableKeys", toJson (Effects.table.toList.map (·.1)).mergeSort),
    ("memberPaths", toJson Effects.memberPaths), ("witnesses", Json.mkObj observations)]).compress
