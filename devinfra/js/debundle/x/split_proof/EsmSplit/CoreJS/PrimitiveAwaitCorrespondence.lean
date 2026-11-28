import EsmSplit.CoreJS.PrimitiveAwait
import EsmSplit.CoreJS.Scope

/-! Drain preservation for the shared evaluator's exact-address state
comparison. This uses the same scope agreement as the ordered split proof:
identical code sees identical addresses for its free names. -/

namespace CoreJS.PrimitiveAwait

variable {W : World}

/-- Correspondence for continuations after choosing the same allocation
identity and code on both sides. Extra unused bindings may differ. -/
structure _root_.CoreJS.Job.Corresponds (left right : Job) : Prop where
  promise : left.promise = right.promise
  region : left.region = right.region
  body : left.body = right.body
  captures : Agree (freeStmts left.body) left.env right.env

inductive QueueCorresponds : List Job → List Job → Prop where
  | nil : QueueCorresponds [] []
  | cons {head head' : Job} {tail tail' : List Job} : head.Corresponds head' → QueueCorresponds tail tail' →
      QueueCorresponds (head :: tail) (head' :: tail')

def _root_.CoreJS.Turn.Corresponds (left right : Turn W) : Prop :=
  left.state = right.state ∧ QueueCorresponds left.jobs right.jobs

/-- Retain exact state and refusal reason, and keep queue correspondence for
every stateful outcome, including an incomplete drain. -/
def Result.Corresponds {α : Type} : Result W α → Result W α → Prop
  | .ok value left, .ok value' right => value = value' ∧ left.Corresponds right
  | .unknown why left, .unknown why' right => why = why' ∧ left.Corresponds right
  | .fuel, .fuel => True
  | _, _ => False

theorem _root_.CoreJS.Job.capture_corresponds (job : Job) : job.capture.Corresponds job := by
  refine ⟨rfl, rfl, rfl, rfl, rfl, fun x hx => ?_⟩
  change x ∈ freeStmts job.body at hx
  show ((freeStmts job.body).eraseDups.filterMap fun y => (job.env.vars.lookup y).map (y, ·)).lookup x = _
  simp only [lookup_filterMap, List.mem_eraseDups, hx, ite_true]

/-- Resume uses the shared statement evaluator and settles the same Promise.
This covers body throws, including writes that precede the throw. -/
theorem onResume_corresponds {left right : Job} (h : left.Corresponds right)
    (fuel : Nat) (state : State W) : onResume left fuel state = onResume right fuel state := by
  rcases left with ⟨pa, ra, ea, ba⟩
  rcases right with ⟨pb, rb, eb, bb⟩
  obtain ⟨hp, hr, hb, captures⟩ := h
  change pa = pb at hp
  change ra = rb at hr
  change ba = bb at hb
  subst pb rb bb
  unfold onResume
  rw [(same (W := W) fuel).stmts ea eb ba captures]

theorem onResume_capture (job : Job) (fuel : Nat) (state : State W) :
    onResume job.capture fuel state = onResume job fuel state :=
  onResume_corresponds job.capture_corresponds fuel state

theorem runJob_corresponds {left right : Job} {tail tail' : List Job}
    (head : left.Corresponds right) (rest : QueueCorresponds tail tail')
    (fuel : Nat) (state : State W) :
    (runJob left fuel state tail).Corresponds (runJob right fuel state tail') := by
  rw [runJob_sync, runJob_sync]
  rw [← head.promise, onResume_corresponds head fuel state]
  split
  · cases onResume right fuel state <;> first
      | exact ⟨rfl, rfl, rest⟩
      | trivial
  · exact ⟨rfl, rfl, rest⟩

/-- With equal immediate states and corresponding queues in order, every FIFO
quota gives matching observations, including partial states and remaining
queues on refusals. Rejection is a successful scheduler step: the shared
completion rule stores it in the original Promise and keeps the body state. -/
theorem drain_corresponds (fuel quota : Nat) {left right : Turn W}
    (h : left.Corresponds right) :
    (drain fuel quota left).Corresponds (drain fuel quota right) := by
  induction quota generalizing left right with
  | zero =>
    obtain ⟨s, xs⟩ := left
    obtain ⟨t, ys⟩ := right
    obtain ⟨hst, jobs⟩ := h
    change s = t at hst
    subst t
    change QueueCorresponds xs ys at jobs
    cases jobs with
    | nil => exact ⟨rfl, rfl, .nil⟩
    | cons head rest => exact ⟨rfl, rfl, .cons head rest⟩
  | succ quota ih =>
    obtain ⟨s, xs⟩ := left
    obtain ⟨t, ys⟩ := right
    obtain ⟨hst, jobs⟩ := h
    change s = t at hst
    subst t
    change QueueCorresponds xs ys at jobs
    cases jobs with
    | nil => exact ⟨rfl, rfl, .nil⟩
    | @cons job job' rest rest' head tail =>
      simp only [drain_head]
      have step := runJob_corresponds head tail fuel s
      cases a : runJob job fuel s rest <;> cases b : runJob job' fuel s rest'
      all_goals simp only [a, b, Result.Corresponds] at step
      all_goals first
        | contradiction
        | exact step
        | exact ih step.2

end CoreJS.PrimitiveAwait
