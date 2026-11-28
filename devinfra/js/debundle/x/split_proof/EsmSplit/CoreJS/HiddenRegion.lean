import EsmSplit.CoreJS.EvaluatorFrame
import EsmSplit.CoreJS.LinkedPivotCheck

/-! Temporarily omit one inaccessible region when checking a move. The same
row is restored before the suffix, where the observer may be called. -/

namespace CoreJS.LinkedBehavior

private theorem unit_restore {w : World} {n q} (hq : q ≠ 0) {u : LinkedUnit}
    (safe : IsolatedUnit q u) {s : State w} (access : Access [] (Outside q) s) (H C) :
    runUnit n u.env u.id u.stmt (s.put q H C) = (runUnit n u.env u.id u.stmt s).put q H C ∧
      (∀ f t, runUnit n u.env u.id u.stmt s = .ok f t → Access [] (Outside q) t) := by
  obtain ⟨id, st, env⟩ := u
  let e := env.capture st.free
  have frames := evaluatorFrames w hq (R := fun _ => False) (fun _ _ h => h) n
  have frame := frames.stmt [] e st safe.2
  have orig : (execStmt n e st : M w Flow) = execStmt n env st :=
    capture_faithful n env st st.free (fun _ h => h)
  rw [orig] at frame
  refine ⟨?_, fun _ _ run => frame.runUnit_access safe.1 access run⟩
  let localState := { s with region := id + 1 }
  have scope : Access [] (Outside q) localState := by
    refine ⟨access.handed, access.supplied, ?_⟩
    intro src dst hs edge
    cases edge with
    | obj h m => exact access.closed _ _ hs (.obj h m)
    | cell h m => exact access.closed _ _ hs (.cell h m)
  have same := frame.replace localState H C scope safe.1
  change
    ((execStmt n env st >>= _) (localState.put q H C)) =
      ((execStmt n env st >>= _) localState).put q H C
  simp only [M.bind_apply, same]
  cases execStmt n env st localState <;> rfl

/-- Run the checked segment with the observer region omitted, then restore it
before the unchecked suffix. This concerns successful completions in both directions. -/
theorem runUnits_restore {w : World} {q} (hq : q ≠ 0) (n : Nat) (us suffix : List LinkedUnit)
    (safe : ∀ u ∈ us, IsolatedUnit q u) (s final : State w) (access : Access [] (Outside q) s)
    (H C) :
    runUnits n (us ++ suffix) (s.put q H C) = .ok () final ↔
      ∃ t, runUnits n us s = .ok () t ∧ runUnits n suffix (t.put q H C) = .ok () final := by
  induction us generalizing s with
  | nil => simp [runUnits, Pure.pure]
  | cons u us ih =>
    obtain ⟨id, st, env⟩ := u
    have ⟨same, keep⟩ := unit_restore (n := n) hq (safe _ List.mem_cons_self) access H C
    simp only [List.cons_append, runUnits, M.bind_apply, same]
    cases h : runUnit n env id st s with
    | ok f t => exact ih (fun u hu => safe u (List.mem_cons_of_mem _ hu)) t (keep f t h)
    | throw
    | fuel
    | unknown => simp [Res.put]

end CoreJS.LinkedBehavior
