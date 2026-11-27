import EsmSplit.CoreJS.Value

/-! The state of one CoreJS turn. The queue is observed alongside the
language state, not hidden in it or projected away by an admission rule. -/

namespace CoreJS

/-- A suspended activation keeps addresses, not copies of binding values. -/
structure Job where
  promise : Addr
  region : Nat
  env : Env
  body : List Stmt

def Job.capture (job : Job) : Job :=
  { job with env := job.env.capture (freeStmts job.body) }

structure Turn (W : World) where
  state : State W
  jobs : List Job := []

/-- Throws and model refusals retain the turn. As in `Res`, exhausted
evaluator fuel makes no claim about intermediate state. -/
inductive TurnResult (W : World) (α : Type) where
  | ok (value : α) (turn : Turn W)
  | throw (reason : Val) (turn : Turn W)
  | unknown (reason : String) (turn : Turn W)
  | fuel

namespace TurnResult

variable {W : World} {α β : Type}

/-- Project the ordinary observation. This projection is not an admission rule. -/
def erase : TurnResult W α → Res W α
  | .ok a turn => .ok a turn.state
  | .throw reason turn => .throw reason turn.state
  | .unknown reason turn => .unknown reason turn.state
  | .fuel => .fuel

def withJobs (jobs : List Job) : Res W α → TurnResult W α
  | .ok a state => .ok a ⟨state, jobs⟩
  | .throw reason state => .throw reason ⟨state, jobs⟩
  | .unknown reason state => .unknown reason ⟨state, jobs⟩
  | .fuel => .fuel

@[simp] theorem erase_withJobs (jobs : List Job) (r : Res W α) :
    (withJobs jobs r).erase = r := by cases r <;> rfl

def andThen (step : M W α) (turn : Turn W)
    (next : α → Turn W → TurnResult W β) : TurnResult W β :=
  match step turn.state with
  | .ok a s => next a { turn with state := s }
  | .throw reason s => .throw reason { turn with state := s }
  | .unknown why s => .unknown why { turn with state := s }
  | .fuel => .fuel

def map (f : α → β) : TurnResult W α → TurnResult W β
  | .ok a turn => .ok (f a) turn
  | .throw reason turn => .throw reason turn
  | .unknown why turn => .unknown why turn
  | .fuel => .fuel

/-- Forget only the successful return value, keeping all state, jobs,
and abrupt outcomes. Callers check public bindings separately. -/
def observed (outcome : TurnResult W α) : TurnResult W Unit :=
  outcome.map fun _ => ()

end TurnResult
end CoreJS
