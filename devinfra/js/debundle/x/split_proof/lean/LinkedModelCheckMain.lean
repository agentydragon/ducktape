import EsmSplit.CoreJS.LinkedCertificate.Probe
import EsmSplit.CoreJS.Run
import EsmSplit.LowerSplit.Input

/-! Test-only observations of linked CoreJS initialization for parsed fixture modules.
The probe compares concrete runs at one model world and fuel; it writes no modules. -/

open Lean EsmSplit EsmSplit.LowerSplit EsmSplit.SplitFiles CoreJS

private def ownData (state : State world) (value : Val) (key : String) : Option Val := do
  let .obj address := value | none
  let object ← state.objAt address
  let .data value .. ← object.props.lookup (.str key) | none
  return value

private def observe (bindings : List (String × Addr)) (state : State world) (path : List String) :
    IO String := do
  let name :: keys := path | throw (IO.userError "expected a nonempty export observation path")
  let some root := (bindings.lookup name).bind (fun cell => (state.cellAt cell).bind (·.value)) |
    throw (IO.userError s!"missing initialized export {name}")
  let some value := keys.foldlM (ownData state) root |
    throw (IO.userError s!"expected initialized own data at {path}")
  return showVal value

private def observePaths (bindings candidateBindings : List (String × Addr))
    (original split : State world) (paths : List (List String)) : IO (List Json) :=
  paths.mapM fun path => do
    let original ← observe bindings original path
    let split ← observe candidateBindings split path
    return Json.mkObj [("path", toJson path), ("original", toJson original), ("split", toJson split)]

private def hostRef (ref : HostRef × String) : Json :=
  let (ref, identity) := ref
  let fields := match ref with
    | .binding name => [("binding", toJson name)]
    | .property receiver key => [("receiver", toJson receiver), ("key", toJson key)]
  Json.mkObj (fields ++ [("identity", toJson identity)])

private def refusalReport (failure : CertificationRefusal) (details : List (String × Json) := []) : Json :=
  Json.mkObj ([
    ("status", toJson "refused"),
    ("reason_code", toJson failure.reasonCode),
    ("reason", toJson failure.reason),
    ("observation", toJson failure.observation)] ++ details)

private def check (input : Json) : IO (Except Json Json) := do
  let source ← IO.ofExcept (input.getObjVal? "source")
  let files ← IO.ofExcept (input.getObjVal? "files")
  let providers := (input.getObjVal? "providers").toOption.getD (Json.mkObj [])
  let spec ← IO.ofExcept (input.getObjValAs? String "spec" >>= parseAuthoredSpec)
  let profile ← match input.getObjVal? "host" with
    | .ok host => IO.ofExcept (HostProfile.read host)
    | .error _ => pure .empty
  let paths ← match input.getObjVal? "observe" with
    | .ok value => IO.ofExcept (fromJson? (α := List (List String)) value)
    | .error _ => pure []
  let mismatchFuel ← (input.getObjVal? "mismatchFuel").toOption.mapM (IO.ofExcept ∘ Json.getNat?)
  if mismatchFuel.isSome && profile != .empty then
    throw (IO.userError "explicit mismatch fuel requires the empty host profile")
  let asts ← IO.ofExcept (source.getObjValAs? String "sourceText" >>= ESTreeUnits.checkedNodes source)
  let bundle := linkedLayoutBundle asts (Effects.footprintUnits asts)
  let plan := mkPlan bundle spec
  unless ← checkLayout bundle plan (some spec) false do
    throw (IO.userError "invalid linked layout")
  let deployment ← match LinkedInput.decodeSingleton source providers files plan profile with
    | .ok deployment => pure deployment
    | .error reason => throw (IO.userError reason)
  let { before, after, assignment } := deployment
  let _ ← IO.ofExcept (checkPlaces assignment before after)
  let pair ← IO.ofExcept (LinkedBehavior.checkProbePair before after)
  let bindings ← IO.ofExcept (EntryExports.resolve before.entry pair.before.linked)
  let candidateBindings ← IO.ofExcept (EntryExports.resolve after.entry pair.after.linked)
  unless decide (bindings.Perm candidateBindings) do
    return .error (refusalReport { reason := EntryExports.mismatch bindings candidateBindings, reasonCode := some "linked_exports" })
  match LinkedBehavior.probeWitness profile before after with
  | .error failure =>
    let refusal : CertificationRefusal :=
      { reason := failure.reason, reasonCode := some failure.code.label, observation := failure.observation }
    match failure.code, mismatchFuel with
    | .probeMismatch, some fuel =>
      -- Refusals retain only a summary. Re-run the exact pair at the caller's
      -- explicit fuel to inspect requested paths in the two completed states.
      let premises : HostPremises := {}
      let world := premises.specialize World.probe
      let .ok () original := runProgram world fuel before |
        throw (IO.userError "original did not complete normally at the requested mismatch fuel")
      let .ok () split := runProgram world fuel after |
        throw (IO.userError "split did not complete normally at the requested mismatch fuel")
      if LinkedBehavior.probeStatesEqual premises original split then
        throw (IO.userError "states agree at the requested mismatch fuel")
      let values ← observePaths bindings candidateBindings original split paths
      return .error (refusalReport refusal [("fuel", toJson fuel), ("observed", toJson values)])
    | _, _ => return .error (refusalReport refusal)
  | .ok probe =>
    let values ← observePaths bindings candidateBindings probe.sourceState probe.candidateState paths
    return .ok (Json.mkObj [
      ("status", toJson "matched"),
      ("fuel", toJson probe.fuel),
      ("assumptions", toJson probe.premises.assumptions),
      ("host_refs", toJson (probe.premises.refs.map hostRef)),
      ("observed", toJson values)])

def main : IO UInt32 := do
  try
    let input ← IO.ofExcept (Json.parse (← (← IO.getStdin).getLine))
    match ← check input with
    | .ok result => IO.println result.compress; return 0
    | .error result => IO.println result.compress; return 1
  catch error =>
    IO.println (refusalReport { reason := error.toString, reasonCode := some "linked_input" }).compress
    return 1
