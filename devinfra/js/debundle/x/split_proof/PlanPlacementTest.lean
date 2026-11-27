import EsmSplit.CoreJS.PlanPlacement

open CoreJS CoreJS.PlanPlacement

private def entry := "entry.mjs"
private def worker := "worker.mjs"

private def statements (n : Nat) : List Stmt :=
  (List.range n).map fun i => .decl .const s!"v{i}" (.num 1)

private def bundle (n : Nat) : EsmSplit.Bundle :=
  (List.range n).map fun i => { src := "", defs := [s!"v{i}"], effect := true }

private def spec (n cut : Nat) : EsmSplit.Spec := {
  entry
  modules := [(worker, ((List.range n).take cut).map .stmt),
    (entry, ((List.range n).drop cut).map .stmt)]
  order := [worker]
  sequential := true
}

private def check (n : Nat) : IO Unit := do
  let ss := statements n
  let b := bundle n
  let s := spec n (n / 2)
  let plan := EsmSplit.mkPlan b s
  let .isTrue sourceSafe := (inferInstance : Decidable (EsmSplit.safeModulePath entry = true)) |
    throw (IO.userError "unsafe source module")
  let .isTrue safe := (inferInstance : Decidable (∀ m ∈ plan.mods, EsmSplit.safeModulePath m.name = true)) |
    throw (IO.userError s!"{n}: unsafe module")
  let .isTrue coverage := (inferInstance : Decidable (((assignments plan).map Prod.fst).Perm (List.range ss.length))) |
    throw (IO.userError s!"{n}: plan does not cover each unit once")
  let .isTrue faithful := (inferInstance : Decidable (EsmSplit.Faithful b s plan)) |
    throw (IO.userError s!"{n}: fixture plan is not faithful")
  have _placed := faithful_planned_places ss b s entry [] sourceSafe safe faithful coverage
  unless (assignments plan).length == n do throw (IO.userError s!"{n}: placement count changed")
  for pair in assignments plan do
    unless s.claimants b pair.1 == [pair.2] do throw (IO.userError "authored owner changed")

private def test : IO Unit := do
  for n in [0, 1, 3, 7, 16] do check n
  let named : EsmSplit.Spec := { entry, modules :=
    [(worker, [.name "v0", .name "v1"]), (entry, [.name "v2"])] }
  let namedPlan := EsmSplit.mkPlan (bundle 3) named
  unless assignments namedPlan == [(0, worker), (1, worker), (2, entry)] &&
      decide (EsmSplit.Faithful (bundle 3) named namedPlan) do
    throw (IO.userError "name selectors did not retain authored owners")
  let b := bundle 2
  let bad : EsmSplit.Spec := { entry, modules := [(worker, [.stmt 0]), (entry, [.stmt 0, .stmt 1])] }
  let plan := EsmSplit.mkPlan b bad
  unless !decide (((assignments plan).map Prod.fst).Perm (List.range 2)) do
    throw (IO.userError "duplicate unit passed source-plan coverage")

def main : IO Unit := test
