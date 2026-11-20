import IdentityFixture.Instance
import Experiments.InferredIdentity
import Experiments.MiniJSSource

open EsmSplit MJS

namespace IdentityFixture

/-- This translation accepts only a tiny syntactic subset; it is not an ECMAScript semantics. -/
def program : List MStmt := (sourceBundle IdentityExample.bundle).getD []

theorem source_accepted : sourceBundle IdentityExample.bundle = some program := by decide +kernel

theorem program_value : program = [
    ⟨["identity"], .lam "x" (.lvar "x"), true⟩,
    ⟨["first"], .app (.gvar "identity") (.num 1), false⟩,
    ⟨["second"], .app (.gvar "identity") (.num 2), false⟩
  ] := by decide +kernel

theorem extracted_footprints : IdentityExample.bundle.map (fun s => { s with src := "" }) = toBundle program := by
  decide +kernel

theorem automatically_discovered : (deriveIdentity program 1).isSome = true := by decide +kernel

def plan : Plan := mkPlan IdentityExample.bundle IdentityExample.control_crossing

theorem crossing_order : plan.execOrder = [0, 2, 1] := by decide +kernel

theorem crossing_perm : Permutes (toBundle program) plan := by decide +kernel

theorem crossing_conditional : ConditionalOrderOk (toBundle program) plan (fun i => i == 1) := by decide +kernel

theorem well_formed : WF program := by
  intro st hst hh
  simp [program_value] at hst
  rcases hst with rfl | rfl | rfl <;> simp_all

theorem crossing_preserves (fuel : Nat) (w0 : World) :
    plan.execOrder.foldl (fun s i => cstep program fuel i s) (G0 program, w0) =
      (List.range program.length).foldl (fun s i => cstep program fuel i s) (G0 program, w0) := by
  apply derived_identity_plan_preserves automatically_discovered (by decide +kernel) well_formed plan
  · exact crossing_perm
  · exact crossing_conditional

end IdentityFixture
