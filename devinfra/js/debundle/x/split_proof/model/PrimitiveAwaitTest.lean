import Lean.Data.Json
import EsmSplit.CoreJS.Eval
import EsmSplit.CoreJS.PrimitiveAwait

open CoreJS Lean
open CoreJS.PrimitiveAwait

private def expectOk {α : Type} (result : Result World.probe α) :
    IO (α × Turn World.probe) :=
  match result with
  | .ok value turn => pure (value, turn)
  | .unknown why _ => throw (IO.userError why)
  | .fuel => throw (IO.userError "unexpected fuel exhaustion")

private def onState {α : Type} (m : M World.probe α) (turn : Turn World.probe) :
    IO (α × Turn World.probe) :=
  match m turn.state with
  | .ok value next => pure (value, { turn with state := next })
  | .unknown why _ => throw (IO.userError why)
  | .throw _ _ => throw (IO.userError "unexpected synchronous throw")
  | .fuel => throw (IO.userError "unexpected fuel exhaustion")

private def addTrace (mark : String) : Stmt :=
  .expr (.assign (.var "trace") (.binary .add (.var "trace") (.str mark)))

private def traceAt (env : Env) (turn : Turn World.probe) : IO String := do
  let (.str s, _) ← onState (readVar env "trace") turn |
    throw (IO.userError "expected a trace string")
  pure s

private def launchFrom (env : Env) (mark : String) (operand : Expr) (resume : List Stmt)
    (turn : Turn World.probe) : Result World.probe Val :=
  launch promiseProto (fun n => do
    let _ ← execStmts n env [addTrace mark]
    let value ← evalExpr n env operand
    pure (.suspended value env resume)) 60 turn

private def withTrace : IO (Env × Turn World.probe) := do
  let initial := { initialState World.probe 2 with region := 1 }
  let (addr, next) ← onState (newCell ⟨some (.str ""), true⟩) ⟨initial, []⟩
  pure (⟨[("trace", addr)], .undef, none⟩, next)

private def runSingle : IO Json := do
  let (env, turn) ← withTrace
  let (.obj promise, next) ← expectOk
    (launchFrom env "s" (.num 0) [addTrace "a", .ret (.num 11)] turn) |
    throw (IO.userError "expected a pending Promise")
  let atReturn ← traceAt env next
  unless isPending next.state promise do throw (IO.userError "await settled before the drain")
  let (_, caller) ← onState (execStmt 60 env (addTrace "e")) next
  let duringTurn ← traceAt env caller
  let (.paused, paused) ← expectOk (drain 60 0 caller) | throw (IO.userError "zero quota ran a job")
  unless (← traceAt env paused) == duringTurn && isPending paused.state promise do
    throw (IO.userError "zero quota changed the turn")
  let .fuel := drain 0 1 caller | throw (IO.userError "zero evaluator fuel ran a body")
  let (.empty, drained) ← expectOk (drain 60 1 caller) | throw (IO.userError "expected an empty queue")
  let afterDrain ← traceAt env drained
  let some (.fulfilled (.num value)) := (drained.state.objAt promise).bind (·.promise) |
    throw (IO.userError "expected fulfillment on the original Promise")
  let (.empty, again) ← expectOk (drain 60 1 drained) | throw (IO.userError "expected an empty drain")
  unless (← traceAt env again) == afterDrain do throw (IO.userError "resumed a continuation twice")
  return Json.mkObj [("atReturn", toJson atReturn), ("duringTurn", toJson duringTurn),
    ("afterDrain", toJson afterDrain), ("value", toJson value)]

private def runPair : IO Json := do
  let (env, turn) ← withTrace
  let (payload, turn) ← onState (alloc { proto := some objectProto, props := [] }) turn
  let (cell, turn) ← onState (newCell ⟨some (.obj payload), true⟩) turn
  let (marker, turn) ← onState (newCell ⟨some (.num 0), true⟩) turn
  let env := { env with vars := env.vars ++ [("payload", cell), ("marker", marker)] }
  let (.obj left, next) ← expectOk
    (launchFrom env "a" (.num 0) [addTrace "A", .throw (.var "payload")] turn) |
    throw (IO.userError "expected first pending Promise")
  let (.obj right, next) ← expectOk
    (launchFrom env "b" (.num 0)
      [addTrace "B", .expr (.obj []), .ret (.var "marker")] next) |
    throw (IO.userError "expected second pending Promise")
  let (_, caller) ← onState (do
    setCell marker (.num 23)
    execStmt 60 env (addTrace "|")) next
  let duringTurn ← traceAt env caller
  unless isPending caller.state left && isPending caller.state right do
    throw (IO.userError "the caller did not see two pending promises")
  -- End the caller's allocation phase. Resume in the saved region, then restore this one.
  let caller := { caller with state := { caller.state with region := 2 } }
  let (.paused, one) ← expectOk (drain 60 1 caller) | throw (IO.userError "expected one remaining job")
  unless (← traceAt env one) == "ab|A" && isPending one.state right && one.state.region == 2 do
    throw (IO.userError "the first drain must run the oldest job only")
  let some (.rejected (.obj reason)) := (one.state.objAt left).bind (·.promise) |
    throw (IO.userError "the first job must reject, not throw out of the scheduler")
  let (.empty, both) ← expectOk (drain 60 1 one) | throw (IO.userError "expected both jobs to finish")
  let some (.fulfilled (.num value)) := (both.state.objAt right).bind (·.promise) |
    throw (IO.userError "the second job did not fulfill")
  let afterDrain ← traceAt env both
  unless both.state.region == 2 do throw (IO.userError "the drain leaked the job region")
  return Json.mkObj [("duringTurn", toJson duringTurn), ("afterDrain", toJson afterDrain),
    ("fresh", toJson (left != right)), ("rejectionIdentity", toJson (reason == payload)),
    ("value", toJson value)]

private def unsupported : IO Unit := do
  let (env, turn) ← withTrace
  let .unknown "await operand requires thenable semantics" next :=
    launchFrom env "s" (.obj []) [addTrace "a"] turn |
    throw (IO.userError "the model tried to assimilate an object")
  unless (← traceAt env next) == "s" && next.jobs.isEmpty do
    throw (IO.userError "unsupported resolution ran or enqueued a continuation")
  let (.obj rejected, next) ← expectOk (launch promiseProto (fun n => do
    let _ ← execStmts n env [addTrace "x", .throw (.str "pre")]
    pure (.completed .undef)) 60 turn) |
    throw (IO.userError "pre-await throw must return a Promise")
  let some (.rejected (.str "pre")) := (next.state.objAt rejected).bind (·.promise) |
    throw (IO.userError "pre-await throw must reject")
  unless (← traceAt env next) == "x" && next.jobs.isEmpty do
    throw (IO.userError "pre-await throw queued a continuation")

def main : IO Unit := do
  unsupported
  let single ← runSingle
  let pair ← runPair
  IO.println (Json.mkObj [("single", single), ("pair", pair)]).compress
