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
  let source : Module := ⟨"source.mjs", [], (List.range statements.length).zip statements, []⟩
  let .ok ⟨sourceShape⟩ := checkTerminalOnly source 32 |
    throw (IO.userError "dense original source with terminal metadata was refused")
  let sparse : Module := ⟨"source.mjs", [],
    [(0, .decl .const "seed" (.num 42)), (2, .decl .const "result" (.var "seed"))],
    [("exposed", "seed")]⟩
  match checkTerminalOnly sparse 1 with
  | .error "expected dense executable original IDs followed by one terminal metadata unit" => pure ()
  | _ => throw (IO.userError "nonterminal metadata with later executable ID was not refused")
  let plan := EsmSplit.mkPlan bundle request
  unless decide (EsmSplit.Valid bundle request plan) do throw (IO.userError "all-unit plan is invalid")
  unless plan.execOrder == List.range 33 do throw (IO.userError "original unit order changed")
  let .isTrue kept := (inferInstance : Decidable (Kept statements plan)) |
    throw (IO.userError "executable projection changed order")
  have sourceStatements : source.body.map Prod.snd = statements := by
    change ((List.range statements.length).zip statements).map Prod.snd = statements
    exact List.map_snd_zip (by simp)
  have sourceKept : Kept (source.body.map Prod.snd) plan := by rw [sourceStatements]; exact kept
  have _loaded := terminal_loaded_ids source 32 bundle plan sourceShape sourceKept
  have _same := holds_statements (ss := statements) (B := bundle) (P := plan) (by rw [kept])
  unless (splitProgram statements bundle plan).regions == 32 do
    throw (IO.userError "terminal metadata added an executable region")
  let generated := splitProgram statements bundle plan
  let some provider := generated.find "provider0.mjs" | throw (IO.userError "missing provider")
  let some consumer := generated.find "consumer1.mjs" | throw (IO.userError "missing consumer")
  let some entry := generated.find "entry.mjs" | throw (IO.userError "missing entry")
  unless provider.body.map Prod.fst == List.range 8 &&
      consumer.body.map Prod.fst == (List.range 8).map (· + 24) && entry.body.isEmpty do
    throw (IO.userError "authored executable owners or entry metadata placement changed")
  match h : link generated (initialState World.probe generated.regions) with
  | .ok linked state =>
    let checked : LinkedBehavior.CheckedLink generated World.probe := ⟨linked, state, h⟩
    have _ids := linked_ids statements bundle plan checked
    have _originalIds : (unitsOf checked.linked).map Prod.fst = source.body.map Prod.fst := by
      apply terminal_linked_ids source 32 bundle plan sourceShape sourceKept
    unless (unitsOf linked).map Prod.fst == List.range 32 do
      throw (IO.userError "actual linked unit order does not preserve original IDs")
  | _ => throw (IO.userError "the synthetic plan did not link")
  -- A metadata-only module can carry an ordering edge. Filtering modules
  -- before visiting this graph would lose the provider dependency.
  let graph : EsmSplit.Plan := { entry := "entry.mjs", mods := [
    { name := "provider.mjs", stmts := [0], imports := [], exports := [] },
    { name := "metadata.mjs", stmts := [1], imports := [("provider.mjs", [])], exports := [] },
    { name := "entry.mjs", stmts := [], imports := [("metadata.mjs", [])], exports := [] }] }
  let shortened : EsmSplit.Plan := { graph with mods := graph.mods.filter fun m => m.name != "metadata.mjs" }
  unless graph.evalOrder == ["provider.mjs", "metadata.mjs", "entry.mjs"] &&
      executable [.decl .const "x" (.num 1)] graph == [0] &&
      shortened.evalOrder != graph.evalOrder do
    throw (IO.userError "metadata-only dependency was discarded before module ordering")
