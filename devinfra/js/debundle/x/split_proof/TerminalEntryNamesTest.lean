import EsmSplit.CoreJS.TerminalEntryNames
import EsmSplit.Split

open EsmSplit CoreJS.TerminalEntryNames

private def row : LocalExportUnit := ⟨2, [("data0", "reader0")]⟩
private def bundle : Bundle := [
  { src := "", defs := ["data0"], effect := true },
  { src := "", defs := ["reader0"], eager := ["data0"], effect := true },
  row.footprint ""]
private def request : Spec := {
  entry := "entry.mjs"
  modules := [("provider.mjs", [.stmt 0]), ("consumer.mjs", [.stmt 1]), ("entry.mjs", [.stmt 2])]
  order := ["provider.mjs", "consumer.mjs"]
  sequential := true
}

def main : IO Unit := do
  let plan := mkPlan bundle request
  let .isTrue valid := (inferInstance : Decidable (Valid bundle request plan)) |
    throw (IO.userError "plan invalid")
  match hentry : plan.find "entry.mjs", howner : plan.find "consumer.mjs" with
  | some entry, some owner =>
    let .isTrue owned := (inferInstance : Decidable (row.ordinal ∈ entry.stmts)) |
      throw (IO.userError "terminal unit has wrong owner")
    let .isTrue declOwned := (inferInstance : Decidable (1 ∈ owner.stmts)) |
      throw (IO.userError "reader has wrong owner")
    have footprint : row.FootprintMatches (bundle.stmt row.ordinal) := row.footprint_matches ""
    have pair : ("data0", "reader0") ∈ row.names := by simp [row]
    have declared : "reader0" ∈ (bundle.stmt 1).defs := by simp [bundle, Bundle.stmt]
    have _binding := bound_to_original_owner valid (List.mem_of_find?_eq_some hentry)
      (List.mem_of_find?_eq_some howner) owned footprint declOwned pair declared
    unless entry.imports.any (fun e => e.1 == "consumer.mjs" && e.2.contains "reader0") &&
        !(entry.imports.any (fun e => e.2.contains "data0")) &&
        (entry.namedExports bundle).contains ("data0", "reader0") do
      throw (IO.userError "public alias or authored owner changed")
  | _, _ => throw (IO.userError "missing entry or declaring owner")
