import EsmSplit.CoreJS.CheckedMoveSplit

open CoreJS CoreJS.CheckedMoveSplit

private def entry := "entry.mjs"
private def left := "left.mjs"
private def right := "right.mjs"

-- `certified_split_test`'s reordered declaration fixture (input 8ccb05394d872465).
private def source (second : Expr := .num 2) : Program := {
  entry
  modules := [{
    name := entry
    imports := []
    body := [
      (0, .decl .const "a" (.num 1)),
      (1, .decl .const "b" second),
      (2, .decl .const "result" (.var "a"))
    ]
    exports := [("result", "result")]
  }]
}

private def spec : EsmSplit.Spec := {
  entry
  modules := [(entry, [.stmt 2]), (left, [.stmt 0]), (right, [.stmt 1])]
  order := [right, left]
  sequential := true
}

def main : IO Unit := do
  let built ← IO.ofExcept (checkedMoveSplit source spec .normalCompletion)
  let output := built.split.program
  unless CheckedSplit.requestedPlacement source spec == [(2, entry), (0, left), (1, right)] do
    throw (IO.userError "the requested owners changed")
  unless output.evalOrder == [right, left, entry] do
    throw (IO.userError "the requested module order changed")
  let some leftModule := output.find left | throw (IO.userError "missing left module")
  let some rightModule := output.find right | throw (IO.userError "missing right module")
  let some entryModule := output.find entry | throw (IO.userError "missing entry module")
  unless rightModule.body.map Prod.fst == [1] && leftModule.body.map Prod.fst == [0] &&
      entryModule.body.map Prod.fst == [2] do
    throw (IO.userError "the constructed units have the wrong owners")
  let some before := LinkedBehavior.exports World.probe source | throw (IO.userError "source exports failed")
  let some after := LinkedBehavior.exports World.probe output | throw (IO.userError "output exports failed")
  unless decide (before = after) do throw (IO.userError "the public cells differ")
  let some [("result", resultCell)] := some before | throw (IO.userError "expected one result cell")
  for program in [source, output] do
    let .ok () state := runProgram World.probe 16 program | throw (IO.userError "expected a normal run")
    let .ok (.num value) _ := readCell resultCell state | throw (IO.userError "expected a numeric result")
    unless value == 1 do throw (IO.userError "the result changed")
  match checkedMoveSplit source spec .allOutcomes with
  | .error "commuting moves prove only normal-completion" => pure ()
  | _ => throw (IO.userError "the unsupported guarantee was not refused")
  match checkedMoveSplit (source (.var "a")) spec .normalCompletion with
  | .error _ => pure ()
  | .ok _ => throw (IO.userError "an interfering read moved ahead of its binding")
  let wrong := { output with modules := output.modules.map fun m =>
    if m.name == entry then { m with exports := [("result", "a")] } else m }
  let _ ← IO.ofExcept (CheckedSplit.checkOutput built.split output)
  match CheckedSplit.checkOutput built.split wrong with
  | .error _ => pure ()
  | .ok _ => throw (IO.userError "a changed export binding passed the output check")
