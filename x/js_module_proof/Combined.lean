/- This file follows Semantics.lean and Effects.lean in generated certificates. -/
def decodeCombined (initial : Nat) (operation : String) : Option Operation :=
  if operation == "init" then some (.initialize initial)
  else if operation == "increment" then some .increment
  else if operation == "record" then some .recordCell
  else none

def sourceCombined (initial : Nat) (operations : List String) : Option (ExecutionState Unit) := do
  let operations ← operations.mapM (decodeCombined initial)
  runExecution ⟨[], [], [], none, [], none⟩ operations

def outputCombined (initial : Nat) (modules : List EffectModule) (entry : Nat) : Option (ExecutionState Unit) := do
  let operations ← effectTrace modules entry
  sourceCombined initial operations

def currentCalls (state : ExecutionState Unit) (count : Nat) : Option (List Nat) := do
  if state.error.isSome || state.cell.isNone then none else
    let result ← runExecution state (List.replicate count .readCell)
    pure ((result.events.drop state.events.length).filterMap (fun event =>
      match event with | .number value => some value | .text _ => none))

theorem combined_current_preserved (source output : Option (ExecutionState Unit))
    (same : source = output) (count : Nat) :
    source.bind (fun state => currentCalls state count) =
      output.bind (fun state => currentCalls state count) := by
  rw [same]
