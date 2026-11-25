import EsmSplit.CoreJS.Run
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.Fragment

/-- Count the individual syntactic candidates for `MayMove`. `other` means that
neither inert nor allocation-only applies, not that execution has a known effect.
Opaque is an overlapping count; refused units stay in the denominator. -/
private def moveCensus (source : Lean.Json) (profile : CoreJS.HostProfile) : Except String Lean.Json := do
  let units ← CoreJS.LinkedInput.censusUnits source profile
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
  return Lean.Json.mkObj [("hostProfile", Lean.toJson profile.label), ("units", Lean.toJson units.size), ("inert", Lean.toJson inert),
    ("allocate", Lean.toJson allocate), ("opaque", Lean.toJson opaqueCount), ("other", Lean.toJson other),
    ("refused", Lean.toJson refused), ("may_move", Lean.toJson (inert + allocate)),
    ("may_move_denominator", Lean.toJson units.size), ("problems", Lean.Json.arr problems)]

private def loweringProfile (j : Lean.Json) : Except String CoreJS.HostProfile := do
  if j == Lean.Json.mkObj [("profile", .str "empty")] then return .empty
  if j == Lean.Json.mkObj [("profile", .str "ecmascript")] then return .ecmascript
  CoreJS.HostProfile.read j

private def runLoweringCensus (file : String) (profile : CoreJS.HostProfile) : IO UInt32 := do
  match Lean.Json.parse (← IO.FS.readFile file) >>= (CoreJS.LinkedInput.concreteCensus · profile) with
  | .ok report => IO.println report.compress; pure 0
  | .error why => IO.eprintln why; pure 1

/-- Census-only selection of production profiles and the two stable-intrinsic
contracts accepted by `lowersplit`. Stable-value premises do not change the
syntactic `MayMove` rule. Node recording is selected only by a separate flag. -/
private def censusProfile (j : Lean.Json) : Except String CoreJS.HostProfile := do
  let obj := Lean.Json.mkObj
  if j == obj [("profile", .str "empty")] then return .empty
  if j == obj [("profile", .str "ecmascript")] ||
      j == obj [("profile", .str "ecmascript"),
        ("stableIntrinsicValues", Lean.toJson (["Object.defineProperty@1"] : List String))] then
    return .ecmascript
  if j == obj [("profile", .str "browser-page"),
      ("stableIntrinsicValues", Lean.toJson (["Math.PI@1"] : List String))] then
    return .browserPage
  loweringProfile j

private def runCensus (file : String) (profile : CoreJS.HostProfile) : IO UInt32 := do
  match Lean.Json.parse (← IO.FS.readFile file) >>= (moveCensus · profile) with
  | .ok report => IO.println report.compress; pure 0
  | .error why => IO.eprintln why; pure 1

/-- Replay uses the recorded Node host; the census defaults to the empty production profile. -/
def main (args : List String) : IO UInt32 := do
  match args with
  | ["--moves-ast", file] => runCensus file .empty
  | ["--moves-ast", file, "--node-recording"] => runCensus file .nodeRecording
  | ["--moves-ast", file, "--host-contract", contract] =>
    match Lean.Json.parse (← IO.FS.readFile contract) >>= censusProfile with
    | .ok profile => runCensus file profile
    | .error why => IO.eprintln why; pure 1
  | ["--lowering-census", file] => runLoweringCensus file .empty
  | ["--lowering-census", file, "--host-contract", contract] =>
    match Lean.Json.parse (← IO.FS.readFile contract) >>= loweringProfile with
    | .ok profile => runLoweringCensus file profile
    | .error why => IO.eprintln why; pure 1
  | ["--ast", file] =>
    for line in (← IO.FS.lines file) do
      IO.println (CoreJS.runLine 100000 line fun j scripts =>
        CoreJS.LinkedInput.program j ((j.getObjVal? "providers").toOption.getD (Lean.Json.mkObj []))
          (Std.HashSet.ofList (scripts.map Prod.fst)) ((j.getObjValAs? String "entry").toOption.getD "main"))
    pure 0
  | _ => CoreJS.main args
