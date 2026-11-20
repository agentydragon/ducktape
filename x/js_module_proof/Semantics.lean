/- Shared execution state and transitions for the supported subsets. -/
inductive Observation where
  | text (value : String)
  | number (value : Nat)
  deriving DecidableEq, Repr

inductive ThrownValue where
  | number (value : Nat)
  | text (value : String)
  | boolean (value : Bool)
  | null
  | undefined
  deriving DecidableEq, Repr

/- Expressions are evaluated by the same transition system as the original
   operations. Parameters shadow module bindings; arithmetic is restricted by
   the reported JavaScript safe-integer assumption. -/
inductive NumericExpr where
  | literal (value : Nat)
  | name (name : String)
  | add (left right : NumericExpr)
  | conditional (test consequent alternative : NumericExpr)
  | local (name : String) (mutable : Bool) (initial body : NumericExpr)
  | assign (name : String) (value body : NumericExpr)
  | raise (value : NumericExpr)
  | raisePrimitive (value : ThrownValue)
  | argumentsNil
  | argumentsCons (head tail : NumericExpr)
  | invoke (name : String) (parameters : List String) (arguments body : NumericExpr)
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
  thrown : Option ThrownValue
  deriving DecidableEq, Repr

def lookupBinding : List (String × Nat) → String → Option Nat
  | [], _ => none
  | (key, value) :: rest, name => if key == name then some value else lookupBinding rest name

def updateBinding : List (String × Nat) → String → Nat → Option (List (String × Nat))
  | [], _, _ => none
  | (key, old) :: rest, name, value =>
    if key == name then some ((key, value) :: rest)
    else (updateBinding rest name value).map (fun tail => (key, old) :: tail)

/- `Except.error` is a JavaScript throw, while `none` means the model could
   not evaluate an unsupported or uninitialized operation. -/
mutual
def evaluateExpression (locals globals : List (String × Nat)) : NumericExpr → Option (Except ThrownValue Nat)
  | .literal value => some (.ok value)
  | .name name => match lookupBinding locals name with
    | some value => some (.ok value)
    | none => (lookupBinding globals name).map Except.ok
  | .add left right => do
    match ← evaluateExpression locals globals left with
    | .error value => pure (.error value)
    | .ok leftValue =>
      match ← evaluateExpression locals globals right with
      | .error value => pure (.error value)
      | .ok rightValue => pure (.ok (leftValue + rightValue))
  | .conditional test consequent alternative => do
    match ← evaluateExpression locals globals test with
    | .error value => pure (.error value)
    | .ok condition =>
      if condition == 0 then evaluateExpression locals globals alternative
      else evaluateExpression locals globals consequent
  | .local name _ initial body => do
    match ← evaluateExpression locals globals initial with
    | .error value => pure (.error value)
    | .ok value => evaluateExpression ((name, value) :: locals) globals body
  | .assign name expression body => do
    match ← evaluateExpression locals globals expression with
    | .error value => pure (.error value)
    | .ok value =>
      let updated ← updateBinding locals name value
      evaluateExpression updated globals body
  | .raise expression => do
    match ← evaluateExpression locals globals expression with
    | .error value => pure (.error value)
    | .ok value => pure (.error (.number value))
  | .raisePrimitive value => some (.error value)
  | .argumentsNil => none
  | .argumentsCons _ _ => none
  | .invoke _ parameters arguments body => do
    match ← evaluateArguments locals globals arguments with
    | .error value => pure (.error value)
    | .ok values =>
      if parameters.length == values.length then
        evaluateExpression (parameters.zip values) globals body
      else none

def evaluateArguments (locals globals : List (String × Nat)) : NumericExpr → Option (Except ThrownValue (List Nat))
  | .argumentsNil => some (.ok [])
  | .argumentsCons head tail => do
    match ← evaluateExpression locals globals head with
    | .error value => pure (.error value)
    | .ok value =>
      match ← evaluateArguments locals globals tail with
      | .error error => pure (.error error)
      | .ok values => pure (.ok (value :: values))
  | .literal _ => none
  | .name _ => none
  | .add _ _ => none
  | .conditional _ _ _ => none
  | .local _ _ _ _ => none
  | .assign _ _ _ => none
  | .raise _ => none
  | .raisePrimitive _ => none
  | .invoke _ _ _ _ => none
end

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
    | .throwNumber value => some { state with thrown := some (.number value) }
    | .returnExpression expression parameters => do
      match ← evaluateExpression parameters state.bindings expression with
      | .error value => pure { state with thrown := some value }
      | .ok value => pure { state with returns := state.returns ++ [some value] }

def runExecution {α : Type} : ExecutionState α → List Operation → Option (ExecutionState α)
  | state, [] => some state
  | state, operation :: operations => do
    let next ← executeStep state operation
    runExecution next operations
