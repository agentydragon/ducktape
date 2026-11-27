import EsmSplit.CoreJS.AsyncPrefix
import EsmSplit.CoreJS.Eval

/-!
An explicit FIFO for one discarded primitive `await` per activation. A caller
runs the prefix synchronously and keeps the returned pending Promise and the
queue. Only a separate drain runs a continuation. This scheduler is not a
host checkpoint and is not wired into ordinary module evaluation.

The continuation is a statement list and the activation's live environment,
not a snapshot of the heap or a Lean closure. The awaiting activation keeps
its allocation region across the cut. Awaited objects, including thenables
and Promises, have no rule here.
-/

namespace CoreJS.PrimitiveAwait

variable {W : World}

/-- The cut is immediately after evaluating the operand of a discarded await. -/
inductive Prefix where
  | completed (value : Val)
  | suspended (operand : Val) (env : Env) (body : List Stmt)

structure Job where
  promise : Addr
  region : Nat
  env : Env
  body : List Stmt

structure Turn (W : World) where
  state : State W
  jobs : List Job := []

/-- Evaluation fuel follows CoreJS: a fuel outcome makes no claim about an
intermediate state. The separate drain quota can pause with a resumable turn. -/
inductive Result (W : World) (α : Type) where
  | ok (value : α) (turn : Turn W)
  | unknown (reason : String) (turn : Turn W)
  | fuel

def isPrimitive : Val → Bool
  | .obj _ | .host _ | .error .. => false
  | _ => true

def isPending (s : State W) (promise : Addr) : Bool :=
  match (s.objAt promise).bind (·.promise) with
  | some .pending => true
  | _ => false

private def liftCompletion (jobs : List Job) : Res W Val → Result W Val
  | .ok v state => .ok v ⟨state, jobs⟩
  | .unknown why state => .unknown why ⟨state, jobs⟩
  | .throw _ state => .unknown "model: promise settlement threw" ⟨state, jobs⟩
  | .fuel => .fuel

private def suspend (promise : Addr) (operand : Val) (env : Env) (body : List Stmt)
    (turn : Turn W) : Result W Val :=
  if !isPrimitive operand then
    .unknown "await operand requires thenable semantics" turn
  else if !isPending turn.state promise then
    .unknown "model: await requires a pending promise" turn
  else .ok (.obj promise) { turn with jobs := turn.jobs ++ [⟨promise, turn.state.region, env, body⟩] }

/-- Allocate once, then run the prefix at smaller fuel. A synchronous throw
rejects the returned Promise and does not enqueue a continuation. -/
def launch (prototype : Addr) (prefixStep : Nat → M W Prefix) :
    Nat → Turn W → Result W Val
  | 0, _ => .fuel
  | fuel + 1, turn =>
    match AsyncPrefix.allocate prototype turn.state with
    | .ok promise s =>
      match prefixStep fuel s with
      | .ok (.completed value) t => liftCompletion turn.jobs (AsyncPrefix.finish promise (.ok value t))
      | .ok (.suspended operand env body) t => suspend promise operand env body ⟨t, turn.jobs⟩
      | .throw reason t => liftCompletion turn.jobs (AsyncPrefix.finish promise (.throw reason t))
      | .unknown why t => .unknown why ⟨t, turn.jobs⟩
      | .fuel => .fuel
    | .unknown why s => .unknown why ⟨s, turn.jobs⟩
    | .throw _ s => .unknown "model: promise allocation threw" ⟨s, turn.jobs⟩
    | .fuel => .fuel

/-- Resume with the current state and the original activation's region.
The result's region belongs to the scheduler. -/
def onResume (job : Job) (fuel : Nat) (s : State W) : Res W Val :=
  let body : M W Val := do
    let flow ← execStmts fuel job.env job.body
    Flow.functionResult flow
  AsyncPrefix.finish job.promise (body { s with region := job.region })

private def restoreRegion (region : Nat) (jobs : List Job) : Res W Val → Result W Unit
  | .ok _ state => .ok () ⟨{ state with region }, jobs⟩
  | .unknown why state => .unknown why ⟨{ state with region }, jobs⟩
  | .throw _ state => .unknown "model: promise settlement threw" ⟨{ state with region }, jobs⟩
  | .fuel => .fuel

def runJob (job : Job) (fuel : Nat) (s : State W) (rest : List Job) : Result W Unit :=
  if isPending s job.promise then restoreRegion s.region rest (onResume job fuel s)
  else .unknown "model: queued continuation requires a pending promise" ⟨s, rest⟩

inductive Drained where
  | empty
  | paused
  deriving BEq, Repr

/-- Explicit scheduler checkpoint. Quota counts dequeued jobs; each job
gets its own evaluator fuel. A language rejection settles the Promise and
continues draining. A model refusal or exhausted evaluator fuel stops. -/
def drain (fuel : Nat) : Nat → Turn W → Result W Drained
  | _, ⟨s, []⟩ => .ok .empty ⟨s, []⟩
  | 0, turn => .ok .paused turn
  | quota + 1, ⟨s, job :: rest⟩ =>
    match runJob job fuel s rest with
    | .ok () next => drain fuel quota next
    | .unknown why next => .unknown why next
    | .fuel => .fuel

@[simp] theorem launch_zero (prototype : Addr) (run : Nat → M W Prefix) (turn : Turn W) :
    launch prototype run 0 turn = .fuel := rfl

/-- Successful suspension hands the unchanged post-prefix state to the caller.
In particular, it cannot run the new job or modify the pending Promise. -/
theorem launch_suspends {prototype promise : Addr} {run : Nat → M W Prefix} {fuel : Nat}
    {turn : Turn W} {allocated post : State W} {operand : Val} {env : Env} {body : List Stmt}
    (fresh : AsyncPrefix.allocate prototype turn.state = .ok promise allocated)
    (reached : run fuel allocated = .ok (.suspended operand env body) post)
    (primitive : isPrimitive operand = true) (pending : isPending post promise = true) :
    launch prototype run (fuel + 1) turn =
      .ok (.obj promise) ⟨post, turn.jobs ++ [⟨promise, post.region, env, body⟩]⟩ := by
  simp [launch, fresh, reached, suspend, primitive, pending]

theorem drain_paused (fuel : Nat) (s : State W) (job : Job) (rest : List Job) :
    drain fuel 0 ⟨s, job :: rest⟩ = .ok .paused ⟨s, job :: rest⟩ := rfl

theorem drain_head (fuel quota : Nat) (s : State W) (job : Job) (rest : List Job) :
    drain fuel (quota + 1) ⟨s, job :: rest⟩ =
      match runJob job fuel s rest with
      | .ok () next => drain fuel quota next
      | .unknown why next => .unknown why next
      | .fuel => .fuel := rfl

/-- A successful job contributes exactly the resumed computation's state, with
only the allocation region restored. It never restores the old heap or world. -/
theorem runJob_keeps_result {job : Job} {fuel : Nat} {s next : State W} {rest : List Job} {v : Val}
    (pending : isPending s job.promise = true) (ran : onResume job fuel s = .ok v next) :
    runJob job fuel s rest = .ok () ⟨{ next with region := s.region }, rest⟩ := by
  simp [runJob, pending, ran, restoreRegion]

end CoreJS.PrimitiveAwait
