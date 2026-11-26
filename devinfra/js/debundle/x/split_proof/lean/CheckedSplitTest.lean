import EsmSplit.CoreJS.CheckedSplit

open CoreJS CoreJS.CheckedSplit

private def entry := "entry.mjs"
private def worker := "worker.mjs"

private def source : Program := {
  entry
  modules := [{
    name := entry
    imports := []
    body := [
      (0, .decl .const "seed" (.num 42)),
      (1, .funDecl (.mk "read" .normal false [] [.ret (.var "seed")])),
      (2, .decl .const "result" (.call (.var "read") [])),
      (3, .decl .const "other" (.num 42))
    ]
    exports := [("result", "result")]
  }]
}

private def spec : EsmSplit.Spec := {
  entry
  modules := [(worker, [.name "seed", .name "read"]), (entry, [.stmt 2, .stmt 3])]
}

private def checkValue (p : Program) : IO Unit := do
  let some [("result", address)] := LinkedBehavior.exports World.probe p |
    throw (IO.userError "expected one public result cell")
  unless address = ⟨3, 0⟩ do throw (IO.userError "result resolves to the wrong live cell")
  let .ok () state := runProgram World.probe 16 p |
    throw (IO.userError "expected normal initialization")
  let .ok (.num value) _ := readCell address state |
    throw (IO.userError "expected the function's numeric result")
  unless value == 42 do throw (IO.userError s!"expected 42, got {value}")

def main : IO Unit := do
  let built ← IO.ofExcept (checkedSplit source spec .allOutcomes)
  let output := built.val
  unless requestedPlacement source spec == [(0, worker), (1, worker), (2, entry), (3, entry)] do
    throw (IO.userError "expected the authored owners")
  let some moved := output.find worker | throw (IO.userError "missing worker module")
  let some remaining := output.find entry | throw (IO.userError "missing entry module")
  unless moved.body.map Prod.fst == [0, 1] && remaining.body.map Prod.fst == [2, 3] do
    throw (IO.userError "the function and capture did not move as requested")
  unless decide (remaining.imports = [⟨worker, [("read", "read")]⟩]) do
    throw (IO.userError "the use must import the moved function")
  checkValue source
  checkValue output
  let _ ← IO.ofExcept (checkOutput built output)
  let normal ← IO.ofExcept (checkedSplit source spec .normalCompletion)
  let _ ← IO.ofExcept (checkOutput normal output)

  let rejected := { spec with modules := [(worker, [.name "read", .name "result"]), (entry, [.stmt 0, .stmt 3])] }
  for guarantee in [Guarantee.allOutcomes, .normalCompletion] do
    match checkedSplit source rejected guarantee with
    | .error "requested placement changes the source unit order" => pure ()
    | .error why => throw (IO.userError s!"unexpected refusal: {why}")
    | .ok _ => throw (IO.userError "the requested reorder was certified")

  let wrongCell := { output with modules := [moved, { remaining with exports := [("result", "other")] }] }
  let wrongFile := { output with modules := [{ moved with name := "other.mjs" }, remaining] }
  for changed in [wrongCell, wrongFile] do
    match checkOutput built changed with
    | .error "decoded output differs from the constructed modules" => pure ()
    | .error why => throw (IO.userError s!"unexpected output refusal: {why}")
    | .ok _ => throw (IO.userError "a changed output graph passed the check")
