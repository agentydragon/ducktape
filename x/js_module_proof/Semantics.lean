/- Shared execution state and transitions for the supported subsets. -/
inductive Observation where
  | text (value : String)
  | number (value : Nat)
  deriving DecidableEq, Repr

inductive Operation where
  | initialize (value : Nat)
  | increment
  | captureSnapshot
  | incrementSnapshot
  | readCell
  | readSnapshot
  | recordText (value : String)
  | recordCell
  | declareBinding (name : String) (value : Nat)
  | addBinding (target source : String)
  | observeBinding (name : String)
  deriving DecidableEq, Repr

structure ExecutionState (α : Type) where
  bindings : List (String × Nat)
  events : List Observation
  replies : List (Option α)
  error : Option α
  deriving DecidableEq, Repr

def lookupBinding : List (String × Nat) → String → Option Nat
  | [], _ => none
  | (key, value) :: rest, name => if key == name then some value else lookupBinding rest name

def updateBinding : List (String × Nat) → String → Nat → Option (List (String × Nat))
  | [], _, _ => none
  | (key, old) :: rest, name, value =>
    if key == name then some ((key, value) :: rest)
    else (updateBinding rest name value).map (fun tail => (key, old) :: tail)

def ExecutionState.cell (state : ExecutionState α) : Option Nat := lookupBinding state.bindings "$live"
def ExecutionState.snapshot (state : ExecutionState α) : Option Nat := lookupBinding state.bindings "$snapshot"

def recordObservation {α : Type} (state : ExecutionState α) (event : Observation) : ExecutionState α :=
  match state.replies with
  | [] => { state with events := state.events ++ [event] }
  | reply :: rest => { state with events := state.events ++ [event], replies := rest, error := reply }

def declare {α : Type} (state : ExecutionState α) (name : String) (value : Nat) : Option (ExecutionState α) :=
  if (lookupBinding state.bindings name).isSome then none
  else some { state with bindings := state.bindings ++ [(name, value)] }

def incrementBy {α : Type} (state : ExecutionState α) (target : String) (value : Nat) : Option (ExecutionState α) := do
  let old ← lookupBinding state.bindings target
  let bindings ← updateBinding state.bindings target (old + value)
  pure { state with bindings }

def executeStep {α : Type} (state : ExecutionState α) (operation : Operation) : Option (ExecutionState α) :=
  match state.error with
  | some _ => some state
  | none =>
    match operation with
    | .initialize value => declare state "$live" value
    | .increment => incrementBy state "$live" 1
    | .captureSnapshot => do
      let value ← state.cell
      declare state "$snapshot" value
    | .incrementSnapshot => incrementBy state "$snapshot" 1
    | .readCell => do
      let value ← state.cell
      pure { state with events := state.events ++ [.number value] }
    | .readSnapshot => do
      let value ← state.snapshot
      pure { state with events := state.events ++ [.number value] }
    | .recordText value => some (recordObservation state (.text value))
    | .recordCell => do
      let value ← state.cell
      pure (recordObservation state (.number value))
    | .declareBinding name value => declare state name value
    | .addBinding target source => do
      let value ← lookupBinding state.bindings source
      incrementBy state target value
    | .observeBinding name => do
      let value ← lookupBinding state.bindings name
      pure (recordObservation state (.number value))

def runExecution {α : Type} : ExecutionState α → List Operation → Option (ExecutionState α)
  | state, [] => some state
  | state, operation :: operations => do
    let next ← executeStep state operation
    runExecution next operations
