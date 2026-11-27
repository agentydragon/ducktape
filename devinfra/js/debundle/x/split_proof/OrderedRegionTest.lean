import EsmSplit.CoreJS.OrderedRegion
import EsmSplit.CoreJS.StateMatch

open CoreJS CoreJS.LinkedBehavior CoreJS.OrderedRegion

/-! An actual plan with a closure capturing an earlier binding, multiple
units in one provider, a called closure, and a throwing initializer. -/
private def statements : List Stmt := [
  .decl .const "base" (.num 7),
  .decl .const "offset" (.unary .neg (.var "base")),
  .decl .const "read" (.func (.mk "" .arrow false [] [.ret (.var "offset")])),
  .decl .const "result" (.call (.var "read") []),
  .decl .const "failure" (.get .null (.str "x"))]

private def bundle : EsmSplit.Bundle := [
  { src := "", defs := ["base"], effect := true },
  { src := "", defs := ["offset"], eager := ["base"], effect := true },
  { src := "", defs := ["read"], eager := ["offset"], effect := true },
  { src := "", defs := ["result"], eager := ["read"], exports := [("publicResult", some "result")], effect := true },
  { src := "", defs := ["failure"], effect := true }]

private def spec : EsmSplit.Spec := {
  entry := "entry.mjs"
  modules := [("part1.mjs", [.stmt 0, .stmt 1]),
    ("part2.mjs", [.stmt 2]), ("entry.mjs", [.stmt 3, .stmt 4])]
  order := ["part1.mjs", "part2.mjs"]
  sequential := true
}

private def test : IO Unit := do
  let plan := EsmSplit.mkPlan bundle spec
  unless decide (EsmSplit.Valid bundle spec plan) do throw (IO.userError "plan invalid")
  unless decide (FootprintsOfSyntax statements bundle) do throw (IO.userError "incorrect source footprints")
  unless decide (OrderKept statements plan) do throw (IO.userError "source order not kept")
  let original : Program := ⟨"original.mjs", [⟨"original.mjs", [],
    (List.range statements.length).zip statements, [("publicResult", "result")]⟩]⟩
  let output := splitProgram statements bundle plan
  unless exports World.probe original == some [("publicResult", ⟨4, 0⟩)] &&
      exports World.probe output == some [("publicResult", ⟨4, 0⟩)] do
    throw (IO.userError "public result no longer resolves to its original live cell")
  unless output.evalOrder == ["part1.mjs", "part2.mjs", "entry.mjs"] do
    throw (IO.userError "module order changed")
  let some first := output.find "part1.mjs" | throw (IO.userError "missing first provider")
  let some second := output.find "part2.mjs" | throw (IO.userError "missing second provider")
  let some entry := output.find "entry.mjs" | throw (IO.userError "missing entry")
  unless first.body.map Prod.fst == [0, 1] && second.body.map Prod.fst == [2] &&
      entry.body.map Prod.fst == [3, 4] do throw (IO.userError "unit IDs or owners changed")
  let .throw error originalState := runProgram World.probe 64 original |
    throw (IO.userError "original did not throw")
  let .throw error' outputState := runProgram World.probe 64 output |
    throw (IO.userError "split did not throw")
  letI : DecidableEq World.probe.σ := by change DecidableEq Unit; infer_instance
  unless decide (error = error') && (StateMatch.state originalState outputState).isSome do
    throw (IO.userError "throw or partial state changed")
  let .ok (.num value) _ := readCell ⟨4, 0⟩ originalState |
    throw (IO.userError "closure did not initialize the result")
  unless value == -7 do throw (IO.userError "closure captured the wrong cell")
  let reversed : EsmSplit.Spec := { spec with order := ["part2.mjs", "part1.mjs"] }
  unless !decide (OrderKept statements (EsmSplit.mkPlan bundle reversed)) do
    throw (IO.userError "inverted authored order unexpectedly preserved source order")

def main : IO Unit := test
