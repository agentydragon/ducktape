import EsmSplit.CoreJS.CallTurn
import EsmSplit.CoreJS.StateMatch

open CoreJS

private def emptyEnv : Env := ⟨[], .undef, none⟩

private def log (line : String) : Stmt := .expr (.prim .log [.str line])

private def jobs : List Job :=
  [⟨⟨2, 0⟩, 2, emptyEnv, []⟩, ⟨⟨3, 0⟩, 3, emptyEnv, []⟩]

private def assertQueue (turn : Turn W) : IO Unit := do
  unless turn.jobs.map Job.promise == jobs.map Job.promise do
    throw (IO.userError "an existing FIFO job was lost or reordered")

/-- The caller goes through a cell alias and crosses a finalizer on either
a nested return or a nested throw. -/
private def throughAlias (returns : Bool) : M World.probe Val := do
  let value := if returns then Stmt.ret (.str "value") else Stmt.throw (.str "value")
  let inner ← makeFunc emptyEnv (.mk "inner" .normal false [] [log "inner", value])
  let env ← declare emptyEnv [("alias", ⟨some inner, false⟩)]
  makeFunc env (.mk "caller" .normal false []
    [.tryFinally (.ret (.call (.var "alias") [])) (log "finally")])

private def checkAlias (returns : Bool) : IO Unit := do
  let .ok callee state := throughAlias returns (initialState World.probe 0) |
    throw (IO.userError "could not install the alias fixture")
  let turn := ⟨state, jobs⟩
  match callFnTurn 48 callee .undef [] turn with
  | .ok (.str "value") final =>
    unless returns && final.state.out == #["inner", "finally"] do
      throw (IO.userError "wrong return or output from aliased call")
    assertQueue final
  | .throw (.str "value") final =>
    unless !returns && final.state.out == #["inner", "finally"] do
      throw (IO.userError "wrong unwind or output from aliased call")
    assertQueue final
  | _ => throw (IO.userError "unexpected result from aliased call")

private def callbackWorld (badHandle : Bool) : World where
  σ := Nat
  init := 0
  step := fun _ _ _ s => (.unknown "host stepped before refusing", s + 1)
  talk := fun told s => match told with
    | .run _ _ _ receiver _ _ =>
      (.call (if badHandle then .handle 50 else receiver) (.prim .undef) [], s + 10)
    | _ => (.unknown "opaque stopped after callback", s + 10)

private def checkReentry (badHandle : Bool) : IO Unit := do
  let w := callbackWorld badHandle
  let setup : M w (Val × Val) := do
    let wrapper ← makeOpaque 0 1 [] "opaque" 0 false
    let callback ← makeFunc emptyEnv (.mk "callback" .normal false []
      [log "callback", .ret (.bool true)])
    pure (wrapper, callback)
  let .ok (wrapper, callback) state := setup (initialState w 0) |
    throw (IO.userError "could not install the callback fixture")
  let .unknown why final := callFnTurn 48 wrapper callback [] ⟨state, jobs⟩ |
    throw (IO.userError "the host conversation should refuse")
  let expected := if badHandle then "the world names an object it was not handed"
    else "opaque stopped after callback"
  unless why == expected && (show Nat from final.state.world.state) == (if badHandle then 10 else 20) &&
      final.state.out == (if badHandle then #[] else #["callback"]) &&
      final.state.world.handed.size == 2 do
    throw (IO.userError "the host conversation's effects changed")
  assertQueue final

private def marker : Job := ⟨⟨4, 0⟩, 4, emptyEnv, []⟩

-- A test effect, injected after the real inner statement. This is not a new
-- JS operation or a production enqueue rule.
private def appendMarker : TurnM W Unit := fun turn => .ok () { turn with jobs := turn.jobs ++ [marker] }

private def assertExtended (turn : Turn W) (out : Array String) : IO Unit := do
  unless turn.jobs.map Job.promise == (jobs ++ [marker]).map Job.promise && turn.state.out == out do
    throw (IO.userError "inner side effect was lost, reordered, or replayed")

-- These handlers compose fixed edges of the common rules. No handler
-- interprets a syntax tree or substitutes a new operation for JS or host code.
private def nestedCall (fuel : Nat) (callee receiver : Val) (args : List Val) : TurnM W Val :=
  dispatchCall TurnM.lift
    (ordinaryCallBody TurnM.lift (fun env ss => match ss with
      | first :: rest => continueBody
          (do let flow ← TurnM.lift (execStmt (fuel - 2) env first); appendMarker; pure flow)
          (TurnM.lift (execStmts (fuel - 2) env rest))
      | [] => TurnM.lift (execStmts (fuel - 1) env [])))
    (fun c cells told => TurnM.lift (converse (fuel - 1) c cells told)) callee receiver args

private inductive Ending where
  | returns | throws | refuses
  deriving BEq

private def endingStmt : Ending → Stmt
  | .returns => .ret (.str "value")
  | .throws => .throw (.str "value")
  | .refuses => .expr (.call (.host "not modeled") [])

private def checkUpdatedBody (ending : Ending) : IO Unit := do
  let setup : M World.probe Val := do
    let inner ← makeFunc emptyEnv (.mk "inner" .normal false [] [log "inner", endingStmt ending])
    let env ← declare emptyEnv [("alias", ⟨some inner, false⟩)]
    makeFunc env (.mk "outer" .normal false []
      [.tryFinally (.ret (.call (.var "alias") [])) (log "finally")])
  let .ok outer state := setup (initialState World.probe 0) | throw (IO.userError "setup failed")
  let fuel := 48
  let run := dispatchCall TurnM.lift
    (ordinaryCallBody TurnM.lift (fun env _ =>
      -- call / list / finally / return / call-expression each consume one.
      let body : TurnM World.probe Flow := do
        let v ← directExprCall (nestedCall (fuel - 5))
          (TurnM.lift (evalExpr (fuel - 5) env (.var "alias")))
          (TurnM.lift (evalArgs (fuel - 5) env []))
        pure (.ret v)
      continueBody (finallyTurn body (TurnM.lift (execStmt (fuel - 3) env (log "finally"))))
        (TurnM.lift (execStmts (fuel - 2) env []))))
    (fun c cells told => TurnM.lift (converse (fuel - 1) c cells told)) outer .undef [] ⟨state, jobs⟩
  match run, callFn fuel outer .undef [] state with
  | .ok (.str "value") final, .ok (.str "value") legacy =>
    unless ending == .returns && (StateMatch.probe legacy final.state).isSome do throw (IO.userError "wrong return")
    assertExtended final #["inner", "finally"]
  | .throw (.str "value") final, .throw (.str "value") legacy =>
    unless ending == .throws && (StateMatch.probe legacy final.state).isSome do throw (IO.userError "wrong throw")
    assertExtended final #["inner", "finally"]
  | .unknown "host function not modeled is called" final, .unknown "host function not modeled is called" legacy =>
    unless ending == .refuses && (StateMatch.probe legacy final.state).isSome do throw (IO.userError "wrong refusal")
    assertExtended final #["inner"]
  | _, _ => throw (IO.userError "the body outcome disagrees with the legacy call")

private def checkUpdatedCallback : IO Unit := do
  let w := callbackWorld false
  let setup : M w (Val × Val) := do
    let wrapper ← makeOpaque 0 1 [] "opaque" 0 false
    let callback ← makeFunc emptyEnv (.mk "callback" .normal false []
      [log "callback", .ret (.bool true)])
    pure (wrapper, callback)
  let .ok (wrapper, callback) state := setup (initialState w 0) |
    throw (IO.userError "unable to build the callback fixture")
  let fuel := 48
  let run := dispatchCall TurnM.lift
    (fun f env receiver args => TurnM.lift (runLocalCall (execStmts (fuel - 1)) f env receiver args))
    (fun code cells told =>
      conversationStep TurnM.lift TurnM.attempt
        (fun req => match req with
          | .call f receiver args =>
            requestCall TurnM.lift (nestedCall (fuel - 2)) f receiver args
          | other => TurnM.lift (perform (callFn (fuel - 2))
              (fun callee args => constructTarget (fuel - 2) callee callee args) code cells other))
        (fun answer => TurnM.lift (converse (fuel - 2) code cells answer)) told)
    wrapper callback [] ⟨state, jobs⟩
  let .unknown "opaque stopped after callback" final := run |
    throw (IO.userError "the second host step should refuse")
  let .unknown "opaque stopped after callback" legacy := callFn fuel wrapper callback [] state |
    throw (IO.userError "the legacy callback should refuse at the same fuel")
  letI : DecidableEq w.σ := by
    change DecidableEq Nat
    infer_instance
  unless (show Nat from final.state.world.state) == 20 && (StateMatch.state legacy final.state).isSome do
    throw (IO.userError "callback state or effects changed")
  assertExtended final #["callback"]

-- A real getter demonstrates why a projected callback cannot serve as the
-- queue-aware implementation of an operation that invokes user code.
private def checkGetterBoundary : IO Unit := do
  let setup : M World.probe (Val × Addr) := do
    let getter ← makeFunc emptyEnv (.mk "get x" .normal false []
      [log "getter", .ret (.str "value")])
    let object ← alloc { proto := some objectProto, props := [
      (.str "x", .accessor getter .undef true true)] }
    pure (getter, object)
  let .ok (getter, object) state := setup (initialState World.probe 0) |
    throw (IO.userError "unable to install getter")
  let .ok (.str "value") direct := nestedCall 48 getter (.obj object) [] ⟨state, jobs⟩ |
    throw (IO.userError "queue-carrying getter should return")
  assertExtended direct #["getter"]
  let project : Call World.probe := fun f r a s => (nestedCall 48 f r a ⟨s, jobs⟩).erase
  let .ok (.str "value") projected := TurnM.lift (getProp project (.obj object) (.str "x")) ⟨state, jobs⟩ |
    throw (IO.userError "projected getter should preserve the ordinary result")
  unless (StateMatch.probe projected.state direct.state).isSome do
    throw (IO.userError "the projected getter lost more than its FIFO")
  assertQueue projected

def main : IO Unit := do
  checkUpdatedBody .returns
  checkUpdatedBody .throws
  checkUpdatedBody .refuses
  checkUpdatedCallback
  checkGetterBoundary
  checkAlias true
  checkAlias false
  checkReentry false
  checkReentry true
  let w := callbackWorld false
  let turn := ⟨initialState w 0, jobs⟩
  let .unknown "host stepped before refusing" supported := callFnTurn 1 (.host "parseInt") .undef [.str "1"] turn |
    throw (IO.userError "supported host call did not run")
  unless (show Nat from supported.state.world.state) == 1 do
    throw (IO.userError "host state lost on refusal")
  assertQueue supported
  let .unknown "host function parseInt is called" refused :=
    callFnTurn 1 (.host "parseInt") .undef [.obj objectProto] turn |
      throw (IO.userError "host call with an object crossed the boundary")
  unless (show Nat from refused.state.world.state) == 0 do
    throw (IO.userError "host was stepped on refused arguments")
  assertQueue refused
