import EsmSplit.CoreJS.SparseCheckedSplit
import EsmSplit.ESTreeEmit

open CoreJS CoreJS.LinkedBehavior CoreJS.ResolvedPlacement CoreJS.SparseCheckedSplit
open Lean (Json toJson)

namespace SparseCheckedSplitTest

/-! CoreJS shape of `const seed = 42; export { seed }; const result = seed;`.
Ordinal 1 is static metadata and is absent from the executable body. -/
private def source (second : Nat) (initial : Expr) : Program :=
  ⟨"entry.mjs", [⟨"entry.mjs", [],
    [(0, .decl .const "seed" initial), (second, .decl .const "result" (.var "seed"))],
    [("seed", "seed")]⟩]⟩

private def names : EsmSplit.Spec := {
  entry := "entry.mjs"
  modules := [("worker.mjs", [.name "seed"]), ("entry.mjs", [.name "result"])]
}

private def ordinals (second : Nat) : EsmSplit.Spec := {
  entry := "entry.mjs"
  modules := [("worker.mjs", [.stmt 0]), ("entry.mjs", [.stmt second])]
}

private def resolved (original : Program) (spec : EsmSplit.Spec) :
    Except String (Placement original spec) :=
  resolve original spec (CheckedSplit.requestedPlacement original)

private def checkCase (second : Nat) (value : Float) (spec : EsmSplit.Spec) : IO Unit := do
  let original := source second (.num value)
  let placement ← IO.ofExcept (resolved original spec)
  unless placement.assignment == [(0, "worker.mjs"), (second, "entry.mjs")] do
    throw (IO.userError "placement did not retain the original source identities")
  let built ← IO.ofExcept (construct placement .allOutcomes)
  let normal ← IO.ofExcept (construct placement .normalCompletion)
  let _ ← IO.ofExcept (checkOutput normal built.val)
  let some worker := built.val.find "worker.mjs" | throw (IO.userError "missing worker")
  let some entry := built.val.find "entry.mjs" | throw (IO.userError "missing entry")
  unless worker.body.map Prod.fst == [0] && entry.body.map Prod.fst == [second] &&
      built.val.regions == original.regions do throw (IO.userError "unit IDs or regions changed")
  unless exports World.probe built.val == some [("seed", ⟨1, 0⟩)] do
    throw (IO.userError "the public live cell changed")
  let .ok () state := runProgram World.probe 16 built.val | throw (IO.userError "expected normal completion")
  let .ok (.num actual) _ := readCell ⟨1, 0⟩ state | throw (IO.userError "expected the numeric seed")
  unless actual == value do throw (IO.userError "seed value changed")
  let altered := { built.val with modules := [worker, { entry with exports := [("seed", "result")] }] }
  unless exports World.probe altered == some [("seed", ⟨second + 1, 0⟩)] do
    throw (IO.userError "wrong-cell control did not select the result cell")
  match checkOutput built altered with
  | .error "decoded output differs from the constructed modules" => pure ()
  | _ => throw (IO.userError "wrong-cell control was accepted")

private def expectError {α : Type} (message : String) (result : Except String α) : IO Unit := do
  match result with
  | .error why => unless why == message do throw (IO.userError s!"expected {message}; got {why}")
  | .ok _ => throw (IO.userError s!"expected refusal: {message}")

/-- The compact template plan cannot be passed unchanged to the emitter with
the original AST: its entry ordinal 1 names the export, not the result. -/
private def checkEmitterBoundary : IO Unit := do
  let compact : Program := ⟨"entry.mjs", [⟨"entry.mjs", [], [
    (0, .decl .const "seed" (.num 42)), (1, .decl .const "result" (.var "seed"))], []⟩]⟩
  let template ← IO.ofExcept (CheckedSplit.checkedSplit compact names .allOutcomes)
  let some entry := template.plan.find "entry.mjs" | throw (IO.userError "compact plan has no entry")
  unless entry.stmts == [1] do throw (IO.userError "unexpected compact entry ordinal")
  let seed := Json.mkObj [("type", toJson "VariableDeclaration"), ("marker", toJson "seed")]
  let metadata := Json.mkObj [("type", toJson "ExportNamedDeclaration"), ("declaration", .null),
    ("source", .null), ("specifiers", toJson (#[] : Array Json))]
  let result := Json.mkObj [("type", toJson "VariableDeclaration"), ("marker", toJson "result")]
  let files ← IO.ofExcept (EsmSplit.ESTreeEmit.programs #[seed, metadata, result] template.bundle template.plan "")
  let emitted ← IO.ofExcept (files.getObjVal? "entry.mjs")
  let body ← IO.ofExcept (emitted.getObjValAs? (Array Json) "body")
  unless body.toList.contains metadata && !body.toList.contains result do
    throw (IO.userError "expected the compact plan to select metadata rather than result")

def test : IO Unit := do
  checkCase 2 42 names
  checkCase 3 7 (ordinals 3)
  checkCase 4 9 (ordinals 4)
  let original := source 2 (.num 42)
  let reordered := { names with modules := [("worker.mjs", [.name "result"]), ("entry.mjs", [.name "seed"])] }
  let reorderedPlacement ← IO.ofExcept (resolved original reordered)
  expectError "requested placement changes the source unit order" (construct reorderedPlacement .allOutcomes)
  let invalid := { names with modules := [("worker.mjs", [.stmt 1]), ("entry.mjs", [.name "result"])] }
  expectError "source position 1 has no executable unit" (resolved original invalid)
  let noGap := source 1 (.num 42)
  let noGapPlacement ← IO.ofExcept (resolved noGap names)
  expectError "sparse construction requires a gap after the first unit" (construct noGapPlacement .allOutcomes)
  let descending : Program := ⟨"entry.mjs", [⟨"entry.mjs", [],
    [(2, .decl .const "seed" (.num 42)), (0, .decl .const "result" (.var "seed"))],
    [("seed", "seed")]⟩]⟩
  let descendingPlacement ← IO.ofExcept (resolved descending names)
  expectError "sparse construction requires two units with first ID 0" (construct descendingPlacement .allOutcomes)
  let duplicateExports : Program := ⟨"entry.mjs", [⟨"entry.mjs", [],
    [(0, .decl .const "seed" (.num 42)), (2, .decl .const "result" (.var "seed"))],
    [("seed", "seed"), ("seed", "seed")]⟩]⟩
  let duplicatePlacement ← IO.ofExcept (resolved duplicateExports names)
  expectError "source public export names are duplicated" (construct duplicatePlacement .allOutcomes)
  let throwing := source 2 (.var "seed")
  let throwingPlacement ← IO.ofExcept (resolved throwing names)
  let throwingBuilt ← IO.ofExcept (construct throwingPlacement .allOutcomes)
  let originalPlacement ← IO.ofExcept (resolved original names)
  let originalBuilt ← IO.ofExcept (construct originalPlacement .allOutcomes)
  let .throw (.error .reference message) before := runProgram World.probe 16 throwing |
    throw (IO.userError "expected source temporal dead zone throw")
  let .throw (.error .reference message') after := runProgram World.probe 16 throwingBuilt.val |
    throw (IO.userError "expected output temporal dead zone throw")
  letI : DecidableEq World.probe.σ := by change DecidableEq Unit; infer_instance
  unless message == message' && (StateMatch.state before after).isSome do
    throw (IO.userError "abrupt result or state changed")
  match runProgram World.probe 0 original, runProgram World.probe 0 originalBuilt.val with
  | .fuel, .fuel => pure ()
  | _, _ => throw (IO.userError "fuel exhaustion changed")
  checkEmitterBoundary

end SparseCheckedSplitTest

def main : IO Unit := SparseCheckedSplitTest.test
