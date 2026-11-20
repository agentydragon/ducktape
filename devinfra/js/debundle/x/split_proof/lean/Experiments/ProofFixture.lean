import ProofFixture.Instance
import Experiments.MiniJS
import EsmSplit.Render

open EsmSplit MJS

namespace ProofFixture

/-- Corresponding MiniJS program. Its footprint must match the actual extractor output. -/
def program : List MStmt := [
  ⟨["unused"], .num 2, false⟩,
  ⟨["one"], .num 1, false⟩,
  ⟨["copy"], .gvar "one", false⟩
]

/-- `src` is rendered verbatim; MiniJS footprints intentionally contain no source text. -/
theorem extracted_footprints : Example.bundle.map (fun s => { s with src := "" }) = toBundle program := by
  rfl

theorem well_formed : WF program := by
  intro st hst hh
  simp [program] at hst
  rcases hst with rfl | rfl | rfl <;> simp_all

theorem reads_closed : ReadsClosed (toBundle program) := by decide +kernel

theorem execution_order : Example.plan.execOrder = [1, 0, 2] := by decide +kernel

theorem preserves_model (fuel : Nat) :
    Example.plan.execOrder.foldl (fun s j => cstep program fuel j s) (G0 program, []) =
      (List.range program.length).foldl (fun s j => cstep program fuel j s) (G0 program, []) := by
  apply mjs_reorder reads_closed well_formed fuel
  · decide +kernel
  · intro x
    simp [execution_order, program]
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
