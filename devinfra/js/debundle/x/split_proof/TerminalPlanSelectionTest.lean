import EsmSplit.CoreJS.TerminalPlanSelection

open CoreJS CoreJS.TerminalExportPlacement

private def source : TerminalSource := {
  module := ⟨"original.mjs", [], [(0, .decl .const "value" (.str "v"))], [("public", "value")]⟩
  exportOrdinal := 1
}

private def bundle : EsmSplit.Bundle := [
  { src := "", defs := ["value"] },
  { src := "", eager := ["value"], exports := [("public", some "value")] }]

private def request : EsmSplit.Spec := {
  entry := "entry.mjs"
  modules := [("entry.mjs", [.stmt 1]), ("provider.mjs", [.name "value"])]
}

def main : IO Unit := do
  let .ok selected := resolve source request | throw (IO.userError "valid original selection refused")
  unless decide (TerminalPlanSelection.ClaimantsMatch bundle request selected.selected) do
    throw (IO.userError "original selection does not match full-bundle claimants")
  if decide (TerminalPlanSelection.ClaimantsMatch bundle request [(0, "entry.mjs"), (1, "provider.mjs")]) then
    throw (IO.userError "wrong owners matched full-bundle claimants")
  let duplicate : EsmSplit.Spec := { request with modules := [
    ("entry.mjs", [.stmt 1]), ("provider.mjs", [.name "value", .stmt 0])] }
  match resolve source duplicate with
  | .error "an original unit is selected more than once" => pure ()
  | _ => throw (IO.userError "name/ordinal aliases did not refuse the duplicate selection")
