import ProofFixture.Instance
import FixtureRender
import EsmSplit.CoreJS.Move

open EsmSplit

namespace ProofFixture

/-- Exact source spelling and CoreJS interpretation of the three statements in this fixture. -/
private def statements : List (String × CoreJS.Stmt) := [
  ("const unused = 2;", .decl .const "unused" (.num 2)),
  ("const one = 1;", .decl .const "one" (.num 1)),
  ("const copy = one;", .decl .const "copy" (.var "one"))
]

def program := statements.map Prod.snd

theorem source_matches : Example.bundle.map (·.src) = statements.map Prod.fst := by decide +kernel

theorem extracted_footprints : CoreJS.FootprintsOfSyntax program Example.bundle := by decide +kernel

theorem execution_order : Example.plan.execOrder = [1, 0, 2] := by decide +kernel

/-- The independent declarations are inverted but do not conflict. -/
theorem safe_inversion : [1, 0].Sublist Example.plan.execOrder ∧
    Bundle.conflictT Example.bundle.toArray Example.bundle.readsTab 0 1 = false := by decide +kernel

/-- A hoisted binding exists before any code runs: it may be read or written early. Exercise the executable table
lookup in both argument orders. -/
def hoistedControl : Bundle := [
  { src := "", defs := ["fn"], hoisted := true },
  { src := "", eager := ["fn"] },
  { src := "", writes := ["fn"] }
]

theorem hoisted_read_both_directions :
    Bundle.conflictT hoistedControl.toArray hoistedControl.readsTab 0 1 = false ∧
    Bundle.conflictT hoistedControl.toArray hoistedControl.readsTab 1 0 = false := by decide +kernel

theorem hoisted_write_both_directions :
    Bundle.conflictT hoistedControl.toArray hoistedControl.readsTab 0 2 = false ∧
    Bundle.conflictT hoistedControl.toArray hoistedControl.readsTab 2 0 = false := by decide +kernel

/-- A write to a binding that is not hoisted conflicts with its declaration. -/
def declaredControl : Bundle := [
  { src := "", defs := ["x"] },
  { src := "", writes := ["x"] }
]

theorem declared_write_both_directions :
    Bundle.conflictT declaredControl.toArray declaredControl.readsTab 0 1 = true ∧
    Bundle.conflictT declaredControl.toArray declaredControl.readsTab 1 0 = true := by decide +kernel

/-- The unsafe generated control moves `copy` before its declaration of `one`.
Use the same original-order conflict query as the translation diagnostic. -/
theorem unsafe_inversion_rejected : ¬ OrderOk Example.bundle (mkPlan Example.bundle Example.control_bad) := by
  apply inverted_conflict_rejected Example.bundle (mkPlan Example.bundle Example.control_bad) 1 2
  all_goals decide +kernel

/-- Successful executions reach the same state at every fuel in every CoreJS world. -/
theorem preserves_model {W : CoreJS.World} (fuel : Nat) (state : CoreJS.State W) :
    CoreJS.runProgram W fuel (.single program) = .ok () state ↔
    CoreJS.runProgram W fuel (CoreJS.splitProgram program Example.bundle Example.plan) = .ok () state :=
  CoreJS.moves_preserve_run Example.plan_valid extracted_footprints (by decide +kernel) fuel state

/-- The same plan passed to the renderer is the plan checked by the generated instance. -/
theorem accepted : Valid Example.bundle Example.spec Example.plan := Example.plan_valid

/-- Concrete source text produced by the fixture renderer for this plan. -/
theorem rendered_base : (Example.plan.find "base.mjs").map (FixtureRender.render Example.bundle Example.plan) =
    some "const one = 1;\n\nexport { one };\n" := by decide +kernel

theorem rendered_entry : (Example.plan.find "main.mjs").map (FixtureRender.render Example.bundle Example.plan) =
    some "import { one } from \"./base.mjs\";\n\nconst unused = 2;\nconst copy = one;\n" := by
  decide +kernel

end ProofFixture
