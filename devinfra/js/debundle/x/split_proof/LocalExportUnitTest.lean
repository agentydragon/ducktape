import EsmSplit.LocalExportUnit
import EsmSplit.Split

open EsmSplit

private def exportUnit : LocalExportUnit := ⟨2, [("data0", "reader0")]⟩

private def bundle : Bundle := [
  { src := "", defs := ["data0"], effect := true },
  { src := "", defs := ["reader0"], eager := ["data0"], effect := true },
  exportUnit.footprint ""]

private def request : Spec := {
  entry := "entry.mjs"
  modules := [("provider.mjs", [.stmt 0]), ("consumer.mjs", [.stmt 1]), ("entry.mjs", [.stmt 2])]
  order := ["provider.mjs", "consumer.mjs"]
  sequential := true
}

private def sharedLocal : LocalExportUnit := ⟨0, [("first", "x"), ("second", "x")]⟩
private def deduplicated : Stmt := {
  src := "", eager := ["x"], exports := [("first", some "x"), ("second", some "x")]
}

private theorem sharedLocalMatches : sharedLocal.FootprintMatches deduplicated := by
  refine ⟨rfl, ?_⟩
  intro pair member
  simp only [sharedLocal, List.mem_cons, List.not_mem_nil, or_false] at member
  rcases member with rfl | rfl <;> simp [deduplicated]

def main : IO Unit := do
  have _sourceFacts := LocalExportUnit.local_use_and_public (B := bundle) (unit := exportUnit)
    (src := "") (publicName := "data0") (localName := "reader0") (by rfl) (by simp [exportUnit])
  have _shared := LocalExportUnit.matched_local_use_and_public (B := [deduplicated]) (unit := sharedLocal)
    (publicName := "second") (localName := "x") sharedLocalMatches (by simp [sharedLocal])
  let plan := mkPlan bundle request
  unless decide (Valid bundle request plan) do throw (IO.userError "plan invalid")
  let some entry := plan.find request.entry | throw (IO.userError "entry missing")
  unless entry.imports.any (fun e => e.1 == "consumer.mjs" && e.2.contains "reader0") do
    throw (IO.userError "export local did not induce the consumer import")
  unless !entry.imports.any (fun e => e.2.contains "data0") do
    throw (IO.userError "public alias was treated as the local binding")
  unless (entry.namedExports bundle).contains ("data0", "reader0") do
    throw (IO.userError "public/local export pair changed")
  unless decide (plan.declaredIn bundle "data0" = .declared "provider.mjs" ∧
      plan.declaredIn bundle "reader0" = .declared "consumer.mjs") do
    throw (IO.userError "test bindings have the wrong owners")
