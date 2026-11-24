import EsmSplit.LowerSplit.Input
import Std.Data.HashSet

/-! Time the planner on renamed copies of a public library's footprints. -/

open EsmSplit EsmSplit.LowerSplit

private def replicateBundle (K : Nat) (B : Bundle) : Bundle :=
  let declared : Std.HashSet String := Std.HashSet.ofList (B.flatMap (·.defs))
  (List.range K).flatMap fun k =>
    let r (n : String) := if declared.contains n then s!"{n}${k}" else n
    B.map fun s => { s with defs := s.defs.map r, eager := s.eager.map r, lazy := s.lazy.map r,
                            exports := s.exports.map fun (p, l) => (s!"{p}${k}", l.map r) }

def main (args : List String) : IO UInt32 := do
  let [dump, spec, count] := args | throw (IO.userError "usage: replicate_plan bundle.json spec.json copies")
  let some copies := count.toNat? | throw (IO.userError "expected a positive copy count")
  if copies == 0 then throw (IO.userError "expected a positive copy count")
  let start ← IO.monoMsNow
  let program ← SplitFiles.readJson dump
  let source ← IO.ofExcept (program.getObjValAs? String "sourceText")
  let asts ← IO.ofExcept (ESTreeUnits.checkedNodes program source)
  let read ← reportElapsed "read + parse JSON" start
  let (fps, _) ← analyzeFootprints asts (Effects.footprintUnits asts) {} false
  let _ ← reportElapsed "footprints (Lean analysis)" read
  let bundle := replicateBundle copies (bundleOfFootprints asts fps #[])
  let placement ← loadSpec spec
  let planning ← IO.monoMsNow
  let plan ← planSplit bundle placement false
  let checking ← reportElapsed "mkPlan" planning
  let ok ← reportPlanChecks bundle placement plan
  let _ ← reportElapsed "all checks" checking
  unless ok do IO.eprintln "planner rejected"
  return if ok then 0 else 1
