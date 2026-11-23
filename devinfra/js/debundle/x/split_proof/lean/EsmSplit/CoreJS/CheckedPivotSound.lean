import EsmSplit.CoreJS.LinkedPivotCheck
import EsmSplit.CoreJS.EvaluatorFrame

namespace CoreJS.LinkedBehavior

/-- The finite checks suffice for the same successful final states. The
unmoved code uses the same evaluator and world protocol in both layouts. -/
theorem normalEquivalent_of_checkedPivot {source candidate : Program}
    (checks : ∀ w, CheckedPivot source candidate w) : NormalEquivalent source candidate := by
  refine normalEquivalent_of_pivot (fun w => (checks w).layout) (fun _ => False)
    (fun w => Unprotected ((checks w).layout.pivot.1 + 1) (fun _ => False))
    (fun w => (checks w).access) (fun w => localPivotOk_sound (checks w).localFlag) ?_
  intro w n u hu
  obtain ⟨away, scope⟩ := (checks w).scope u hu
  refine ⟨fun eq => away (congrArg (· + 1) eq), [], by simp, ?_⟩
  rw [← capture_faithful n u.2.2 u.2.1 u.2.1.free (fun _ h => h)]
  have frames := evaluatorFrames w (r := (checks w).layout.pivot.1 + 1) (by omega)
    (R := fun _ => False) (fun _ _ h => h) n
  have step := frames.stmt [] (u.2.2.capture u.2.1.free) u.2.1 scope
  refine ⟨step.replace, step.preserves, ?_⟩
  intro s access away
  have result := step.accessible s access away
  cases h : execStmt n (u.2.2.capture u.2.1.free) u.2.1 s <;> rw [h] at result
  case ok v t => exact ⟨trivial, result.2⟩
  all_goals exact result

end CoreJS.LinkedBehavior
