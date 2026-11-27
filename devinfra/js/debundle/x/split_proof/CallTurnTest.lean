import EsmSplit.CoreJS.CallTurn

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

def main : IO Unit := do
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
