import EsmSplit.CoreJS.SparseProjectedOrder
import EsmSplit.Split

open CoreJS CoreJS.SparseProjectedOrder

private def sourceRows : List (Nat × Stmt) := [
  (0, .decl .const "seed" (.num 42)),
  (2, .decl .const "result" (.var "seed"))]

/-- Three original AST rows: executable declaration #0, local export metadata
#1, and executable declaration #2. -/
private def bundle : EsmSplit.Bundle := [
  { src := "const seed = 42;", defs := ["seed"], effect := true },
  { src := "export { seed as exposed };", eager := ["seed"],
    exports := [("exposed", some "seed")], effect := true },
  { src := "const result = seed;", defs := ["result"], eager := ["seed"], effect := true }]

/-- The existing sparse nonterminal placement: the worker owns #0 and the
entry owns both metadata #1 and executable #2. -/
private def request : EsmSplit.Spec := {
  entry := "entry.mjs"
  modules := [("worker.mjs", [.stmt 0]), ("entry.mjs", [.stmt 1, .stmt 2])]
  order := ["worker.mjs"]
  sequential := true
}

private def plan := EsmSplit.mkPlan bundle request
private def constructed := splitProgramIndexed sourceRows bundle plan

private theorem samplePlanValid : EsmSplit.Valid bundle request plan := by decide

private theorem sampleProjection : OrderKept sourceRows plan := by decide

example : plan.execOrder = [0, 1, 2] := by decide

example : executableIds sourceRows plan = [0, 2] := by decide

example : ((constructed.loaded.flatMap (·.body)).map Prod.fst) = [0, 2] := by
  have h := loaded_ids_of_order_kept (source := sourceRows) (B := bundle) (P := plan) sampleProjection
  simpa [sourceRows, constructed] using h

example : constructed.loaded.flatMap (·.body) = sourceRows := by rfl

example : (plan.find "worker.mjs").map (·.stmts) = some [0] := by decide

example : (plan.find "entry.mjs").map (·.stmts) = some [1, 2] := by decide

example :
    ∀ id owner, (id, owner) ∈ CoreJS.PlanPlacement.assignments plan →
      EsmSplit.Spec.claimants request bundle id = [owner] := by
  intro id owner member
  exact authored_owner samplePlanValid.2.2.1 member

def main : IO Unit := do
  unless decide (EsmSplit.Valid bundle request plan) do
    throw (IO.userError "sparse interior plan is invalid")
  unless decide (OrderKept sourceRows plan) do
    throw (IO.userError "full plan order does not project to source IDs #0 and #2")
  let loaded : List (Nat × Stmt) := constructed.loaded.flatMap (·.body)
  match loaded with
  | [(0, .decl .const "seed" (.num seedValue)), (2, .decl .const "result" (.var "seed"))] =>
    unless seedValue == 42 do throw (IO.userError "the indexed split changed a source statement")
  | _ => throw (IO.userError "indexed split did not preserve original statements and IDs")
  match constructed.fileUnitPlacements with
  | [(0, "worker.mjs"), (2, "entry.mjs")] => pure ()
  | _ => throw (IO.userError "authored executable owners changed")
  let some entry := constructed.find "entry.mjs" | throw (IO.userError "missing entry module")
  match entry.exports with
  | [("exposed", "seed")] => pure ()
  | _ => throw (IO.userError "entry metadata export was not retained")
