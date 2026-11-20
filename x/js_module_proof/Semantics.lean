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
  | initializeCapture (key : String) (expression : NumericExpr) (parameters : List (String × Nat)) (prior : List (String × String))
  | invokeCaptured (captures : List (String × String)) (parameters : List String) (arguments : List Nat) (body : NumericExpr)
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
   not evaluate an unsupported or uninitialized operation. The local frame is
   returned so captured cells can be observed after a call. -/
structure ExpressionResult (α : Type) where
  locals : List (String × Nat)
  completion : Except ThrownValue α

mutual
def evaluateExpression (locals globals : List (String × Nat)) : NumericExpr → Option (ExpressionResult Nat)
  | .literal value => some ⟨locals, .ok value⟩
  | .name name => do
    let value ← match lookupBinding locals name with
      | some value => some value
      | none => lookupBinding globals name
    pure ⟨locals, .ok value⟩
  | .add left right => do
    let leftResult ← evaluateExpression locals globals left
    match leftResult.completion with
    | .error value => pure ⟨leftResult.locals, .error value⟩
    | .ok leftValue =>
      let rightResult ← evaluateExpression leftResult.locals globals right
      match rightResult.completion with
      | .error value => pure ⟨rightResult.locals, .error value⟩
      | .ok rightValue => pure ⟨rightResult.locals, .ok (leftValue + rightValue)⟩
  | .conditional test consequent alternative => do
    let result ← evaluateExpression locals globals test
    match result.completion with
    | .error value => pure ⟨result.locals, .error value⟩
    | .ok condition =>
      if condition == 0 then evaluateExpression result.locals globals alternative
      else evaluateExpression result.locals globals consequent
  | .local name _ initial body => do
    let initialResult ← evaluateExpression locals globals initial
    match initialResult.completion with
    | .error value => pure ⟨initialResult.locals, .error value⟩
    | .ok value =>
      let bodyResult ← evaluateExpression ((name, value) :: initialResult.locals) globals body
      match bodyResult.locals with
      | [] => none
      | _ :: tail => pure ⟨tail, bodyResult.completion⟩
  | .assign name expression body => do
    let result ← evaluateExpression locals globals expression
    match result.completion with
    | .error value => pure ⟨result.locals, .error value⟩
    | .ok value =>
      let updated ← updateBinding result.locals name value
      evaluateExpression updated globals body
  | .raise expression => do
    let result ← evaluateExpression locals globals expression
    match result.completion with
    | .error value => pure ⟨result.locals, .error value⟩
    | .ok value => pure ⟨result.locals, .error (.number value)⟩
  | .raisePrimitive value => some ⟨locals, .error value⟩
  | .argumentsNil => none
  | .argumentsCons _ _ => none
  | .invoke _ parameters arguments body => do
    let args ← evaluateArguments locals globals arguments
    match args.completion with
    | .error value => pure ⟨args.locals, .error value⟩
    | .ok values =>
      if parameters.length == values.length then
        (evaluateExpression (parameters.zip values) globals body).map (fun result => ⟨args.locals, result.completion⟩)
      else none

def evaluateArguments (locals globals : List (String × Nat)) : NumericExpr → Option (ExpressionResult (List Nat))
  | .argumentsNil => some ⟨locals, .ok []⟩
  | .argumentsCons head tail => do
    let headResult ← evaluateExpression locals globals head
    match headResult.completion with
    | .error value => pure ⟨headResult.locals, .error value⟩
    | .ok value =>
      let tailResult ← evaluateArguments headResult.locals globals tail
      match tailResult.completion with
      | .error error => pure ⟨tailResult.locals, .error error⟩
      | .ok values => pure ⟨tailResult.locals, .ok (value :: values)⟩
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

/- Closure cells have keys that cannot be JavaScript identifiers. Keeping them
   in the shared binding store makes closure creation and calls participate in
   the same execution state and observations as every other operation. -/
def loadCaptures (bindings : List (String × Nat)) : List (String × String) → Option (List (String × Nat))
  | [] => some []
  | (name, key) :: rest => do
    let value ← lookupBinding bindings key
    let tail ← loadCaptures bindings rest
    pure ((name, value) :: tail)

def storeCaptures (bindings : List (String × Nat)) : List (String × String) → List (String × Nat) → Option (List (String × Nat))
  | [], [] => some bindings
  | (name, key) :: rest, (actual, value) :: values => do
    if name != actual then none else
      let updated ← updateBinding bindings key value
      storeCaptures updated rest values
  | [], _ :: _ => none
  | _ :: _, [] => none

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
      let result ← evaluateExpression parameters state.bindings expression
      match result.completion with
      | .error value => pure { state with thrown := some value }
      | .ok value => pure { state with returns := state.returns ++ [some value] }
    | .initializeCapture key expression parameters prior => do
      let captures ← loadCaptures state.bindings prior
      let result ← evaluateExpression (parameters ++ captures) state.bindings expression
      match result.completion with
      | .error _ => none
      | .ok value => declare state key value
    | .invokeCaptured captures parameters arguments body => do
      if parameters.length != arguments.length then none else
        let values ← loadCaptures state.bindings captures
        let result ← evaluateExpression (parameters.zip arguments ++ values) state.bindings body
        let bindings ← storeCaptures state.bindings captures (result.locals.drop parameters.length)
        match result.completion with
        | .error value => pure { state with bindings, thrown := some value }
        | .ok value => pure { state with bindings, returns := state.returns ++ [some value] }

def runExecution {α : Type} : ExecutionState α → List Operation → Option (ExecutionState α)
  | state, [] => some state
  | state, operation :: operations => do
    let next ← executeStep state operation
    runExecution next operations
