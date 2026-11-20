/- Shared execution state and transitions for the supported subsets. -/
inductive Observation where
  | text (value : String)
  | number (value : Nat)
  deriving DecidableEq, Repr

/- Expressions are evaluated by the same transition system as the original
   operations. Parameters shadow module bindings; arithmetic is restricted by
   the reported JavaScript safe-integer assumption. -/
inductive NumericExpr where
  | literal (value : Nat)
  | name (name : String)
  | add (left right : NumericExpr)
  | conditional (test consequent alternative : NumericExpr)
  | local (name : String) (initial body : NumericExpr)
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
  | copyBinding (target source : String)
  | incrementBinding (name : String)
  | addBinding (target source : String)
  | observeBinding (name : String)
  | readBinding (name : String)
  | returnBinding (name : String)
  | returnUndefined
  | throwNumber (value : Nat)
  | returnExpression (expression : NumericExpr) (parameters : List (String × Nat))
  deriving DecidableEq, Repr

structure ExecutionState (α : Type) where
  bindings : List (String × Nat)
  events : List Observation
  replies : List (Option α)
  error : Option α
  returns : List (Option Nat)
  thrown : Option Nat
  deriving DecidableEq, Repr

def lookupBinding : List (String × Nat) → String → Option Nat
  | [], _ => none
  | (key, value) :: rest, name => if key == name then some value else lookupBinding rest name

def evaluateExpression (locals globals : List (String × Nat)) : NumericExpr → Option Nat
  | .literal value => some value
  | .name name => match lookupBinding locals name with
    | some value => some value
    | none => lookupBinding globals name
  | .add left right => do
    let leftValue ← evaluateExpression locals globals left
    let rightValue ← evaluateExpression locals globals right
    pure (leftValue + rightValue)
  | .conditional test consequent alternative => do
    let condition ← evaluateExpression locals globals test
    if condition == 0 then evaluateExpression locals globals alternative
    else evaluateExpression locals globals consequent
  | .local name initial body => do
    let value ← evaluateExpression locals globals initial
    evaluateExpression ((name, value) :: locals) globals body

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
    if state.thrown.isSome then some state else
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
    | .copyBinding target source => do
      let value ← lookupBinding state.bindings source
      declare state target value
    | .incrementBinding name => incrementBy state name 1
    | .addBinding target source => do
      let value ← lookupBinding state.bindings source
      incrementBy state target value
    | .observeBinding name => do
      let value ← lookupBinding state.bindings name
      pure (recordObservation state (.number value))
    | .readBinding name => do
      let _ ← lookupBinding state.bindings name
      pure state
    | .returnBinding name => do
      let value ← lookupBinding state.bindings name
      pure { state with returns := state.returns ++ [some value] }
    | .returnUndefined => some { state with returns := state.returns ++ [none] }
    | .throwNumber value => some { state with thrown := some value }
    | .returnExpression expression parameters => do
      let value ← evaluateExpression parameters state.bindings expression
      pure { state with returns := state.returns ++ [some value] }

def runExecution {α : Type} : ExecutionState α → List Operation → Option (ExecutionState α)
  | state, [] => some state
  | state, operation :: operations => do
    let next ← executeStep state operation
    runExecution next operations
