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
  | multiply (left right : NumericExpr)
  | subtract (left right : NumericExpr)
  | equal (left right : NumericExpr)
  | less (left right : NumericExpr)
  | conditional (test consequent alternative : NumericExpr)
  | guard (test consequent continuation : NumericExpr)
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
  captured : List (String × Nat)
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
  | .equal left right => do
    let leftResult ← evaluateExpression locals globals left
    match leftResult.completion with
    | .error value => pure ⟨leftResult.locals, .error value⟩
    | .ok leftValue =>
      let rightResult ← evaluateExpression leftResult.locals globals right
      match rightResult.completion with
      | .error value => pure ⟨rightResult.locals, .error value⟩
      | .ok rightValue => pure ⟨rightResult.locals, .ok (if leftValue == rightValue then 1 else 0)⟩
  | .less left right => do
    let leftResult ← evaluateExpression locals globals left
    match leftResult.completion with
    | .error value => pure ⟨leftResult.locals, .error value⟩
    | .ok leftValue =>
      let rightResult ← evaluateExpression leftResult.locals globals right
      match rightResult.completion with
      | .error value => pure ⟨rightResult.locals, .error value⟩
      | .ok rightValue => pure ⟨rightResult.locals, .ok (if leftValue < rightValue then 1 else 0)⟩
  | .multiply left right => do
    let leftResult ← evaluateExpression locals globals left
    match leftResult.completion with
    | .error value => pure ⟨leftResult.locals, .error value⟩
    | .ok leftValue =>
      let rightResult ← evaluateExpression leftResult.locals globals right
      match rightResult.completion with
      | .error value => pure ⟨rightResult.locals, .error value⟩
      | .ok rightValue => pure ⟨rightResult.locals, .ok (leftValue * rightValue)⟩
  | .subtract left right => do
    let leftResult ← evaluateExpression locals globals left
    match leftResult.completion with
    | .error value => pure ⟨leftResult.locals, .error value⟩
    | .ok leftValue =>
      let rightResult ← evaluateExpression leftResult.locals globals right
      match rightResult.completion with
      | .error value => pure ⟨rightResult.locals, .error value⟩
      | .ok rightValue =>
        if leftValue < rightValue then none
        else pure ⟨rightResult.locals, .ok (leftValue - rightValue)⟩
  | .conditional test consequent alternative => do
    let result ← evaluateExpression locals globals test
    match result.completion with
    | .error value => pure ⟨result.locals, .error value⟩
    | .ok condition =>
      if condition == 0 then evaluateExpression result.locals globals alternative
      else evaluateExpression result.locals globals consequent
  | .guard test consequent alternative => do
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
  | .multiply _ _ => none
  | .subtract _ _ => none
  | .equal _ _ => none
  | .less _ _ => none
  | .conditional _ _ _ => none
  | .guard _ _ _ => none
  | .local _ _ _ _ => none
  | .assign _ _ _ => none
  | .raise _ => none
  | .raisePrimitive _ => none
  | .invoke _ _ _ _ => none
end

/- Captured cells belong to the same execution state as module bindings, but
   have their own store. An environment maps lexical names to cell identities. -/
def loadCaptures (bindings : List (String × Nat)) : List (String × String) → Option (List (String × Nat))
  | [] => some []
  | (name, key) :: rest => do
    let value ← lookupBinding bindings key
    let tail ← loadCaptures bindings rest
    pure ((name, value) :: tail)

/- Environments correspond when each lexical name reads the same value,
   even if the two executions allocated that value under different cell keys. -/
def relatedCapturedEnvironments (leftStore rightStore : List (String × Nat))
    (leftCapture rightCapture : List (String × String)) : Prop :=
  ∃ frame, loadCaptures leftStore leftCapture = some frame ∧
    loadCaptures rightStore rightCapture = some frame

def evaluateCapturedFrame (globals store : List (String × Nat)) (captures : List (String × String))
    (parameters : List String) (arguments : List Nat) (body : NumericExpr) : Option (ExpressionResult Nat) := do
  if parameters.length != arguments.length then none else
    let values ← loadCaptures store captures
    evaluateExpression (parameters.zip arguments ++ values) globals body

theorem captured_frame_evaluation_preserved
    (globals leftStore rightStore : List (String × Nat)) (leftCapture rightCapture : List (String × String))
    (parameters : List String) (arguments : List Nat) (body : NumericExpr)
    (related : relatedCapturedEnvironments leftStore rightStore leftCapture rightCapture) :
    evaluateCapturedFrame globals leftStore leftCapture parameters arguments body =
      evaluateCapturedFrame globals rightStore rightCapture parameters arguments body := by
  obtain ⟨frame, left, right⟩ := related
  unfold evaluateCapturedFrame
  rw [left, right]

def storeCaptures (bindings : List (String × Nat)) : List (String × String) → List (String × Nat) → Option (List (String × Nat))
  | [], [] => some bindings
  | (name, key) :: rest, (actual, value) :: values => do
    if name != actual then none else
      let updated ← updateBinding bindings key value
      storeCaptures updated rest values
  | [], _ :: _ => none
  | _ :: _, [] => none

/- Writeback returns a store only when reading its captured cells again yields
   exactly the updated lexical frame. This rejects conflicting aliases instead
   of silently losing a write. -/
def storeCapturedFrame (store : List (String × Nat)) (captures : List (String × String))
    (values : List (String × Nat)) : Option { result : List (String × Nat) // loadCaptures result captures = some values } := do
  let result ← storeCaptures store captures values
  if valid : loadCaptures result captures = some values then some ⟨result, valid⟩ else none

theorem captured_frame_writeback_preserved
    (leftCapture rightCapture : List (String × String)) (values : List (String × Nat))
    (left : { result : List (String × Nat) // loadCaptures result leftCapture = some values })
    (right : { result : List (String × Nat) // loadCaptures result rightCapture = some values }) :
    relatedCapturedEnvironments left.val right.val leftCapture rightCapture :=
  ⟨values, left.property, right.property⟩

/- One call preserves corresponding capture frames when both writebacks
   succeed. The returned subtype is produced by the same checked writeback
   operation used by the machine; allocation keys need not be equal. -/
theorem captured_call_frame_preserved
    (globals leftStore rightStore : List (String × Nat)) (leftCapture rightCapture : List (String × String))
    (parameters : List String) (arguments : List Nat) (body : NumericExpr) (result : ExpressionResult Nat)
    (related : relatedCapturedEnvironments leftStore rightStore leftCapture rightCapture)
    (evaluated : evaluateCapturedFrame globals leftStore leftCapture parameters arguments body = some result)
    (left : { store : List (String × Nat) // loadCaptures store leftCapture = some (result.locals.drop parameters.length) })
    (right : { store : List (String × Nat) // loadCaptures store rightCapture = some (result.locals.drop parameters.length) }) :
    evaluateCapturedFrame globals rightStore rightCapture parameters arguments body = some result ∧
      relatedCapturedEnvironments left.val right.val leftCapture rightCapture := by
  constructor
  · rw [← captured_frame_evaluation_preserved globals leftStore rightStore leftCapture rightCapture parameters arguments body related]
    exact evaluated
  · exact captured_frame_writeback_preserved leftCapture rightCapture _ left right

def ExecutionState.cell (state : ExecutionState α) : Option Nat := lookupBinding state.bindings "$live"
def ExecutionState.snapshot (state : ExecutionState α) : Option Nat := lookupBinding state.bindings "$snapshot"

def recordObservation {α : Type} (state : ExecutionState α) (event : Observation) : ExecutionState α :=
  match state.replies with
  | [] => { state with events := state.events ++ [event] }
  | reply :: rest => { state with events := state.events ++ [event], replies := rest, error := reply }

def declare {α : Type} (state : ExecutionState α) (name : String) (value : Nat) : Option (ExecutionState α) :=
  if (lookupBinding state.bindings name).isSome || (lookupBinding state.captured name).isSome then none
  else some { state with bindings := state.bindings ++ [(name, value)] }

def declareCapture {α : Type} (state : ExecutionState α) (key : String) (value : Nat) : Option (ExecutionState α) :=
  if (lookupBinding state.captured key).isSome || (lookupBinding state.bindings key).isSome then none
  else some { state with captured := state.captured ++ [(key, value)] }

def incrementBy {α : Type} (state : ExecutionState α) (target : String) (value : Nat) : Option (ExecutionState α) := do
  let old ← lookupBinding state.bindings target
  let bindings ← updateBinding state.bindings target (old + value)
  pure { state with bindings }

def finishCapturedCall {α : Type} (state : ExecutionState α) (captured : List (String × Nat))
    (completion : Except ThrownValue Nat) : ExecutionState α :=
  match completion with
  | .error value => { state with captured, thrown := some value }
  | .ok value => { state with captured, returns := state.returns ++ [some value] }

/- Once both executions have written the same resulting frame, the actual
   machine transition preserves observations and the capture relation. -/
theorem captured_completion_preserved {α : Type}
    (leftState rightState : ExecutionState α) (leftCapture rightCapture : List (String × String))
    (values : List (String × Nat)) (completion : Except ThrownValue Nat)
    (left : { store : List (String × Nat) // loadCaptures store leftCapture = some values })
    (right : { store : List (String × Nat) // loadCaptures store rightCapture = some values })
    (sameBindings : leftState.bindings = rightState.bindings)
    (sameEvents : leftState.events = rightState.events)
    (sameReplies : leftState.replies = rightState.replies)
    (sameError : leftState.error = rightState.error)
    (sameReturns : leftState.returns = rightState.returns)
    (sameThrown : leftState.thrown = rightState.thrown) :
    let leftNext := finishCapturedCall leftState left.val completion
    let rightNext := finishCapturedCall rightState right.val completion
    leftNext.bindings = rightNext.bindings ∧ leftNext.events = rightNext.events ∧
      leftNext.replies = rightNext.replies ∧ leftNext.error = rightNext.error ∧
      leftNext.returns = rightNext.returns ∧ leftNext.thrown = rightNext.thrown ∧
      relatedCapturedEnvironments leftNext.captured rightNext.captured leftCapture rightCapture := by
  cases completion with
  | error value =>
    exact ⟨sameBindings, sameEvents, sameReplies, sameError, sameReturns, rfl,
      captured_frame_writeback_preserved leftCapture rightCapture values left right⟩
  | ok value =>
    exact ⟨sameBindings, sameEvents, sameReplies, sameError,
      congrArg (fun returns => returns ++ [some value]) sameReturns, sameThrown,
      captured_frame_writeback_preserved leftCapture rightCapture values left right⟩

theorem captured_machine_step_preserved {α : Type}
    (leftState rightState : ExecutionState α) (leftCapture rightCapture : List (String × String))
    (parameters : List String) (arguments : List Nat) (body : NumericExpr) (result : ExpressionResult Nat)
    (sameBindings : leftState.bindings = rightState.bindings)
    (sameEvents : leftState.events = rightState.events)
    (sameReplies : leftState.replies = rightState.replies)
    (sameError : leftState.error = rightState.error)
    (sameReturns : leftState.returns = rightState.returns)
    (sameThrown : leftState.thrown = rightState.thrown)
    (related : relatedCapturedEnvironments leftState.captured rightState.captured leftCapture rightCapture)
    (evaluated : evaluateCapturedFrame leftState.bindings leftState.captured leftCapture parameters arguments body = some result)
    (left : { store : List (String × Nat) // loadCaptures store leftCapture = some (result.locals.drop parameters.length) })
    (right : { store : List (String × Nat) // loadCaptures store rightCapture = some (result.locals.drop parameters.length) }) :
    evaluateCapturedFrame rightState.bindings rightState.captured rightCapture parameters arguments body = some result ∧
      let leftNext := finishCapturedCall leftState left.val result.completion
      let rightNext := finishCapturedCall rightState right.val result.completion
      leftNext.bindings = rightNext.bindings ∧ leftNext.events = rightNext.events ∧
        leftNext.replies = rightNext.replies ∧ leftNext.error = rightNext.error ∧
        leftNext.returns = rightNext.returns ∧ leftNext.thrown = rightNext.thrown ∧
        relatedCapturedEnvironments leftNext.captured rightNext.captured leftCapture rightCapture := by
  constructor
  · rw [← sameBindings]
    rw [← captured_frame_evaluation_preserved leftState.bindings leftState.captured rightState.captured leftCapture rightCapture parameters arguments body related]
    exact evaluated
  · exact captured_completion_preserved leftState rightState leftCapture rightCapture _ result.completion left right
      sameBindings sameEvents sameReplies sameError sameReturns sameThrown

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
      let captures ← loadCaptures state.captured prior
      let result ← evaluateExpression (parameters ++ captures) state.bindings expression
      match result.completion with
      | .error _ => none
      | .ok value => declareCapture state key value
    | .invokeCaptured captures parameters arguments body => do
      let result ← evaluateCapturedFrame state.bindings state.captured captures parameters arguments body
      let stored ← storeCapturedFrame state.captured captures (result.locals.drop parameters.length)
      pure (finishCapturedCall state stored.val result.completion)

def runExecution {α : Type} : ExecutionState α → List Operation → Option (ExecutionState α)
  | state, [] => some state
  | state, operation :: operations => do
    let next ← executeStep state operation
    runExecution next operations

theorem executeCaptured_success {α : Type} (state : ExecutionState α)
    (captures : List (String × String)) (parameters : List String) (arguments : List Nat)
    (body : NumericExpr) (result : ExpressionResult Nat)
    (stored : { store : List (String × Nat) // loadCaptures store captures = some (result.locals.drop parameters.length) })
    (noError : state.error = none) (noThrow : state.thrown = none)
    (evaluated : evaluateCapturedFrame state.bindings state.captured captures parameters arguments body = some result)
    (written : storeCapturedFrame state.captured captures (result.locals.drop parameters.length) = some stored) :
    executeStep state (.invokeCaptured captures parameters arguments body) =
      some (finishCapturedCall state stored.val result.completion) := by
  unfold executeStep
  rw [noError, noThrow]
  change (do
    let frame ← evaluateCapturedFrame state.bindings state.captured captures parameters arguments body
    let heap ← storeCapturedFrame state.captured captures (frame.locals.drop parameters.length)
    pure (finishCapturedCall state heap.val frame.completion)) = _
  rw [evaluated]
  change (do
    let heap ← storeCapturedFrame state.captured captures (result.locals.drop parameters.length)
    pure (finishCapturedCall state heap.val result.completion)) = _
  rw [written]
  rfl

theorem executeCaptured_decompose {α : Type} (state next : ExecutionState α)
    (captures : List (String × String)) (parameters : List String) (arguments : List Nat) (body : NumericExpr)
    (noError : state.error = none) (noThrow : state.thrown = none)
    (stepped : executeStep state (.invokeCaptured captures parameters arguments body) = some next) :
    ∃ (result : ExpressionResult Nat)
      (stored : { store : List (String × Nat) // loadCaptures store captures = some (result.locals.drop parameters.length) }),
      evaluateCapturedFrame state.bindings state.captured captures parameters arguments body = some result ∧
      storeCapturedFrame state.captured captures (result.locals.drop parameters.length) = some stored ∧
      next = finishCapturedCall state stored.val result.completion := by
  unfold executeStep at stepped
  rw [noError, noThrow] at stepped
  change (do
    let frame ← evaluateCapturedFrame state.bindings state.captured captures parameters arguments body
    let heap ← storeCapturedFrame state.captured captures (frame.locals.drop parameters.length)
    pure (finishCapturedCall state heap.val frame.completion)) = some next at stepped
  cases evaluated : evaluateCapturedFrame state.bindings state.captured captures parameters arguments body with
  | none =>
    rw [evaluated] at stepped
    cases stepped
  | some result =>
    rw [evaluated] at stepped
    change (do
      let heap ← storeCapturedFrame state.captured captures (result.locals.drop parameters.length)
      pure (finishCapturedCall state heap.val result.completion)) = some next at stepped
    cases written : storeCapturedFrame state.captured captures (result.locals.drop parameters.length) with
    | none =>
      rw [written] at stepped
      cases stepped
    | some stored =>
      rw [written] at stepped
      change some (finishCapturedCall state stored.val result.completion) = some next at stepped
      cases stepped
      exact ⟨result, stored, rfl, written, rfl⟩

theorem captured_paired_step_preserved {α : Type}
    (leftState rightState : ExecutionState α) (leftCapture rightCapture : List (String × String))
    (parameters : List String) (arguments : List Nat) (body : NumericExpr) (result : ExpressionResult Nat)
    (left : { store : List (String × Nat) // loadCaptures store leftCapture = some (result.locals.drop parameters.length) })
    (right : { store : List (String × Nat) // loadCaptures store rightCapture = some (result.locals.drop parameters.length) })
    (sameBindings : leftState.bindings = rightState.bindings)
    (sameEvents : leftState.events = rightState.events)
    (sameReplies : leftState.replies = rightState.replies)
    (sameError : leftState.error = rightState.error)
    (sameReturns : leftState.returns = rightState.returns)
    (sameThrown : leftState.thrown = rightState.thrown)
    (related : relatedCapturedEnvironments leftState.captured rightState.captured leftCapture rightCapture)
    (noError : leftState.error = none) (noThrow : leftState.thrown = none)
    (evaluated : evaluateCapturedFrame leftState.bindings leftState.captured leftCapture parameters arguments body = some result)
    (leftWritten : storeCapturedFrame leftState.captured leftCapture (result.locals.drop parameters.length) = some left)
    (rightWritten : storeCapturedFrame rightState.captured rightCapture (result.locals.drop parameters.length) = some right) :
    executeStep leftState (.invokeCaptured leftCapture parameters arguments body) =
        some (finishCapturedCall leftState left.val result.completion) ∧
      executeStep rightState (.invokeCaptured rightCapture parameters arguments body) =
        some (finishCapturedCall rightState right.val result.completion) ∧
      let leftNext := finishCapturedCall leftState left.val result.completion
      let rightNext := finishCapturedCall rightState right.val result.completion
      leftNext.bindings = rightNext.bindings ∧ leftNext.events = rightNext.events ∧
        leftNext.replies = rightNext.replies ∧ leftNext.error = rightNext.error ∧
        leftNext.returns = rightNext.returns ∧ leftNext.thrown = rightNext.thrown ∧
        relatedCapturedEnvironments leftNext.captured rightNext.captured leftCapture rightCapture := by
  have step := captured_machine_step_preserved leftState rightState leftCapture rightCapture parameters arguments body result
    sameBindings sameEvents sameReplies sameError sameReturns sameThrown related evaluated left right
  refine ⟨executeCaptured_success leftState leftCapture parameters arguments body result left noError noThrow evaluated leftWritten, ?_, step.2⟩
  exact executeCaptured_success rightState rightCapture parameters arguments body result right
    (sameError.symm.trans noError) (sameThrown.symm.trans noThrow) step.1 rightWritten

def capturedStatesRelated {α : Type} (leftCapture rightCapture : List (String × String))
    (left right : ExecutionState α) : Prop :=
  left.bindings = right.bindings ∧ left.events = right.events ∧ left.replies = right.replies ∧
    left.error = right.error ∧ left.returns = right.returns ∧ left.thrown = right.thrown ∧
    relatedCapturedEnvironments left.captured right.captured leftCapture rightCapture

theorem captured_unblocked_step_related {α : Type}
    (leftCapture rightCapture : List (String × String)) (parameters : List String) (arguments : List Nat)
    (body : NumericExpr) (left right leftNext rightNext : ExecutionState α)
    (related : capturedStatesRelated leftCapture rightCapture left right)
    (noError : left.error = none) (noThrow : left.thrown = none)
    (leftStep : executeStep left (.invokeCaptured leftCapture parameters arguments body) = some leftNext)
    (rightStep : executeStep right (.invokeCaptured rightCapture parameters arguments body) = some rightNext) :
    capturedStatesRelated leftCapture rightCapture leftNext rightNext := by
  obtain ⟨sameBindings, sameEvents, sameReplies, sameError, sameReturns, sameThrown, sameCaptures⟩ := related
  obtain ⟨leftResult, leftStored, leftEvaluated, leftWritten, leftFinished⟩ :=
    executeCaptured_decompose left leftNext leftCapture parameters arguments body noError noThrow leftStep
  obtain ⟨rightResult, rightStored, rightEvaluated, rightWritten, rightFinished⟩ :=
    executeCaptured_decompose right rightNext rightCapture parameters arguments body
      (sameError.symm.trans noError) (sameThrown.symm.trans noThrow) rightStep
  have sameEvaluation : evaluateCapturedFrame left.bindings left.captured leftCapture parameters arguments body =
      evaluateCapturedFrame right.bindings right.captured rightCapture parameters arguments body := by
    rw [← sameBindings]
    exact captured_frame_evaluation_preserved left.bindings left.captured right.captured leftCapture rightCapture
      parameters arguments body sameCaptures
  have sameResult : leftResult = rightResult := Option.some.inj (leftEvaluated.symm.trans (sameEvaluation.trans rightEvaluated))
  cases sameResult
  rw [leftFinished, rightFinished]
  exact captured_completion_preserved left right leftCapture rightCapture _ leftResult.completion leftStored rightStored
    sameBindings sameEvents sameReplies sameError sameReturns sameThrown

theorem captured_step_related {α : Type}
    (leftCapture rightCapture : List (String × String)) (parameters : List String) (arguments : List Nat)
    (body : NumericExpr) (left right leftNext rightNext : ExecutionState α)
    (related : capturedStatesRelated leftCapture rightCapture left right)
    (leftStep : executeStep left (.invokeCaptured leftCapture parameters arguments body) = some leftNext)
    (rightStep : executeStep right (.invokeCaptured rightCapture parameters arguments body) = some rightNext) :
    capturedStatesRelated leftCapture rightCapture leftNext rightNext := by
  have sameError : left.error = right.error := related.2.2.2.1
  have sameThrown : left.thrown = right.thrown := related.2.2.2.2.2.1
  cases leftError : left.error with
  | some error =>
    have rightError : right.error = some error := sameError.symm.trans leftError
    have leftBlocked : executeStep left (.invokeCaptured leftCapture parameters arguments body) = some left := by
      unfold executeStep
      rw [leftError]
    have rightBlocked : executeStep right (.invokeCaptured rightCapture parameters arguments body) = some right := by
      unfold executeStep
      rw [rightError]
    rw [leftBlocked] at leftStep
    rw [rightBlocked] at rightStep
    cases leftStep
    cases rightStep
    exact related
  | none =>
    cases leftThrown : left.thrown with
    | some thrown =>
      have rightError : right.error = none := sameError.symm.trans leftError
      have rightThrown : right.thrown = some thrown := sameThrown.symm.trans leftThrown
      have leftBlocked : executeStep left (.invokeCaptured leftCapture parameters arguments body) = some left := by
        unfold executeStep
        rw [leftError, leftThrown]
        rfl
      have rightBlocked : executeStep right (.invokeCaptured rightCapture parameters arguments body) = some right := by
        unfold executeStep
        rw [rightError, rightThrown]
        rfl
      rw [leftBlocked] at leftStep
      rw [rightBlocked] at rightStep
      cases leftStep
      cases rightStep
      exact related
    | none =>
      exact captured_unblocked_step_related leftCapture rightCapture parameters arguments body left right leftNext rightNext
        related leftError leftThrown leftStep rightStep

inductive RelatedOperations (relation : Operation → Operation → Prop) : List Operation → List Operation → Prop where
  | nil : RelatedOperations relation [] []
  | cons {left right leftTail rightTail} : relation left right → RelatedOperations relation leftTail rightTail →
      RelatedOperations relation (left :: leftTail) (right :: rightTail)

/- Lift a relation preserved by each pair of machine steps to any finite pair
   of operation sequences. Both executions must complete in the model. -/
theorem runExecution_relation_preserved {α : Type}
    (stateRel : ExecutionState α → ExecutionState α → Prop)
    (operationRel : Operation → Operation → Prop)
    (preserve : ∀ left right leftOp rightOp leftNext rightNext,
      stateRel left right → operationRel leftOp rightOp →
      executeStep left leftOp = some leftNext → executeStep right rightOp = some rightNext →
      stateRel leftNext rightNext)
    (leftOps rightOps : List Operation) (operations : RelatedOperations operationRel leftOps rightOps)
    (left right leftFinal rightFinal : ExecutionState α)
    (initial : stateRel left right)
    (leftRun : runExecution left leftOps = some leftFinal)
    (rightRun : runExecution right rightOps = some rightFinal) : stateRel leftFinal rightFinal := by
  induction operations generalizing left right with
  | nil =>
    change some left = some leftFinal at leftRun
    change some right = some rightFinal at rightRun
    cases leftRun
    cases rightRun
    exact initial
  | @cons leftOp rightOp leftTail rightTail opRelated tailsRelated inductionHypothesis =>
    change (executeStep left leftOp).bind (fun next => runExecution next leftTail) = some leftFinal at leftRun
    change (executeStep right rightOp).bind (fun next => runExecution next rightTail) = some rightFinal at rightRun
    cases leftStep : executeStep left leftOp with
    | none =>
      rw [leftStep] at leftRun
      cases leftRun
    | some leftNext =>
      cases rightStep : executeStep right rightOp with
      | none =>
        rw [rightStep] at rightRun
        cases rightRun
      | some rightNext =>
        rw [leftStep] at leftRun
        rw [rightStep] at rightRun
        exact inductionHypothesis leftNext rightNext
          (preserve left right leftOp rightOp leftNext rightNext initial opRelated leftStep rightStep)
          leftRun rightRun

inductive PairedCapturedOperation (leftCapture rightCapture : List (String × String)) : Operation → Operation → Prop where
  | call (parameters : List String) (arguments : List Nat) (body : NumericExpr) :
      PairedCapturedOperation leftCapture rightCapture (.invokeCaptured leftCapture parameters arguments body)
        (.invokeCaptured rightCapture parameters arguments body)

theorem captured_call_sequence_related {α : Type}
    (leftCapture rightCapture : List (String × String)) (leftOps rightOps : List Operation)
    (operations : RelatedOperations (PairedCapturedOperation leftCapture rightCapture) leftOps rightOps)
    (left right leftFinal rightFinal : ExecutionState α)
    (initial : capturedStatesRelated leftCapture rightCapture left right)
    (leftRun : runExecution left leftOps = some leftFinal)
    (rightRun : runExecution right rightOps = some rightFinal) :
    capturedStatesRelated leftCapture rightCapture leftFinal rightFinal := by
  apply runExecution_relation_preserved (capturedStatesRelated leftCapture rightCapture)
    (PairedCapturedOperation leftCapture rightCapture) ?_ leftOps rightOps operations left right leftFinal rightFinal
    initial leftRun rightRun
  intro leftState rightState leftOp rightOp leftStateNext rightStateNext related operation leftStep rightStep
  cases operation with
  | call parameters arguments body =>
    exact captured_step_related leftCapture rightCapture parameters arguments body leftState rightState leftStateNext
      rightStateNext related leftStep rightStep
