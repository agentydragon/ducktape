import EsmSplit.CoreJS.ResolvedPlacement
import EsmSplit.CoreJS.CheckedSplit
import EsmSplit.CoreJS.LinkedPivotCheck
import EsmSplit.CoreJS.StateMatch

open CoreJS CoreJS.LinkedBehavior CoreJS.ResolvedPlacement

namespace ResolvedPlacementTest

private abbrev LegacyPlacement (source : Program) (request : EsmSplit.Spec) :=
  CheckedPlacement source request (CheckedSplit.requestedPlacement source)

private def legacyResolve (source : Program) (request : EsmSplit.Spec) :
    Except String (LegacyPlacement source request) :=
  resolve source request (CheckedSplit.requestedPlacement source)

/-! `const seed = 42; export { seed }; const result = seed;` has executable
source ordinals 0 and 2, with the static export at ordinal 1. -/
private def sparse : Program := ⟨"entry.mjs", [⟨"entry.mjs", [], [
  (0, .decl .const "seed" (.num 42)), (2, .decl .const "result" (.var "seed"))],
  [("seed", "seed")]⟩]⟩

private def output : Program := ⟨"entry.mjs", [
  ⟨"worker.mjs", [], [(0, .decl .const "seed" (.num 42))], [("seed", "seed")]⟩,
  ⟨"entry.mjs", [⟨"worker.mjs", [("seed", "seed")]⟩],
    [(2, .decl .const "result" (.var "seed"))], [("seed", "seed")]⟩]⟩

private def spec : EsmSplit.Spec := {
  entry := "entry.mjs"
  modules := [("worker.mjs", [.name "seed"]), ("entry.mjs", [.name "result"])]
}

/-- Link and schedule checks produce the strict proof for this actual pair.
No equality of executions is used as an input. -/
def proveSparse (resolved : LegacyPlacement sparse spec) : Except String
    (PLift (ResolvedPlacement.Sound resolved .allOutcomes output)) := do
  let ⟨places⟩ ← checkPlaces resolved.assignment sparse output
  let links ← checkProbePair sparse output
  let some ⟨ready⟩ := StateMatch.probe links.before.ready links.after.ready |
    throw "initial linker states differ"
  let some ⟨units⟩ := sameUnits? (unitsOf links.before.linked) (unitsOf links.after.linked) |
    throw "linked unit schedules differ"
  if exported : exportsMatchFrom links.before links.after = true then
    have runs (w : World) (fuel : Nat) : runProgram w fuel sparse = runProgram w fuel output := by
      rw [run_after_link fuel (links.before.forWorld w), run_after_link fuel (links.after.forWorld w)]
      have sameReady := congrArg (linkState w) ready
      change runUnits fuel (unitsOf links.before.linked) (linkState w links.before.ready) =
        runUnits fuel (unitsOf links.after.linked) (linkState w links.after.ready)
      rw [← sameReady]
      exact congrFun (runUnits_same fuel units) _
    return ⟨places, links.sameExports exported, fun w _ fuel => runs w fuel⟩
  else throw "entry exports do not resolve to the source cells"

private def dense : Program := ⟨"entry.mjs", [⟨"entry.mjs", [], [
  (0, .decl .const "seed" (.num 42)),
  (1, .funDecl (.mk "read" .normal false [] [.ret (.var "seed")])),
  (2, .decl .const "result" (.call (.var "read") [])),
  (3, .decl .const "other" (.num 42))], [("result", "result")]⟩]⟩

private def denseSpec : EsmSplit.Spec := {
  entry := "entry.mjs"
  modules := [("worker.mjs", [.name "seed", .name "read"]),
    ("entry.mjs", [.stmt 2, .stmt 3])]
}

/-! CoreJS shape of the dense staged-artifact example. -/
private def artifactDense : Program := ⟨"entry.mjs", [⟨"entry.mjs", [], [
  (0, .decl .const "seed" (.num 42)),
  (1, .funDecl (.mk "read" .normal false [] [.ret (.var "seed")])),
  (2, .funDecl (.mk "other" .normal false [] [.ret (.var "seed")])),
  (3, .decl .const "result" (.call (.var "read") [])),
  (4, .decl .const "decoy" (.call (.var "other") []))], [("result", "result")]⟩]⟩

private def artifactSpec : EsmSplit.Spec := {
  entry := "entry.mjs"
  modules := [("entry.mjs", [.name "result", .name "decoy"]),
    ("worker.mjs", [.name "seed", .name "read", .name "other"])]
}

/-- A prior dense construction transfers through the resolved-placement
interface and back to the unchanged strict contract for either guarantee. -/
def provePriorDense {requested : Guarantee} (resolved : LegacyPlacement dense denseSpec)
    (previous : CheckedSplit.Construction dense denseSpec requested) : Except String
    (PLift (ResolvedPlacement.Sound resolved requested previous.program ∧
      CheckedSplit.Sound dense denseSpec requested previous.program)) := do
  if denseIDs : resolved.module.body.map Prod.fst = List.range resolved.module.body.length then
    have sound : ResolvedPlacement.Sound resolved requested previous.program := by
      unfold ResolvedPlacement.Sound
      rw [dense_compatible resolved denseIDs]
      exact previous.sound
    return ⟨sound, dense_sound resolved denseIDs sound⟩
  else throw "the prior dense fixture no longer has dense source IDs"

private def expectError {α : Type} (expected : String) (result : Except String α) : IO Unit := do
  match result with
  | .error why => unless why == expected do throw (IO.userError s!"expected {expected}, got {why}")
  | .ok _ => throw (IO.userError s!"expected refusal: {expected}")

private def withBody (body : List (Nat × Stmt)) : Program :=
  ⟨"entry.mjs", [⟨"entry.mjs", [], body, [("seed", "seed")]⟩]⟩

def test : IO Unit := do
  let chosen ← IO.ofExcept (legacyResolve sparse spec)
  unless chosen.assignment == [(0, "worker.mjs"), (2, "entry.mjs")] do
    throw (IO.userError "name selectors did not preserve sparse source IDs")
  let _ ← IO.ofExcept (proveSparse chosen)
  unless sparse.regions == 3 && output.regions == sparse.regions do
    throw (IO.userError "sparse output changed region layout")
  unless exports World.probe sparse == some [("seed", ⟨1, 0⟩)] &&
      exports World.probe output == some [("seed", ⟨1, 0⟩)] do
    throw (IO.userError "sparse live export cell changed")
  let ordinals := { spec with modules := [("worker.mjs", [.stmt 0]), ("entry.mjs", [.stmt 2])] }
  let chosenByOrdinal ← IO.ofExcept (legacyResolve sparse ordinals)
  unless chosenByOrdinal.assignment == chosen.assignment do throw (IO.userError "source-position selectors chose different IDs")
  let wrongPlanner : EsmSplit.Spec → List UnitPlacement := fun _ => [(0, "entry.mjs"), (1, "worker.mjs")]
  expectError "planner placement differs from the selected original units" (resolve sparse spec wrongPlanner)
  let outsidePlanner : EsmSplit.Spec → List UnitPlacement := fun _ => [(99, "worker.mjs")]
  expectError "planner position 99 is out of range" (resolve sparse spec outsidePlanner)
  let denseChosen ← IO.ofExcept (legacyResolve dense denseSpec)
  unless denseChosen.assignment == CheckedSplit.requestedPlacement dense denseSpec &&
      denseChosen.assignment == [(0, "worker.mjs"), (1, "worker.mjs"), (2, "entry.mjs"), (3, "entry.mjs")] do
    throw (IO.userError "prior dense placement changed")
  let all ← IO.ofExcept (CheckedSplit.checkedSplit dense denseSpec .allOutcomes)
  let _ ← IO.ofExcept (provePriorDense denseChosen all)
  let normal ← IO.ofExcept (CheckedSplit.checkedSplit dense denseSpec .normalCompletion)
  let _ ← IO.ofExcept (provePriorDense denseChosen normal)
  let artifactChosen ← IO.ofExcept (legacyResolve artifactDense artifactSpec)
  unless artifactChosen.assignment == CheckedSplit.requestedPlacement artifactDense artifactSpec do
    throw (IO.userError "prior staged-artifact placement changed")
  let _ ← IO.ofExcept (CheckedSplit.checkedSplit artifactDense artifactSpec .allOutcomes)
  let _ ← IO.ofExcept (CheckedSplit.checkedSplit artifactDense artifactSpec .normalCompletion)
  let missingName := { spec with modules := [("worker.mjs", [.name "missing"]), ("entry.mjs", [.name "result"])] }
  expectError "source binding missing is missing" (legacyResolve sparse missingName)
  let metadata := { spec with modules := [("worker.mjs", [.stmt 1]), ("entry.mjs", [.name "result"])] }
  expectError "source position 1 has no executable unit" (legacyResolve sparse metadata)
  let outOfRange := { spec with modules := [("worker.mjs", [.stmt 99]), ("entry.mjs", [.name "result"])] }
  expectError "source position 99 has no executable unit" (legacyResolve sparse outOfRange)
  let duplicate := { spec with modules := [("worker.mjs", [.name "seed", .stmt 0]), ("entry.mjs", [.name "result"])] }
  expectError "an original unit is selected more than once" (legacyResolve sparse duplicate)
  let incomplete := { spec with modules := [("worker.mjs", [.name "seed"]), ("entry.mjs", [])] }
  expectError "selectors do not cover every original unit" (legacyResolve sparse incomplete)
  let duplicateIDs := withBody [(0, .decl .const "seed" (.num 42)), (0, .decl .const "result" (.num 1))]
  expectError "source unit identities are duplicated" (legacyResolve duplicateIDs spec)
  let ambiguous := withBody [(0, .decl .const "seed" (.num 42)), (2, .decl .const "seed" (.num 1))]
  expectError "source binding seed is ambiguous" (legacyResolve ambiguous spec)
  let duplicateModules := { spec with modules := [("entry.mjs", [.name "seed"]), ("entry.mjs", [.name "result"])] }
  expectError "duplicate module name" (legacyResolve sparse duplicateModules)
  let unsupported := { spec with sequential := true }
  expectError "placement requires an unordered non-sequential spec" (legacyResolve sparse unsupported)
  let wrongID : Program := ⟨"entry.mjs", [⟨"worker.mjs", [], [(0, .decl .const "seed" (.num 42))], [("seed", "seed")]⟩,
    ⟨"entry.mjs", [⟨"worker.mjs", [("seed", "seed")]⟩], [(1, .decl .const "result" (.var "seed"))], [("seed", "seed")]⟩]⟩
  match checkPlaces chosen.assignment sparse wrongID with
  | .error _ => pure ()
  | .ok _ => throw (IO.userError "wrong output identity passed placement")
  let wrongCell : Program := ⟨"entry.mjs", [⟨"worker.mjs", [], [(0, .decl .const "seed" (.num 42))], [("seed", "seed")]⟩,
    ⟨"entry.mjs", [⟨"worker.mjs", [("seed", "seed")]⟩], [(2, .decl .const "result" (.var "seed"))], [("seed", "result")]⟩]⟩
  let links ← IO.ofExcept (checkProbePair sparse wrongCell)
  unless exportsMatchFrom links.before links.after == false &&
      exports World.probe wrongCell == some [("seed", ⟨3, 0⟩)] do
    throw (IO.userError "wrong live export cell was not distinguished")
  match CheckedSplit.checkedSplit sparse spec .allOutcomes with
  | .error "source must be an import-free entry with consecutive unit identities starting at zero" => pure ()
  | _ => throw (IO.userError "the unchanged production splitter should refuse sparse IDs")

#print axioms proveSparse
#print axioms provePriorDense

end ResolvedPlacementTest

def main : IO Unit := ResolvedPlacementTest.test
