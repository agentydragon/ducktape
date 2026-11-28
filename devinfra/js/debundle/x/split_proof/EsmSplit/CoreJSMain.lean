import EsmSplit.CoreJS.Fragment
import EsmSplit.CoreJS.LinkedInput
import EsmSplit.CoreJS.LinkedInputCensus
import EsmSplit.CoreJS.Run
import EsmSplit.CoreJS.StableIntrinsicDescriptor

/-- Count the individual syntactic candidates for `MayMove`. `other` means that
neither inert nor allocation-only applies, not that execution has a known effect.
Opaque is an overlapping count; refused units stay in the denominator. -/
private def moveCensus (source : Lean.Json) (profile : CoreJS.HostProfile) :
    Except String Lean.Json := do
  let units ← CoreJS.LinkedInputCensus.censusUnits source profile
  let mut inert : Nat := 0
  let mut allocate : Nat := 0
  let mut opaqueCount : Nat := 0
  let mut other : Nat := 0
  let mut refused : Nat := 0
  let mut problems : Array Lean.Json := #[]
  let mut details : Array Lean.Json := #[]
  let mut concreteRefusals : Array Lean.Json := #[]
  for unit in units do
    let i := unit.ordinal
    if let some why := unit.concreteRefusal then
      concreteRefusals :=
        concreteRefusals.push (Lean.Json.mkObj [("unit", Lean.toJson i), ("reason", .str why)])
    let (kind, isOpaque, structuralRefusal) ←
      match unit.statement with
      | .error why =>
        refused := refused + 1
        if problems.size < 20 then
          problems := problems.push (Lean.Json.str s!"{i}: {why}")
        pure ("refused", false, some why)
      | .ok stmt =>
        let isOpaque :=
          match stmt with
          | .opaque .. => true
          | _ => false
        if isOpaque then
          opaqueCount := opaqueCount + 1
        if stmt.inert then
          inert := inert + 1
          pure ("inert", isOpaque, none)
        else if stmt.allocs.isSome then
          allocate := allocate + 1
          pure ("allocate", isOpaque, none)
        else
          other := other + 1
          pure ("other", isOpaque, none)
    details :=
      details.push
        (Lean.Json.mkObj
          [("unit", Lean.toJson i), ("kind", .str kind), ("opaque", Lean.toJson isOpaque),
            ("concrete_refusal", Lean.toJson unit.concreteRefusal),
            ("structural_refusal", Lean.toJson structuralRefusal)])
  return Lean.Json.mkObj
      [("hostProfile", Lean.toJson profile.label), ("units", Lean.toJson units.size),
        ("inert", Lean.toJson inert), ("allocate", Lean.toJson allocate),
        ("opaque", Lean.toJson opaqueCount), ("other", Lean.toJson other),
        ("refused", Lean.toJson refused), ("may_move", Lean.toJson (inert + allocate)),
        ("may_move_denominator", Lean.toJson units.size), ("problems", Lean.Json.arr problems),
        ("unit_details", Lean.Json.arr details),
        ("lowering_refusals", Lean.Json.arr concreteRefusals)]

/-- Census-only shortcuts select the empty and ECMAScript profiles. Other
contracts use the certified path's reader; their premises do not change this
syntactic census. -/
private def censusProfile (j : Lean.Json) : Except String CoreJS.HostProfile := do
  if j == Lean.Json.mkObj [("profile", .str "empty")] then
    return .empty
  if j == Lean.Json.mkObj [("profile", .str "ecmascript")] then
    return .ecmascript
  return (← CoreJS.StableIntrinsic.readContract j).1

private def runCensus (file : String) (profile : CoreJS.HostProfile)
    (report : Lean.Json → CoreJS.HostProfile → Except String Lean.Json) : IO UInt32 := do
  match Lean.Json.parse (← IO.FS.readFile file) >>= (report · profile) with
  | .ok result =>
    IO.println result.compress;
    pure 0
  | .error why =>
    IO.eprintln why;
    pure 1

/-- Replay uses the recorded Node host; the census defaults to the empty production profile. -/
def main (args : List String) : IO UInt32 := do
  match args with
  | ["--moves-ast", file] =>
    runCensus file .empty moveCensus
  | ["--moves-ast", file, "--node-recording"] =>
    runCensus file .nodeRecording moveCensus
  | ["--moves-ast", file, "--host-contract", contract] =>
    match Lean.Json.parse (← IO.FS.readFile contract) >>= censusProfile with
    | .ok profile =>
      runCensus file profile moveCensus
    | .error why =>
      IO.eprintln why;
      pure 1
  | ["--lowering-census", file] =>
    runCensus file .empty CoreJS.LinkedInputCensus.concreteCensus
  | ["--lowering-census", file, "--host-contract", contract] =>
    match Lean.Json.parse (← IO.FS.readFile contract) >>= censusProfile with
    | .ok profile =>
      runCensus file profile CoreJS.LinkedInputCensus.concreteCensus
    | .error why =>
      IO.eprintln why;
      pure 1
  | ["--ast", file] =>
    for line in (← IO.FS.lines file) do
      IO.println
          (CoreJS.runLine 100000 line fun j scripts =>
            CoreJS.LinkedInput.program j
              ((j.getObjVal? "providers").toOption.getD (Lean.Json.mkObj []))
              (Std.HashSet.ofList (scripts.map Prod.fst))
              ((j.getObjValAs? String "entry").toOption.getD "main"))
    pure 0
  | _ =>
    CoreJS.main args
