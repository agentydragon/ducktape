import EsmSplit.CoreJS.LinkedBehavior
import EsmSplit.CoreJS.RequestFrame
import EsmSplit.CoreJS.Split

namespace CoreJS.LinkedBehavior

/-- The same normal outcomes at any evaluator bound, including the full final
binding, heap, output and world state. A source that throws before the pivot
does not promise the candidate throws with an identical partial state. -/
def NormalEquivalent (source candidate : Program) : Prop :=
  ∀ w (final : State w), Ends w source (.ok () final) ↔ Ends w candidate (.ok () final)

/-- The shape of a single earlier move, witnessed by the actual linked programs.
The common schedule uses source scopes. Candidate scopes agree on exactly the
names each retained statement means. -/
structure PivotLayout (source candidate : Program) (w : World) where
  sourceLink : CheckedLink source w
  candidateLink : CheckedLink candidate w
  initial : sourceLink.ready = candidateLink.ready
  pre : List (Nat × Stmt × Env)
  crossed : List (Nat × Stmt × Env)
  pivot : Nat × Stmt × Env
  suffix : List (Nat × Stmt × Env)
  sourceOrder : SameUnits (unitsOf sourceLink.linked) (pre ++ crossed ++ [pivot] ++ suffix)
  candidateOrder : SameUnits (unitsOf candidateLink.linked) (pre ++ [pivot] ++ crossed ++ suffix)

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

theorem normalEquivalent_of_pivot {source candidate : Program}
    (layout : ∀ w, PivotLayout source candidate w) (reads : Addr → Prop) (allowed : World → HeapNode → Prop)
    (access : ∀ w, Access [] (allowed w) (layout w).sourceLink.ready)
    (localPivot : ∀ w n, Local ((layout w).pivot.1 + 1) reads
      (execStmt n (layout w).pivot.2.2 (layout w).pivot.2.1 : M w Flow))
    (unchanged : ∀ w n, FramedUnits w n (layout w).pivot.1 reads (allowed w)
      ((layout w).pre ++ (layout w).crossed)) : NormalEquivalent source candidate := by
  intro w final
  let layout' := layout w
  have run (n : Nat) :
      runProgram w n source = .ok () final ↔ runProgram w n candidate = .ok () final := by
    rw [run_after_link n layout'.sourceLink, run_after_link n layout'.candidateLink,
      runUnits_same n layout'.sourceOrder, runUnits_same n layout'.candidateOrder, ← layout'.initial]
    have beforeFrames : FramedUnits w n layout'.pivot.1 reads (allowed w) layout'.pre :=
      fun u hu => unchanged w n u (List.mem_append_left _ hu)
    have crossedFrames : FramedUnits w n layout'.pivot.1 reads (allowed w) layout'.crossed :=
      fun u hu => unchanged w n u (List.mem_append_right _ hu)
    have frame := runUnits_reachFrame_pivot n layout'.pivot.1 layout'.pivot.2.2 layout'.pivot.2.1 reads (allowed w)
      layout'.pre layout'.crossed layout'.suffix layout'.sourceLink.ready final
      (localPivot w n) (fun _ hs => Access.afterUnits beforeFrames (access w) hs)
    apply (frame ?_).symm
    intro unit hu
    obtain ⟨different, frames⟩ := crossedFrames unit hu
    exact ⟨Ne.symm different, frames⟩
  constructor
  · rintro ⟨n, before, existsOutcome⟩
    exact ⟨n, (run n).mp before, existsOutcome⟩
  · rintro ⟨n, after, existsOutcome⟩
    exact ⟨n, (run n).mpr after, existsOutcome⟩

end CoreJS.LinkedBehavior
