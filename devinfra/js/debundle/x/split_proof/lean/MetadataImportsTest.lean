import EsmSplit.MetadataImports
import EsmSplit.Split

open EsmSplit EsmSplit.MetadataImports

set_option maxRecDepth 16000

private def dataName (i : Nat) := s!"data{i}"
private def readerName (i : Nat) := s!"reader{i}"

private def bundle : Bundle :=
  ((List.range 16).map fun i => { src := "", defs := [dataName i], effect := true }) ++
  ((List.range 16).map fun i => { src := "", defs := [readerName i], eager := [dataName i], effect := true }) ++
  [{ src := "", eager := (List.range 16).map readerName,
     exports := (List.range 16).map fun i => (readerName i, some (readerName i)) }]

private def request : Spec := {
  entry := "entry.mjs"
  modules := [("entry.mjs", [.stmt 32]),
    ("provider0.mjs", (List.range 8).map .stmt),
    ("provider1.mjs", (List.range 8).map fun i => .stmt (i + 8)),
    ("consumer0.mjs", (List.range 8).map fun i => .stmt (i + 16)),
    ("consumer1.mjs", (List.range 8).map fun i => .stmt (i + 24))]
  order := ["provider0.mjs", "provider1.mjs", "consumer0.mjs", "consumer1.mjs"]
  sequential := true
}

def main : IO Unit := do
  let plan := mkPlan bundle request
  let .isTrue valid := (inferInstance : Decidable (Valid bundle request plan)) |
    throw (IO.userError "all-unit plan is invalid")
  match found : plan.find request.entry with
  | none => throw (IO.userError "entry missing")
  | some entry =>
    have moduleIn : entry ∈ plan.mods := List.mem_of_find?_eq_some found
    let .isTrue owned := (inferInstance : Decidable (32 ∈ entry.stmts)) |
      throw (IO.userError "terminal export unit not owned by entry")
    for i in List.range 16 do
      let name := readerName i
      let owner := if i < 8 then "consumer0.mjs" else "consumer1.mjs"
      let .isTrue used := (inferInstance : Decidable (name ∈ (bundle.stmt 32).eager ++ (bundle.stmt 32).lazy)) |
        throw (IO.userError s!"{name} missing from export-unit uses")
      let .isTrue sourceOwner := (inferInstance : Decidable (plan.declaredIn bundle name = .declared owner)) |
        throw (IO.userError s!"{name} has the wrong declaring owner")
      let .isTrue notLocal := (inferInstance : Decidable (entry.declares bundle name = false)) |
        throw (IO.userError s!"{name} unexpectedly declared in entry")
      have _imported := external_use_imported valid moduleIn owned used sourceOwner notLocal
      unless entry.imports.any (fun e => e.1 == owner && e.2.contains name) do
        throw (IO.userError s!"{name} was not imported from {owner}")
    unless decide (plan.declaredIn bundle "reader0" ≠ .declared "consumer1.mjs") do
      throw (IO.userError "wrong source owner was accepted")
