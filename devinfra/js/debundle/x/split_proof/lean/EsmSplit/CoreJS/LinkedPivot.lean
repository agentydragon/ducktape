import EsmSplit.CoreJS.LinkedBehavior
import EsmSplit.CoreJS.Request.Frame
import EsmSplit.CoreJS.Split

namespace CoreJS.LinkedBehavior

/-- The same normal outcomes at any evaluator bound, including the full final
binding, heap, output and world state. A source that throws before the pivot
does not promise the candidate throws with an identical partial state. -/
def NormalEquivalent (worlds : World → Prop) (source candidate : Program) : Prop :=
  ∀ w, worlds w → ∀ (final : State w), Ends w source (.ok () final) ↔ Ends w candidate (.ok () final)

theorem normalEquivalent_of_bounded {worlds : World → Prop} {source candidate : Program}
    (run : ∀ w, worlds w → ∀ n (final : State w),
      runProgram w n source = .ok () final ↔ runProgram w n candidate = .ok () final) :
    NormalEquivalent worlds source candidate := by
  intro w hw final
  constructor
  · rintro ⟨n, before, existsOutcome⟩
    exact ⟨n, (run w hw n final).mp before, existsOutcome⟩
  · rintro ⟨n, after, existsOutcome⟩
    exact ⟨n, (run w hw n final).mpr after, existsOutcome⟩

def FramedUnits (w : World) (fuel pivotId : Nat) (R : Addr → Prop) (A : HeapNode → Prop)
    (steps : List (Nat × Stmt × Env)) : Prop :=
  ∀ u ∈ steps, u.1 ≠ pivotId ∧ ∃ cells,
    (∀ a, some a ∈ cells → A (.cell a)) ∧
    ReachFrame (pivotId + 1) R cells A (fun _ => True) (execStmt fuel u.2.2 u.2.1 : M w Flow)

private theorem Access.afterUnits {w : World} {n i R A units} {s t : State w}
    (frames : FramedUnits w n i R A units) (ready : Access [] A s)
    (run : runUnits n units s = .ok () t) : Access [] A t := by
  induction units generalizing s with
  | nil => cases run; exact ready
  | cons u us ih =>
    obtain ⟨id, st, env⟩ := u
    obtain ⟨flow, mid, step, tail⟩ := bind_ok run
    obtain ⟨different, cells, roots, hf⟩ := frames _ List.mem_cons_self
    have access := hf.runUnit_access (show id + 1 ≠ i + 1 by omega) (s := s)
      ⟨ready.handed, roots, ready.closed⟩ step
    exact ih (fun x hx => frames x (List.mem_cons_of_mem _ hx)) ⟨access.handed, by simp, access.closed⟩ tail

/-- An earlier move preserves successful final states from the given linked state. -/
theorem runUnits_pivot_normal {W : World} {s : State W}
    {pre crossed suffix : List (Nat × Stmt × Env)} {pivot : Nat × Stmt × Env}
    (n : Nat) (final : State W) (reads : Addr → Prop) (allowed : HeapNode → Prop)
    (access : Access [] allowed s)
    (localPivot : Local (pivot.1 + 1) reads (execStmt n pivot.2.2 pivot.2.1 : M W Flow))
    (unchanged : FramedUnits W n pivot.1 reads allowed (pre ++ crossed)) :
    runUnits n (pre ++ crossed ++ [pivot] ++ suffix) s = .ok () final ↔
      runUnits n (pre ++ [pivot] ++ crossed ++ suffix) s = .ok () final := by
  have beforeFrames : FramedUnits W n pivot.1 reads allowed pre :=
    fun u hu => unchanged u (List.mem_append_left _ hu)
  have crossedFrames : FramedUnits W n pivot.1 reads allowed crossed :=
    fun u hu => unchanged u (List.mem_append_right _ hu)
  have frame := runUnits_reachFrame_pivot n pivot.1 pivot.2.2 pivot.2.1 reads allowed
    pre crossed suffix s final localPivot (fun _ hs => Access.afterUnits beforeFrames access hs)
  apply (frame ?_).symm
  intro unit hu
  obtain ⟨different, frames⟩ := crossedFrames unit hu
  exact ⟨Ne.symm different, frames⟩

end CoreJS.LinkedBehavior
