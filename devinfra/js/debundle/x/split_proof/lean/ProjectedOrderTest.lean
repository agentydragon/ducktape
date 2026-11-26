import EsmSplit.CoreJS.ProjectedOrder
import EsmSplit.Split

open CoreJS CoreJS.ProjectedOrder

private def dataName (i : Nat) := s!"data{i}"
private def readerName (i : Nat) := s!"reader{i}"

private def statements : List Stmt :=
  ((List.range 16).map fun i => .decl .const (dataName i) (.str s!"value{i}")) ++
  ((List.range 16).map fun i => .decl .const (readerName i) (.var (dataName i)))

private def bundle : EsmSplit.Bundle :=
  ((List.range 16).map fun i => { src := "", defs := [dataName i], effect := true }) ++
  ((List.range 16).map fun i => { src := "", defs := [readerName i], eager := [dataName i], effect := true }) ++
  [{ src := "", eager := (List.range 16).map readerName,
     exports := (List.range 16).map fun i => (readerName i, some (readerName i)) }]

private def request : EsmSplit.Spec := {
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
  let plan := EsmSplit.mkPlan bundle request
  unless decide (EsmSplit.Valid bundle request plan) do throw (IO.userError "all-unit plan is invalid")
  unless plan.execOrder == List.range 33 do throw (IO.userError "original unit order changed")
  let .isTrue kept := (inferInstance : Decidable (Kept statements plan)) |
    throw (IO.userError "executable projection changed order")
  have _same := holds_statements (ss := statements) (B := bundle) (P := plan) (by rw [kept])
  unless (splitProgram statements bundle plan).regions == 32 do
    throw (IO.userError "terminal metadata added an executable region")
