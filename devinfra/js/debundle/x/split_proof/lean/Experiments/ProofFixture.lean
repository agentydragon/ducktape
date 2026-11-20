import ProofFixture.Instance
import Experiments.MiniJSSource
import EsmSplit.Render

open EsmSplit MJS

namespace ProofFixture

/-- Derived from the exact source strings in the extractor-generated bundle. -/
def program : List MStmt := (sourceBundle Example.bundle).getD []

theorem source_accepted : sourceBundle Example.bundle = some program := by decide +kernel

theorem program_value : program = [
    ⟨["unused"], .num 2, false⟩,
    ⟨["one"], .num 1, false⟩,
    ⟨["copy"], .gvar "one", false⟩
  ] := by decide +kernel

theorem unsupported_nonconst : sourceStmt "let x = 1;" = none := by decide +kernel
theorem unsupported_extra_tokens : sourceStmt "const x = 1;alert(1);" = none := by decide +kernel
theorem unsupported_call : sourceStmt "const x = f();" = none := by decide +kernel
theorem unsupported_coercion : sourceStmt "const x = y+1;" = none := by decide +kernel
theorem unsupported_number : sourceStmt "const x = 9007199254740993;" = none := by decide +kernel
theorem unsupported_escape : sourceStmt "const x = \\u0061;" = none := by decide +kernel
theorem unsupported_keyword : sourceStmt "const x = true;" = none := by decide +kernel
theorem unsupported_reserved_name : sourceStmt "const new = 1;" = none := by decide +kernel
theorem unsupported_strict_name : sourceStmt "const eval = 1;" = none := by decide +kernel

/-- `src` is rendered verbatim; MiniJS footprints intentionally contain no source text. -/
theorem extracted_footprints : Example.bundle.map (fun s => { s with src := "" }) = toBundle program := by
  rfl

theorem well_formed : WF program := by
  intro st hst hh
  simp [program_value] at hst
  rcases hst with rfl | rfl | rfl <;> simp_all

theorem reads_closed : ReadsClosed (toBundle program) := by decide +kernel

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

theorem preserves_model (fuel : Nat) :
    Example.plan.execOrder.foldl (fun s j => cstep program fuel j s) (G0 program, []) =
      (List.range program.length).foldl (fun s j => cstep program fuel j s) (G0 program, []) := by
  apply mjs_reorder reads_closed well_formed fuel
  · decide +kernel
  · intro x
    simp [execution_order, program_value]
    omega
  · decide +kernel

/-- The same plan passed to the renderer is the plan checked by the generated instance. -/
theorem accepted : Valid Example.bundle Example.spec Example.plan := Example.plan_valid

/-- Concrete source text produced by the actual renderer for this plan. -/
theorem rendered_base : (Example.plan.find "base.mjs").map (Module.render Example.bundle) =
    some "const one = 1;\n\nexport { one };\n" := by decide +kernel

theorem rendered_entry : (Example.plan.find "main.mjs").map (Module.render Example.bundle) =
    some "import { one } from \"./base.mjs\";\n\nconst unused = 2;\nconst copy = one;\n" := by
  decide +kernel

end ProofFixture
