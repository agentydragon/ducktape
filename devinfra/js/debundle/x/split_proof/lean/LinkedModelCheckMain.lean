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

private def hostRef (ref : HostRef × String) : Json :=
  let (ref, identity) := ref
  let fields := match ref with
    | .binding name => [("binding", toJson name)]
    | .property receiver key => [("receiver", toJson receiver), ("key", toJson key)]
  Json.mkObj (fields ++ [("identity", toJson identity)])

private def check (input : Json) : IO (Except CertificationRefusal Json) := do
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
    return .error { reason := EntryExports.mismatch bindings candidateBindings, reasonCode := some "linked_exports" }
  match LinkedBehavior.probeWitness profile before after with
  | .error failure =>
    return .error { reason := failure.reason, reasonCode := some failure.code.label, observation := failure.observation }
  | .ok probe =>
    let values ← paths.mapM fun path => do
      let original ← observe bindings probe.sourceState path
      let split ← observe candidateBindings probe.candidateState path
      return Json.mkObj [("path", toJson path), ("original", toJson original), ("split", toJson split)]
    return .ok (Json.mkObj [
      ("status", toJson "matched"),
      ("fuel", toJson probe.fuel),
      ("assumptions", toJson probe.premises.assumptions),
      ("host_refs", toJson (probe.premises.refs.map hostRef)),
      ("observed", toJson values)])

private def refuse (failure : CertificationRefusal) : IO UInt32 := do
  IO.println (Json.mkObj [
    ("status", toJson "refused"),
    ("reason_code", toJson failure.reasonCode),
    ("reason", toJson failure.reason),
    ("observation", toJson failure.observation)]).compress
  return 1

def main : IO UInt32 := do
  try
    let input ← IO.ofExcept (Json.parse (← (← IO.getStdin).getLine))
    match ← check input with
    | .ok result => IO.println result.compress; return 0
    | .error failure => refuse failure
  catch error =>
    refuse { reason := error.toString, reasonCode := some "linked_input" }
