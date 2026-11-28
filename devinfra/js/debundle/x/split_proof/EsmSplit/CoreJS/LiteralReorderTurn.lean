import EsmSplit.CoreJS.LiteralReorderConstruct
import EsmSplit.CoreJS.TurnPublication

/-! Migrate the existing literal reorder constructor without changing its
checks, the authored placement, or the evaluator it certifies. -/

namespace CoreJS.LiteralReorderConstruct

open ExactLiteralMove LiteralReorderLayout

private theorem completes {original request} (c : Construction original request) (w : World) :
    ∃ final, runProgram w 2 original = .ok () final := by
  rw [c.sourceEq, LinkedBehavior.run_after_link 2 (sourceLink w request.entry c.x c.y c.z c.a c.b)]
  let env := sourceEnv c.x c.y c.z
  have hx : env.vars.lookup c.x = some ⟨1, 0⟩ := by simp [env, sourceEnv]
  have hy : env.vars.lookup c.y = some ⟨2, 0⟩ := by
    simp [env, sourceEnv, List.lookup_cons, beq_eq_false_iff_ne.mpr (Ne.symm c.distinctXY)]
  have hz : env.vars.lookup c.z = some ⟨3, 0⟩ := by
    simp [env, sourceEnv, List.lookup_cons, beq_eq_false_iff_ne.mpr (Ne.symm c.distinctXZ),
      beq_eq_false_iff_ne.mpr (Ne.symm c.distinctYZ)]
  change
    ∃ final,
      runUnits 2
          [(0, decl c.x c.a, env), (1, decl c.y c.b, env), (2, .decl .const c.z (.var c.x), env)]
          (ready w) =
        .ok () final
  simp only [runUnits, M.bind_apply, run_literal 0 0 env c.x c.a _ hx,
    run_literal 0 1 env c.y c.b _ hy]
  simp only [runUnit, inRegion, execStmt, evalExpr, readVar, hx, initVar, hz, M.bind_apply,
    getState, modifyState]
  exact ⟨_, rfl⟩

/-- Exact FIFO observations follow from the existing result proof on this
synchronous kernel. The positive witness runs the actual literal syntax. -/
theorem Construction.turnPublication {original request} (c : Construction original request) :
    TurnPublication original c.program
    where
  exports := (sound c).2.1
  immediate w fuel := by rw [runProgramTurn_sync, runProgramTurn_sync, (sound c).2.2 w fuel]
  supported w := by
    obtain ⟨final, h⟩ := completes c w
    refine ⟨2, 0, 0, ⟨final, []⟩, ⟨final, []⟩, ?_, rfl⟩
    rw [runProgramTurn_sync, h]
    rfl

/-- Migration check on the actual reread AST. Both the legacy theorem and
the stronger checkpoint theorem can be obtained from the returned proof. -/
def Construction.checkTurnReadback {original request} (c : Construction original request)
    (files : Lean.Json) :=
  checkTurnPublication files original c.program c.turnPublication

end CoreJS.LiteralReorderConstruct
