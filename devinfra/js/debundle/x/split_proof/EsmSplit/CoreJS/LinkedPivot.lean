import EsmSplit.CoreJS.LinkedBehavior
import EsmSplit.CoreJS.Request.Frame
import EsmSplit.CoreJS.Split

namespace CoreJS.LinkedBehavior

def FramedUnits (w : World) (fuel pivotId : Nat) (R : Addr → Prop) (A : HeapNode → Prop)
    (steps : List LinkedUnit) : Prop :=
  ∀ u ∈ steps, u.id ≠ pivotId ∧ ∃ cells,
    (∀ a, some a ∈ cells → A (.cell a)) ∧
    ReachFrame (pivotId + 1) R cells A (fun _ => True) (execStmt fuel u.env u.stmt : M w Flow)

private theorem Access.afterUnits {w : World} {n i R A units} {s t : State w}
    (frames : FramedUnits w n i R A units) (ready : Access [] A s)
    (run : runUnits n units s = .ok () t) : Access [] A t := by
  induction units generalizing s with
  | nil => cases run; exact ready
  | cons u us ih =>
    obtain ⟨id, st, env⟩ := u
    obtain ⟨flow, mid, step, tail⟩ := bind_ok run
    obtain ⟨different, cells, roots, hf⟩ := frames _ List.mem_cons_self
    have access := hf.runUnit_access (show id + 1 ≠ i + 1 from fun h => different (Nat.succ.inj h)) (s := s)
      ⟨ready.handed, roots, ready.closed⟩ step
    exact ih (fun x hx => frames x (List.mem_cons_of_mem _ hx)) ⟨access.handed, by simp, access.closed⟩ tail

/-- An earlier move preserves successful final states from the given linked state. -/
theorem runUnits_pivot_normal {W : World} {s : State W}
    {pre crossed suffix : List LinkedUnit} {pivot : LinkedUnit}
    (n : Nat) (final : State W) (reads : Addr → Prop) (allowed : HeapNode → Prop)
    (access : Access [] allowed s)
    (localPivot : Local (pivot.id + 1) reads (execStmt n pivot.env pivot.stmt : M W Flow))
    (unchanged : FramedUnits W n pivot.id reads allowed (pre ++ crossed)) :
    runUnits n (pre ++ crossed ++ [pivot] ++ suffix) s = .ok () final ↔
      runUnits n (pre ++ [pivot] ++ crossed ++ suffix) s = .ok () final := by
  have beforeFrames : FramedUnits W n pivot.id reads allowed pre :=
    fun u hu => unchanged u (List.mem_append_left _ hu)
  have crossedFrames : FramedUnits W n pivot.id reads allowed crossed :=
    fun u hu => unchanged u (List.mem_append_right _ hu)
  have frame := runUnits_reachFrame_pivot n pivot.id pivot.env pivot.stmt reads allowed
    pre crossed suffix s final localPivot (fun _ hs => Access.afterUnits beforeFrames access hs)
  apply (frame ?_).symm
  intro unit hu
  obtain ⟨different, frames⟩ := crossedFrames unit hu
  exact ⟨Ne.symm different, frames⟩

end CoreJS.LinkedBehavior
