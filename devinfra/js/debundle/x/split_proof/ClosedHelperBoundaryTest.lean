import EsmSplit.CoreJS.ClosedHelperBoundary

open CoreJS CoreJS.ClosedHelperBoundary

private def entry := "entry.mjs"
private def provider := "provider.mjs"

/-- Model projection of `function helper() { return 1; }\nconst value = helper();\nexport { value };`. -/
private def source : Program := ⟨entry, [⟨entry, [], [
  (0, .funDecl (.mk "helper" .normal false [] [.ret (.num 1)])),
  (1, .decl .const "value" (.call (.var "helper") []))], [("value", "value")]⟩]⟩

/-- The original three source units, including the non-executable export. -/
private def bundle : EsmSplit.Bundle := [
  { src := "", defs := ["helper"], hoisted := true },
  { src := "", defs := ["value"], eager := ["helper"] },
  { src := "", eager := ["value"], exports := [("value", some "value")] }
]

private def colocated : EsmSplit.Spec := {
  entry
  modules := [(provider, [.stmt 0, .stmt 1]), (entry, [.stmt 2])]
  order := [provider]
  sequential := true
}

private def separated : EsmSplit.Spec := {
  entry
  modules := [(provider, [.stmt 0]), (entry, [.stmt 1, .stmt 2])]
  order := [provider]
  sequential := true
}

private def expectRefusal (label : String) (result : Except String α) : IO Unit := do
  match result with
  | .error _ => pure ()
  | .ok _ => throw (IO.userError s!"{label} unexpectedly passed")

def main : IO Unit := do
  let accepted ← IO.ofExcept (check source bundle colocated)
  unless accepted.provider == provider && accepted.helper == "helper" && accepted.result == "value" do
    throw (IO.userError "guard returned different bindings or owner")
  let _ := helper_not_exported accepted
  let _ := helper_closed accepted
  -- Splitting the call away introduces a glue export that exposes the helper.
  let badPlan := EsmSplit.mkPlan bundle separated
  let some badProvider := badPlan.find provider | throw (IO.userError "missing separated provider")
  unless badProvider.namedExports bundle == [("helper", "helper")] do
    throw (IO.userError "separated request did not expose the helper")
  expectRefusal "helper-exporting placement" (check source bundle separated)
  -- A forged footprint can also introduce an alias for the helper even when
  -- the two executable units are co-located. The guard inspects all glue.
  let aliasedBundle : EsmSplit.Bundle := [
    { src := "", defs := ["helper"], hoisted := true },
    { src := "", defs := ["value"], eager := ["helper"] },
    { src := "", eager := ["value", "helper"],
      exports := [("value", some "value"), ("leaked", some "helper")] }
  ]
  let aliasedPlan := EsmSplit.mkPlan aliasedBundle colocated
  let some aliasedProvider := aliasedPlan.find provider | throw (IO.userError "missing aliased provider")
  unless (aliasedProvider.namedExports aliasedBundle).any (fun (_, localName) => localName == "helper") do
    throw (IO.userError "alias case did not expose the helper")
  expectRefusal "helper exported through an alias" (check source aliasedBundle colocated)
  let captured : Program := ⟨entry, [⟨entry, [], [
    (0, .funDecl (.mk "helper" .normal false [] [.ret (.var "external")])),
    (1, .decl .const "value" (.call (.var "helper") []))], [("value", "value")]⟩]⟩
  expectRefusal "helper with a free binding" (check captured bundle colocated)
  let wrongOrdinal : Program := ⟨entry, [⟨entry, [], [
    (4, .funDecl (.mk "helper" .normal false [] [.ret (.num 1)])),
    (1, .decl .const "value" (.call (.var "helper") []))], [("value", "value")]⟩]⟩
  expectRefusal "changed helper ordinal" (check wrongOrdinal bundle colocated)
  let exportedHelper : Program := ⟨entry, [⟨entry, [], [
    (0, .funDecl (.mk "helper" .normal false [] [.ret (.num 1)])),
    (1, .decl .const "value" (.call (.var "helper") []))],
    [("value", "value"), ("visibleHelper", "helper")]⟩]⟩
  expectRefusal "public helper alias" (check exportedHelper bundle colocated)
